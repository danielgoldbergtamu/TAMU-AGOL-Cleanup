# Author: Dalton Peterson
# Description: This script downloads arcgis online reports and updates the database with the new information. 
# It uses the ArcGIS API for Python to access the reports and pandas to manipulate the data before updating the database.


from arcgis.gis import GIS
import pandas as pd

from sqlalchemy import create_engine, text, Float
from sqlalchemy.dialects.mssql import NVARCHAR, DATETIME, BIT

from dotenv import load_dotenv
from os import getenv
from urllib.parse import quote_plus

import subprocess
import shutil
import sys

import argparse
import datetime
import os

from TAMU_AGOL_Runs import Run, merge_snapshot_into_history


# GLOBAL VARIABLES & INITIALIZATION
########################################################################################################################

CURRENT_DATE = datetime.datetime.now().date()
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, '.env')


# History is append-only (TAMU_AGOL_Runs.merge_snapshot_into_history). The duplicate-removal queries
# that used to live here deleted every history row identical to a newer one; removed 3 October 2026.

# Load environment variables from .env file
load_dotenv(dotenv_path=ENV_PATH, override=True)

# Get SQL connection string from environment variable and create SQLAlchemy engine
sql_connection_string = (getenv("SQL_CONNECTION_STRING") or "").strip().strip('"').strip("'")
if not sql_connection_string:
    raise RuntimeError(
        f"Missing SQL_CONNECTION_STRING. Add it to environment variables or {ENV_PATH}."
    )

# Accept either a SQLAlchemy URL or a raw ODBC connection string (Driver=...;Server=...;...)
if "://" in sql_connection_string:
    engine = create_engine(sql_connection_string)
else:
    odbc_connect = quote_plus(sql_connection_string)
    engine = create_engine(f"mssql+pyodbc:///?odbc_connect={odbc_connect}")

# Connect to ArcGIS Online with system credentials (make sure to login to ArcGIS Online with an admin account before running)
gis = GIS("home")
print(f'connected to ArcGIS online as {gis.users.me.username}')

# Create reports directory if it doesn't exist
os.makedirs(os.path.join(SCRIPT_DIR, 'reports'), exist_ok=True)


# FUNCTIONS
########################################################################################################################

# HELPER FUNCTIONS FOR SQL OPERATIONS

def quote_sql_identifier(name):
    """Safely quote SQL Server identifiers with brackets."""
    return f"[{name.replace(']', ']]')}]"


def get_table_columns(connection, table_name):
    """Get table columns in ordinal order."""
    rows = connection.execute(
        text(
            """
            SELECT COLUMN_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_NAME = :table_name
            ORDER BY ORDINAL_POSITION
            """
        ),
        {"table_name": table_name},
    ).fetchall()
    return [row[0] for row in rows]


def insert_by_matching_columns(connection, destination_table, source_table):
    """Insert rows by matching column names, not positional order."""
    source_columns = get_table_columns(connection, source_table)
    destination_columns = set(get_table_columns(connection, destination_table))
    shared_columns = [col for col in source_columns if col in destination_columns]

    if not shared_columns:
        raise RuntimeError(f"No shared columns between {source_table} and {destination_table}.")

    columns_sql = ", ".join(quote_sql_identifier(col) for col in shared_columns)
    destination_sql = quote_sql_identifier(destination_table)
    source_sql = quote_sql_identifier(source_table)
    connection.execute(
        text(f"INSERT INTO {destination_sql} ({columns_sql}) SELECT {columns_sql} FROM {source_sql}")
    )

