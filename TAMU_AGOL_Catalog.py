# Author: Dalton Peterson
# Description: This script downloads arcgis online reports and updates the database with the new information.
# It uses the ArcGIS API for Python to access the reports and pandas to manipulate the data before updating the database.
#
# DuckDB since 20 October 2026 (agol_db.py). The database is opened only once every lookup has finished: the Entra
# lookup runs as a separate process and writes its own history tables, and DuckDB allows one writer at a time.
#
# Signing in to ArcGIS Online: only as the read-only service account (AGOL_USERNAME, AGOL_PASSWORD), checked to
# have view privileges only (agol_db.agol_gis). It only reads ArcGIS Online.

import argparse
import datetime
import os
import shutil
import subprocess
import sys
from os import getenv

import pandas as pd
from dotenv import load_dotenv

from agol_db import agol_gis, connect, reports_dir, tables_like, write
from TAMU_AGOL_Runs import Run, merge_snapshot_into_history


# GLOBAL VARIABLES & INITIALIZATION
########################################################################################################################

CURRENT_DATE = datetime.datetime.now().date()
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, '.env')

# History is append-only (TAMU_AGOL_Runs.merge_snapshot_into_history). The duplicate-removal queries
# that used to live here deleted every history row identical to a newer one; removed 3 October 2026.

load_dotenv(dotenv_path=ENV_PATH, override=True)


# FUNCTIONS
########################################################################################################################

# Column types of the report tables (DuckDB). Anything not listed is text.
REPORT_TYPES = {
    'OrganizationItems': {
        'Date Created': 'TIMESTAMP', 'Date Modified': 'TIMESTAMP', 'View Counts': 'DOUBLE',
        'File Storage Size': 'DOUBLE', 'Feature Storage Size': 'DOUBLE', '# of Groups shared with': 'DOUBLE',
        'Number of Comments': 'DOUBLE', 'Is Hosted Service': 'BOOLEAN', 'Date Last Viewed': 'TIMESTAMP',
        'updated_date': 'TIMESTAMP'},
    'OrganizationMembers': {
        'Available Credits': 'DOUBLE', 'Assigned Credits': 'DOUBLE', 'Last Login Date': 'TIMESTAMP',
        'Date Created': 'TIMESTAMP', '# of Items Owned': 'DOUBLE', '# of Groups Owned': 'DOUBLE',
        '# of Groups Total': 'DOUBLE', 'Verified Email Status': 'BOOLEAN', 'updated_date': 'TIMESTAMP'},
    'EntraID_Status': {'EntraID_Status': 'BOOLEAN', 'updated_date': 'TIMESTAMP'},
}


def report_types(report_name, frame_columns=()):
    """{column: DuckDB type} for every column of a report; text unless listed in REPORT_TYPES (so a column that
    happens to be empty in one report is still text, as the history expects)."""
    listed = next((types for key, types in REPORT_TYPES.items() if key in report_name), {})
    return {c: listed.get(c, 'VARCHAR') for c in frame_columns} or listed


def prepare_frame(df, types):
    """Coerce the report's columns to their types and everything else to text, before writing to DuckDB."""
    df = df.copy()
    for col in df.columns:
        t = types.get(col)
        if t == 'TIMESTAMP':
            df[col] = pd.to_datetime(df[col], errors='coerce')
        elif t == 'DOUBLE':
            df[col] = pd.to_numeric(df[col], errors='coerce')
        elif t == 'BOOLEAN':
            df[col] = df[col].map(lambda v: None if pd.isna(v) else str(v).strip().lower() in ('true', '1', 'yes'))
        else:
            df[col] = df[col].map(lambda v: None if pd.isna(v) else str(v))
    return df


# MAIN FUNCTIONS

def fetch_reports(gis, out_dir):
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

    item_report_df = pd.read_csv(item_report.download(save_path=out_dir))
    member_report_df = pd.read_csv(member_report.download(save_path=out_dir))

    # Add dates to each row in the DataFrames based on the report creation date
    item_report_df['updated_date'] = CURRENT_DATE
    member_report_df['updated_date'] = CURRENT_DATE

    item_report_title = item_report.title.replace("/", "_").replace("-", "_")
    member_report_title = member_report.title.replace("/", "_").replace("-", "_")
    # Into this run's folder, so no run ever shares a file name with another.
    item_report_csv_path = os.path.join(out_dir, f'{item_report_title}.csv')
    member_report_csv_path = os.path.join(out_dir, f'{member_report_title}.csv')
    item_report_df.to_csv(item_report_csv_path, index=False)
    member_report_df.to_csv(member_report_csv_path, index=False)
    print(f'saved item report to {item_report_csv_path}\nsaved member report to {member_report_csv_path}')

    return item_report_df, member_report_df, item_report_csv_path, member_report_csv_path, item_report_title, member_report_title


