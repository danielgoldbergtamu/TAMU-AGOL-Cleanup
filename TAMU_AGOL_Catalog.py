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

import datetime
import os


# GLOBAL VARIABLES & INITIALIZATION
########################################################################################################################

CURRENT_DATE = datetime.datetime.now().date()
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, '.env')

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

def fetch_reports():
    """Fetches reports from ArcGIS Online, saves them as CSV's, and returns them as a pandas DataFrame."""
    print("fetching AGOL item and member reports...")

    # Get all org item & member reports. These are generated daily by 
    items = gis.content.search('title:"OrganizationItems_"', max_items=100)
    members = gis.content.search('title:"OrganizationMembers_"', max_items=100)

    # Assume first result is the most recent report (the one you want)
    item_report = sorted(items, key=lambda x: x.created, reverse=True)[0]
    member_report = sorted(members, key=lambda x: x.created, reverse=True)[0]

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
    item_report_df.to_csv(os.path.join(SCRIPT_DIR, 'reports', f'{item_report_title}.csv'), index=False)
    member_report_df.to_csv(os.path.join(SCRIPT_DIR, 'reports', f'{member_report_title}.csv'), index=False)

    print (f'saved item report to ./reports/{item_report_title}.csv'
           f'\nsaved member report to ./reports/{member_report_title}.csv')

    member_report_csv_path = f'./reports/{member_report_title}.csv'
    item_report_csv_path = f'./reports/{item_report_title}.csv'

    return (
        item_report_df,
        member_report_df,
        item_report_csv_path,
        member_report_csv_path,
        item_report_title,
        member_report_title,
    )


def Collect_EntraID_Information(member_report_csv_path):
    """Calls TAMU_AGOL_EntraID.ps1 to collect EntraID information for each user in the member report and write to a CSV."""

    # Run the PowerShell script to collect EntraID information for each user in the member report and write to CSV
    result = subprocess.Popen(
    [
    'powershell', 
    '-ExecutionPolicy', 'Bypass',
    '-File', os.path.join(SCRIPT_DIR, 'TAMU_AGOL_EntraID.ps1'),
    '-input_csv_path', member_report_csv_path,
    ], 
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True
    )

    output_lines = []
    for line in result.stdout:
        print(line, end='')  # Print to terminal in real-time
        output_lines.append(line)

    result.wait()
    stderr_output = result.stderr.read() if result.stderr else ""

    # Now you have both
    if stderr_output:
        print(f"Errors: {stderr_output}")

    if result.returncode != 0:
        print(f"Error executing PowerShell script: {result.stderr}")
    
    # Add date to each row in the csv file
    print("adding updated_date to EntraID status report...")
    entraid_status_df = pd.read_csv(os.path.join(SCRIPT_DIR, 'reports', 'AGOL_EntraID_Status.csv'))
    entraid_status_df['updated_date'] = CURRENT_DATE
    entraid_status_df.to_csv(os.path.join(SCRIPT_DIR, 'reports', 'AGOL_EntraID_Status.csv'), index=False)


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

def Catalog_and_Cleanup():
    "Adds data from previous reports to history tables and deletes old reports from the database."
    print("cataloging and clearing previous reports...")

    # Collect names of previous reports from the database
    with engine.connect() as connection:
        cursor_item = connection.execute(text("SELECT name FROM sys.tables WHERE name LIKE 'OrganizationItems_%'"))
        previous_item_reports = cursor_item.fetchall()
        
        cursor_member = connection.execute(text("SELECT name FROM sys.tables WHERE name LIKE 'OrganizationMembers_%'"))
        previous_member_reports = cursor_member.fetchall()
        
        cursor_entraid = connection.execute(text("SELECT name FROM sys.tables WHERE name = 'AGOL_EntraID_Status'"))
        previous_entraid_status = cursor_entraid.fetchall()

    item_history_table_title = 'HIST_OrganizationItems'
    member_history_table_title = 'HIST_OrganizationMembers'
    entraid_status_history_table_title = 'HIST_EntraID_Status'

    # Add previous reports to history tables and delete if they exist
    print("adding tables to history tables and clearing previous reports...")
    
    with engine.connect() as connection:
        # For item reports
        if previous_item_reports:
            for row in previous_item_reports:
                table_name = row[0]  # Assuming name is the first column
                # Check if history table exists, if not, create it
                check_result = connection.execute(text(f"SELECT OBJECT_ID('{item_history_table_title}')"))
                exists = check_result.fetchone()
                if exists[0] is None:
                    connection.execute(text(f"SELECT * INTO [{item_history_table_title}] FROM [{table_name}]"))
                else:
                    insert_by_matching_columns(connection, item_history_table_title, table_name)
                connection.execute(text(f"DROP TABLE [{table_name}]"))
                connection.commit()  # Commit after each operation
        else:
            print("No previous item reports found in the database.")
        
        # For member reports
        if previous_member_reports:
            for row in previous_member_reports:
                table_name = row[0]
                check_result = connection.execute(text(f"SELECT OBJECT_ID('{member_history_table_title}')"))
                exists = check_result.fetchone()
                if exists[0] is None:
                    connection.execute(text(f"SELECT * INTO [{member_history_table_title}] FROM [{table_name}]"))
                else:
                    insert_by_matching_columns(connection, member_history_table_title, table_name)
                connection.execute(text(f"DROP TABLE [{table_name}]"))
                connection.commit()
        else:
            print("No previous member reports found in the database.")   
        
        # For EntraID status
        if previous_entraid_status:
            table_name = previous_entraid_status[0][0]
            check_result = connection.execute(text(f"SELECT OBJECT_ID('{entraid_status_history_table_title}')"))
            exists = check_result.fetchone()
            if exists[0] is None:
                connection.execute(text(f"SELECT * INTO [{entraid_status_history_table_title}] FROM [{table_name}]"))
            else:
                insert_by_matching_columns(connection, entraid_status_history_table_title, table_name)
            connection.execute(text(f"DROP TABLE [{table_name}]"))
            connection.commit()
        else:
            print("No previous entraID status reports found in the database.")

    # clear reports directory
    print("clearing reports directory...")
    for filename in os.listdir(os.path.join(SCRIPT_DIR, 'reports')):
        if filename.lower().endswith('.md'):
            continue
        file_path = os.path.join(SCRIPT_DIR, 'reports', filename)
        try:
            if os.path.isfile(file_path):
                os.unlink(file_path)
        except Exception as e:
            print(f"Error deleting file {file_path}: {e}")

def main():
    Catalog_and_Cleanup()
    item_report_df, member_report_df, item_report_csv_path, member_report_csv_path, item_report_title, member_report_title = fetch_reports()
    Collect_EntraID_Information(member_report_csv_path)
    Upload_Tables_to_Database(
        item_report_df,
        member_report_df,
        os.path.join(SCRIPT_DIR, 'reports', 'AGOL_EntraID_Status.csv'),
        item_report_title,
        member_report_title,
    )

    print("Catalog script execution complete.")    


# EXECUTION
#########################################################################################################################


if __name__ == "__main__":
    main()






    