def get_report_sql_dtypes(report_name):
    """Function to define and apply SQL table data types for AGOL reports"""
    if 'OrganizationItems' in report_name:
        return {
            'Title': NVARCHAR(255),
            'Item ID': NVARCHAR(255),
            'Item Url': NVARCHAR(255),
            'Item Type': NVARCHAR(255),
            'Date Created': DATETIME(),
            'Date Modified' : DATETIME(),
            'Content Category' : NVARCHAR(),
            'View Counts' : Float(),
            'Owner' : NVARCHAR(255),
            'File Storage Size': Float(),
            'Feature Storage Size' : Float(),
            'Share Level' : NVARCHAR(255),
            '# of Groups shared with' : Float(),
            'Tags' : NVARCHAR(),
            'Number of Comments': Float(),
            'Is Hosted Service' : BIT(),
            'Date Last Viewed' : DATETIME(),
            'In Recycle Bin' : NVARCHAR(255),
            'updated_date': DATETIME()
        }
    elif 'OrganizationMembers' in report_name:
        return {
            'Username': NVARCHAR(255),
            'Name': NVARCHAR(255),
            'Email': NVARCHAR(255),
            'Profile Visibility': NVARCHAR(255),
            'My Esri Access': NVARCHAR(255),
            'UserType': NVARCHAR(255),
            'Role': NVARCHAR(255),
            'Available Credts': Float(),
            'Assigned Credits': Float(),
            'Last Login Date': DATETIME(),
            'Date Created' : DATETIME(),
            'Add-On Apps' : NVARCHAR(),
            '# of Items Owned' : Float(),
            '# of Groups Owned' : Float(),
            '# of Groups Total' : Float(),
            'Login Type' : NVARCHAR(255),
            'Member Account Status' : NVARCHAR(255),
            'Verified Email Status' : BIT(),
            'Multifactor Authentication Exempt' : NVARCHAR(255),
            'Member Categories' : NVARCHAR(),
            'Multifactor Authentication' : NVARCHAR(255),
            'updated_date' : DATETIME()
        }
    elif 'EntraID_Status' in report_name:
        return {
            'Username': NVARCHAR(255),
            'Email' : NVARCHAR(255),
            'Name' : NVARCHAR(255),
            'EntraID_Status': BIT(),
            'ManagerEmail' : NVARCHAR(255),
            'Groups': NVARCHAR(),
            'updated_date': DATETIME(),
            'WorkingEmail' : NVARCHAR(255),
            'EmailsTried' : NVARCHAR(),
            'UserDepartment' : NVARCHAR(255),
            'ManagerDepartment' : NVARCHAR(255)
        }

def preprocess_dataframe_for_sql(df, dtype_map):
    """Convert datetime and numeric columns to proper types before SQL upload."""
    df = df.copy()
    for col, dtype_obj in dtype_map.items():
        if col in df.columns:
            dtype_str = str(dtype_obj)
            if 'DATETIME' in dtype_str or 'DATE' in dtype_str:
                df[col] = pd.to_datetime(df[col], errors='coerce')
            elif 'FLOAT' in dtype_str or 'Float' in dtype_str:
                df[col] = pd.to_numeric(df[col], errors='coerce')
            elif 'BIT' in dtype_str:
                df[col] = df[col].astype('bool', errors='ignore') if df[col].dtype != 'bool' else df[col]
    return df

# MAIN FUNCTIONS

def fetch_reports(out_dir):
    """Fetches reports from ArcGIS Online, saves them as CSV's, and returns them as a pandas DataFrame."""
    print("fetching AGOL item and member reports...")

    # Get the newest org item & member reports, generated on a schedule by AGOL. Sort on the server:
    # the search used to fetch 100 results in no fixed order and pick the newest of those, and with
    # more than 100 reports in the org it picked the 29 September report on 2 October.
    items = gis.content.search('title:"OrganizationItems_"', max_items=5, sort_field='created', sort_order='desc')
    members = gis.content.search('title:"OrganizationMembers_"', max_items=5, sort_field='created', sort_order='desc')
    if not items or not members:
        raise RuntimeError("No OrganizationItems_ or OrganizationMembers_ report found in AGOL.")

    item_report = sorted(items, key=lambda x: x.created, reverse=True)[0]
    member_report = sorted(members, key=lambda x: x.created, reverse=True)[0]
    for report in (item_report, member_report):
        age_days = (datetime.datetime.now() - datetime.datetime.fromtimestamp(report.created / 1000)).days
        if age_days > 7:
            print(f"WARNING: newest {report.title} is {age_days} days old. Check the scheduled report in AGOL.")

    print(f'found item report: {item_report.title} created on {item_report.created}'
          f'\nfound member report: {member_report.title} created on {member_report.created}')

    # Download the report data and convert to DataFrame
    item_report_df = pd.read_csv(item_report.download())
    member_report_df = pd.read_csv(member_report.download())

    # Add dates to each row in the DataFrames based on the report creation date
    item_report_df['updated_date'] = CURRENT_DATE
    member_report_df['updated_date'] = CURRENT_DATE

    # Save the DataFrames as CSV files
    item_report_title = item_report.title.replace("/", "_").replace("-", "_")
    member_report_title = member_report.title.replace("/", "_").replace("-", "_")
    # Into this run's folder, so no run ever shares a file name with another.
    item_report_csv_path = os.path.join(out_dir, f'{item_report_title}.csv')
    member_report_csv_path = os.path.join(out_dir, f'{member_report_title}.csv')
    item_report_df.to_csv(item_report_csv_path, index=False)
    member_report_df.to_csv(member_report_csv_path, index=False)
    print(f'saved item report to {item_report_csv_path}\nsaved member report to {member_report_csv_path}')

    return (
        item_report_df,
        member_report_df,
        item_report_csv_path,
        member_report_csv_path,
        item_report_title,
        member_report_title,
    )