def Collect_EntraID_Information(member_report_csv_path, entraid_status_path):
    """Runs TAMU_AGOL_EntraID.py to look up each member of the member report in Entra ID and write
    AGOL_EntraID_Status.csv into the run's folder. It replaced TAMU_AGOL_EntraID.ps1 on 2 October 2026: same output,
    minutes instead of hours, and without the PowerShell version's alias and 1,000-character group
    bugs. It opens a browser for Microsoft sign-in."""
    result = subprocess.Popen(
        [sys.executable, '-u', os.path.join(SCRIPT_DIR, 'TAMU_AGOL_EntraID.py'),
         '--input_csv_path', os.path.abspath(member_report_csv_path), '--output_csv_path', entraid_status_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,   # one stream, so a chatty error output can never block the pipe
        text=True)
    for line in result.stdout:
        print(line, end='')  # Print to terminal in real-time
    result.wait()
    if result.returncode != 0:
        raise RuntimeError("EntraID lookup failed; see errors above. The database has not been changed.")
    if not os.path.exists(entraid_status_path):
        raise FileNotFoundError(f"Expected EntraID status report was not created: {entraid_status_path}")

    print("adding updated_date to EntraID status report...")
    entraid_status_df = pd.read_csv(entraid_status_path)
    entraid_status_df['updated_date'] = CURRENT_DATE
    entraid_status_df.to_csv(entraid_status_path, index=False)


def Upload_Tables_to_Database(con, item_report_df, member_report_df, entraid_status_path, item_report_title, member_report_title):
    """Loads the item report, member report and Entra lookup as this run's report tables."""
    print("uploading item report to database...")
    types = report_types(item_report_title, item_report_df.columns)
    write(con, item_report_title, prepare_frame(item_report_df, types), types=types)

    print("uploading member report to database...")
    types = report_types(member_report_title, member_report_df.columns)
    write(con, member_report_title, prepare_frame(member_report_df, types), types=types)

    print("uploading EntraID status report to database...")
    entra = pd.read_csv(entraid_status_path)
    types = report_types('EntraID_Status', entra.columns)
    write(con, 'AGOL_EntraID_Status', prepare_frame(entra, types), types=types)


def Collect_MQS_Information(member_report_csv_path, mqs_status_path):
    """Runs TAMU_AGOL_MQS.py (TAMU directory: department, college, major) for every member, into the
    run's folder. Returns False, and the catalog carries on without it, when the MQS client is not
    configured in .env, so an institution without this API can use the rest of the pipeline."""
    if not (getenv("MQS_CLIENT_ID") and getenv("MQS_SHARED_SECRET")):
        print("MQS_CLIENT_ID / MQS_SHARED_SECRET not set: skipping the directory lookup.")
        return False
    result = subprocess.Popen(
        [sys.executable, '-u', os.path.join(SCRIPT_DIR, 'TAMU_AGOL_MQS.py'),
         '--input_csv_path', os.path.abspath(member_report_csv_path), '--output_csv_path', mqs_status_path],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in result.stdout:
        print(line, end='')
    result.wait()
    if result.returncode != 0 or not os.path.exists(mqs_status_path):
        raise RuntimeError("Directory (MQS) lookup failed; see errors above. The database has not been changed.")
    return True


def Upload_MQS_Table(con, mqs_status_path):
    """Loads a directory lookup as AGOL_MQS_Status. Every column is text except updated_date; the next
    catalog run merges it into HIST_MQS_Status, so a student's college and major are kept after the
    directory stops returning them."""
    print("uploading directory (MQS) status to database...")
    frame = pd.read_csv(mqs_status_path, dtype=str, keep_default_na=False, na_values=[""])
    frame['updated_date'] = pd.to_datetime(frame['updated_date'])
    write(con, 'AGOL_MQS_Status', frame, types={c: ('TIMESTAMP' if c == 'updated_date' else 'VARCHAR') for c in frame.columns})
    return len(frame)


def Catalog_and_Cleanup(con, run):
    """Merges the previous run's report tables into their history tables, then drops the dated tables.
    Nothing is lost: every row is in the history with the dates it was seen, and the run folder that
    loaded each table still holds its CSV. History is append-only since 3 October 2026."""
    print("merging previous report tables into history...")
    pairs = [(r"OrganizationItems_20\d\d_\d\d_\d\d", "HIST_OrganizationItems"),
             (r"OrganizationMembers_20\d\d_\d\d_\d\d", "HIST_OrganizationMembers"),
             (r"AGOL_EntraID_Status", "HIST_EntraID_Status"),
             (r"AGOL_MQS_Status", "HIST_MQS_Status")]
    for pattern, history_table in pairs:
        for table_name in tables_like(con, pattern):
            seen = con.execute(f'SELECT CAST(MAX(updated_date) AS DATE) FROM "{table_name}"').fetchone()[0] or CURRENT_DATE
            extended, added = merge_snapshot_into_history(con, history_table, table_name, run.run_id, seen)
            run.note(**{f"history_{table_name}": {"history": history_table, "seen": str(seen), "extended": extended, "added": added}})
            con.execute(f'DROP TABLE "{table_name}"')


def Archive_Reports_Directory():
    """Moves the previous run's files out of reports/ into reports/archive/<date_time>/ so each run
    starts clean. Nothing is deleted. Until 2 October 2026 this step deleted every file in reports/,
    which on its first run that day took a 7-minute lookup and the July Entra status with it."""
    folder = reports_dir()
    os.makedirs(folder, exist_ok=True)
    archive_dir = os.path.join(folder, 'archive', datetime.datetime.now().strftime('%Y-%m-%d_%H%M%S'))
    moved = 0
    for filename in os.listdir(folder):
        file_path = os.path.join(folder, filename)
        if filename.lower().endswith('.md') or not os.path.isfile(file_path):
            continue
        os.makedirs(archive_dir, exist_ok=True)
        try:
            shutil.move(file_path, os.path.join(archive_dir, filename))
            moved += 1
        except OSError as e:  # a file another process still has open, such as a running log
            print(f"Left in place (in use): {filename} ({e})")
    print(f"Archived {moved} previous report files to {archive_dir}" if moved else "No previous report files to archive.")


def main(entraid_csv=None, mqs_csv=None, members_csv=None, items_csv=None):
    """One catalog run. Order matters (2 October 2026): everything new is fetched and looked up FIRST, and
    the database is touched only once it all exists. With entraid_csv, an Entra lookup already made
    (TAMU_AGOL_EntraID.py run on its own, with its sign-in) is used instead of starting a new one;
    mqs_csv does the same for the directory lookup (TAMU_AGOL_MQS.py). members_csv and items_csv load
    report files already downloaded instead of fetching them (for re-runs and tests)."""
    with Run(None, 'catalog') as run:
        Archive_Reports_Directory()
        if members_csv and items_csv:
            title = lambda p: os.path.splitext(os.path.basename(p))[0].replace("-", "_")
            item_report_df, member_report_df = pd.read_csv(items_csv), pd.read_csv(members_csv)
            item_report_csv_path, member_report_csv_path = items_csv, members_csv
            item_report_title, member_report_title = title(items_csv), title(members_csv)
        else:
            item_report_df, member_report_df, item_report_csv_path, member_report_csv_path, item_report_title, member_report_title = \
                fetch_reports(agol_gis(), run.dir)
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
        mqs_status_path = run.path('MQS_Directory_Status.csv')
        if mqs_csv:
            shutil.copy2(mqs_csv, mqs_status_path)
            have_mqs = True
            run.note(mqs_source=f"supplied: {mqs_csv}")
        else:
            have_mqs = Collect_MQS_Information(member_report_csv_path, mqs_status_path)
            run.note(mqs_source="looked up in this run" if have_mqs else "skipped (not configured)")
        with connect() as con:   # only now, with every lookup finished
            con.execute("BEGIN TRANSACTION")
            Catalog_and_Cleanup(con, run)
            Upload_Tables_to_Database(con, item_report_df, member_report_df, entraid_status_path, item_report_title, member_report_title)
            if have_mqs:
                run.note(mqs_rows=Upload_MQS_Table(con, mqs_status_path))
            con.execute("COMMIT")
        print("Catalog script execution complete.")


# EXECUTION
#########################################################################################################################

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch the newest AGOL reports, look members up in Entra, and load the database.")
    parser.add_argument("--entraid-csv", help="use this Entra lookup file instead of running a new lookup")
    parser.add_argument("--mqs-csv", help="use this directory (MQS) lookup file instead of running a new lookup")
    parser.add_argument("--members-csv", help="load this member report file instead of fetching the newest (with --items-csv)")
    parser.add_argument("--items-csv", help="load this item report file instead of fetching the newest (with --members-csv)")
    arguments = parser.parse_args()
    main(entraid_csv=arguments.entraid_csv, mqs_csv=arguments.mqs_csv, members_csv=arguments.members_csv, items_csv=arguments.items_csv)