def Collect_EntraID_Information(member_report_csv_path, entraid_status_path):
    """Runs TAMU_AGOL_EntraID.py to look up each member of the member report in Entra ID and write
    AGOL_EntraID_Status.csv into the run's folder. It replaced TAMU_AGOL_EntraID.ps1 on 2 October 2026: same output,
    minutes instead of hours, and without the PowerShell version's alias and 1,000-character group
    bugs. It opens a browser for Microsoft sign-in."""

    member_report_csv_path = os.path.abspath(os.path.join(SCRIPT_DIR, member_report_csv_path))

    result = subprocess.Popen(
    [
    sys.executable, '-u',
    os.path.join(SCRIPT_DIR, 'TAMU_AGOL_EntraID.py'),
    '--input_csv_path', member_report_csv_path,
    '--output_csv_path', entraid_status_path,
    ],
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,   # one stream, so a chatty error output can never block the pipe
    text=True
    )

    for line in result.stdout:
        print(line, end='')  # Print to terminal in real-time

    result.wait()

    if result.returncode != 0:
        raise RuntimeError("EntraID lookup failed; see errors above. The database has not been changed.")

    if not os.path.exists(entraid_status_path):
        raise FileNotFoundError(f"Expected EntraID status report was not created: {entraid_status_path}")
    
    # Add date to each row in the csv file
    print("adding updated_date to EntraID status report...")
    entraid_status_df = pd.read_csv(entraid_status_path)
    entraid_status_df['updated_date'] = CURRENT_DATE
    entraid_status_df.to_csv(entraid_status_path, index=False)


def Upload_Tables_to_Database(item_report_df, member_report_df, entraid_status_path, item_report_title, member_report_title):
    """Uploads the item and member report DataFrames to the database."""

    # Preprocess and upload the item report DataFrame
    print("uploading item report to database...")
    item_dtypes = get_report_sql_dtypes(item_report_title)
    item_report_df = preprocess_dataframe_for_sql(item_report_df, item_dtypes)
    item_report_df.to_sql(item_report_title, engine, if_exists='replace', index=False, dtype=item_dtypes)

    # Preprocess and upload the member report DataFrame
    print("uploading member report to database...")
    member_dtypes = get_report_sql_dtypes(member_report_title)
    member_report_df = preprocess_dataframe_for_sql(member_report_df, member_dtypes)
    member_report_df.to_sql(member_report_title, engine, if_exists='replace', index=False, dtype=member_dtypes)

    # Preprocess and upload the EntraID status CSV
    print("uploading EntraID status report to database...")
    entraid_status_df = pd.read_csv(entraid_status_path)
    entraid_dtypes = get_report_sql_dtypes('AGOL_EntraID_Status')
    entraid_status_df = preprocess_dataframe_for_sql(entraid_status_df, entraid_dtypes)
    entraid_status_df.to_sql('AGOL_EntraID_Status', engine, if_exists='replace', index=False, dtype=entraid_dtypes)

def Catalog_and_Cleanup(run):
    """Merges the previous run's report tables into their history tables, then drops the dated tables.
    Nothing is lost: every row is in the history with the dates it was seen, and the run folder that
    loaded each table still holds its CSV. History is append-only since 3 October 2026."""
    print("merging previous report tables into history...")
    pairs = [("OrganizationItems[_]20%", "HIST_OrganizationItems"),
             ("OrganizationMembers[_]20%", "HIST_OrganizationMembers"),
             ("AGOL[_]EntraID[_]Status", "HIST_EntraID_Status")]
    with engine.begin() as connection:
        for pattern, history_table in pairs:
            tables = [r[0] for r in connection.execute(text(f"SELECT name FROM sys.tables WHERE name LIKE '{pattern}' ORDER BY name")).fetchall()]
            for table_name in tables:
                seen = connection.execute(text(f"SELECT CAST(MAX(updated_date) AS DATE) FROM dbo.[{table_name}]")).scalar() or CURRENT_DATE
                extended, added = merge_snapshot_into_history(connection, history_table, table_name, run.run_id, seen)
                run.note(**{f"history_{table_name}": {"history": history_table, "seen": str(seen), "extended": extended, "added": added}})
                connection.execute(text(f"DROP TABLE dbo.[{table_name}]"))

def Archive_Reports_Directory():
    """Moves the previous run's files out of reports/ into reports/archive/<date_time>/ so each run
    starts clean. Nothing is deleted. Until 2 October 2026 this step deleted every file in reports/,
    which on its first run that day took a 7-minute lookup and the July Entra status with it."""
    reports_dir = os.path.join(SCRIPT_DIR, 'reports')
    os.makedirs(reports_dir, exist_ok=True)
    archive_dir = os.path.join(reports_dir, 'archive', datetime.datetime.now().strftime('%Y-%m-%d_%H%M%S'))
    moved = 0
    for filename in os.listdir(reports_dir):
        file_path = os.path.join(reports_dir, filename)
        if filename.lower().endswith('.md') or not os.path.isfile(file_path):
            continue
        os.makedirs(archive_dir, exist_ok=True)
        try:
            shutil.move(file_path, os.path.join(archive_dir, filename))
            moved += 1
        except OSError as e:  # a file another process still has open, such as a running log
            print(f"Left in place (in use): {filename} ({e})")
    print(f"Archived {moved} previous report files to {archive_dir}" if moved else "No previous report files to archive.")

def main(entraid_csv=None):
    """One catalog run. Order matters (2 October 2026): everything new is fetched and looked up FIRST, and
    the database is touched only once it all exists. With entraid_csv, an Entra lookup already made
    (TAMU_AGOL_EntraID.py run on its own, with its sign-in) is used instead of starting a new one."""
    with Run(engine, 'catalog') as run:
        Archive_Reports_Directory()
        item_report_df, member_report_df, item_report_csv_path, member_report_csv_path, item_report_title, member_report_title = fetch_reports(run.dir)
        run.note(item_report=item_report_title, member_report=member_report_title,
                 items=len(item_report_df), members=len(member_report_df))
        entraid_status_path = run.path('AGOL_EntraID_Status.csv')
        if entraid_csv:
            shutil.copy2(entraid_csv, entraid_status_path)
            run.note(entraid_source=f"supplied: {entraid_csv}")
            print(f"using the supplied EntraID lookup {entraid_csv} (copied into the run folder)")
        else:
            Collect_EntraID_Information(member_report_csv_path, entraid_status_path)
            run.note(entraid_source="looked up in this run")
        Catalog_and_Cleanup(run)
        Upload_Tables_to_Database(item_report_df, member_report_df, entraid_status_path, item_report_title, member_report_title)
        print("Catalog script execution complete.")


# EXECUTION
#########################################################################################################################


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch the newest AGOL reports, look members up in Entra, and load the database.")
    parser.add_argument("--entraid-csv", help="use this Entra lookup file instead of running a new lookup")
    main(entraid_csv=parser.parse_args().entraid_csv)






    





