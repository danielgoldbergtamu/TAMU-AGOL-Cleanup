# File Name: TAMU_AGOL_DeleteStatus.py
# Decription: This script accesses the AGOL database and calculates which items should be flagged and deleted
#             based on their EntraID status and updated dates. When a user is flagged for deletion, their 
#             on-file email is contacted as well as their supervisor if applicable.
#             Delete Statuses:
#             0 - No action needed
#             1 - User is flagged for deletion, email sent to user & supervisor
#             2 - User has been flagged for 30 days, user & content should be deleted and email sent to user & supervisor
# Author: Dalton Peterson
# Date: 2026-04-02


from urllib.parse import quote_plus

import pandas as pd

from sqlalchemy import create_engine, text
from sqlalchemy.dialects.mssql import NVARCHAR, DATETIME, BIT, INTEGER

import datetime
import os

from os import getenv
from dotenv import load_dotenv

from TAMU_AGOL_UserQuotas import collect_table_names


# GLOBAL VARIABLES & INITIALIZATION
############################################################################################

CURRENT_DATE = datetime.datetime.now().date()
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, '.env')

load_dotenv(dotenv_path=ENV_PATH, override=True)


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


SENDER_EMAIL = getenv("SENDER_EMAIL")
SENDER_PASSWORD = getenv("SENDER_PASSWORD") 

SMTP_PORT = getenv("SMTP_PORT")
SMTP_SERVER = getenv("SMTP_SERVER")

DELETE_STATUS_TABLE_NAME = getenv("DELETE_STATUS_TABLE_NAME")
WHITELISTED_ENTRAID_GROUPS_TABLE_NAME = getenv("WHITELISTED_ENTRAID_GROUPS_TABLE_NAME")


# FUNCTIONS
#############################################################################################


# HELPER FUNCTIONS


def deletestatus_sql_datatypes():
    """This function defines the SQL data types for the DeleteStatus table."""
    return {
        'Username': NVARCHAR(255),
        'Name': NVARCHAR(255),
        'WorkingEmail': NVARCHAR(255),
        'ManagerEmail': NVARCHAR(255),
        'DeleteStatus': INTEGER,
        'FlagDate': DATETIME,
        'DeleteDate': DATETIME,
        'Override': BIT,
        'updated_date': DATETIME
    }


def collect_entraid_table_name():
    """This function collects the name of the most recent EntraID status table from the DB.
    Returns:
        entraid_table_name (str): The name of the most recent EntraID status table.
    """
    with engine.connect() as connection:
        cursor_entraid = connection.execute(text("SELECT name FROM sys.tables WHERE name LIKE 'AGOL_EntraID_Status%'"))
        previous_entraid_reports = cursor_entraid.fetchall()

    # Return the most recent EntraID table name
    entraid_table_name = previous_entraid_reports[-1][0] if previous_entraid_reports else None

    return entraid_table_name

def get_empty_users(member_table_name, item_table_name):
    """This function queries the AGOL catalog database to find all users that have 0 items published.
    Returns:
        empty_users (list): A list of usernames that have 0 items published.
    """
    print('Querying database for users with 0 items published...')

    query = f"""
    SELECT m.Username
    FROM {member_table_name} m
    LEFT JOIN {item_table_name} i ON m.Username = i.Owner
    WHERE i.[Item ID] IS NULL
    """
    with engine.connect() as connection:
        result = connection.execute(text(query))
        empty_users = [row[0] for row in result.fetchall()]
    
    print(f'Found {len(empty_users)} users with 0 items published.')

    return empty_users


# MAIN FUNCTIONS


def Update_DeleteStatus_Table(entraid_table_name, delete_status_table_name, dry_run=False):
    """This function retrieves the DeleteStatus and Current OrganizationMembers tables from the database. If there is a member in the OrganizationMembers table that is not in the DeleteStatus table, they are added with a DeleteStatus of 0. This function serves to add newly created accounts to the DeleteStatus table so that they can be monitored for deletion if needed."""

    print ("Updating DeleteStatus table with any new users...")

    with engine.connect() as connection:
        delete_status_df = pd.read_sql(text(f"SELECT * FROM {delete_status_table_name}"), connection)
        entraid_df = pd.read_sql(text(f"SELECT * FROM {entraid_table_name}"), connection)

    # Identify new members that are not in the delete status table
    new_users_df = entraid_df[~entraid_df['Username'].isin(delete_status_df['Username'])]
    print(f"Found {len(new_users_df)} new users that are not in the DeleteStatus table.")

    # If there are new users, add them to the delete status table with a status of 0
    if not new_users_df.empty:
        print ("Adding new users to DeleteStatus table with a status of 0...")
        new_delete_status_entries = new_users_df[['Username', 'Name', 'WorkingEmail', 'ManagerEmail']].copy()
        new_delete_status_entries['DeleteStatus'] = 0
        new_delete_status_entries['FlagDate'] = None
        new_delete_status_entries['DeleteDate'] = None
        new_delete_status_entries['Override'] = False
        new_delete_status_entries['updated_date'] = CURRENT_DATE

        # Append the new entries to the existing delete status DataFrame
        updated_delete_status_df = pd.concat([delete_status_df, new_delete_status_entries], ignore_index=True)

        # Upload the updated delete status DataFrame back to the database (not in a dry run)
        if not dry_run:
            updated_delete_status_df.to_sql(delete_status_table_name, con=engine, if_exists='replace', index=False, dtype=deletestatus_sql_datatypes())
    
    return updated_delete_status_df if not new_users_df.empty else delete_status_df



def Calculate_Delete_Status(delete_status_df, entraid_status_df, empty_users, dry_run=False):
    """Recalculates every member's DeleteStatus from the latest EntraID lookup and writes it back.

    Rules, applied to every member on every run (2 October 2026 rewrite, register AGOL-024):
      - Override set                         -> 0
      - owns nothing (empty account)         -> 2; FlagDate kept from the first time
      - found in a current-affiliation group -> 0, at ANY earlier status. Before the rewrite only
        status-0 members were checked, so a wrong flag could never clear and a flagged member moved
        to 2 after 30 days whatever Entra said by then.
      - no row in the lookup at all          -> unchanged. Missing data is not evidence of leaving.
      - otherwise (not current): 0 -> 1 (flagged); 1 for 30+ days -> 2; else unchanged.

    Every change is written to reports/DeleteStatus_changes_<date>.csv. The run refuses to save if
    it would newly flag more than MAX_NEW_FLAG_FRACTION of the currently unflagged members (default
    0.05), which in practice means the lookup is broken. With dry_run, nothing is saved.
    """

    print ("Calculating delete status for users in DeleteStatus table...")

    whitelisted_groups_df = pd.read_sql(text(f"SELECT * FROM {WHITELISTED_ENTRAID_GROUPS_TABLE_NAME}"), engine)
    whitelisted_group_ids = {str(g).strip().lower() for g in whitelisted_groups_df['ID']}

    lookup = {}
    for r in entraid_status_df.itertuples():
        groups = {g.strip().lower() for g in str(r.Groups or "").split(',') if g.strip()} if not pd.isna(r.Groups) else set()
        lookup[str(r.Username).strip().lower()] = (r.EntraID_Status, groups)
    empty_users = {str(u).strip().lower() for u in empty_users}

    changes = []
    unflagged_before = max(1, int((delete_status_df['DeleteStatus'] == 0).sum()))

    def set_status(index, row, new_status, reason, flag_date="keep", delete_date="keep"):
        if row.DeleteStatus != new_status:
            changes.append({"Username": row.Username, "From": row.DeleteStatus, "To": new_status, "Reason": reason})
        delete_status_df.at[index, 'DeleteStatus'] = new_status
        if flag_date != "keep":
            delete_status_df.at[index, 'FlagDate'] = flag_date
        if delete_date != "keep":
            delete_status_df.at[index, 'DeleteDate'] = delete_date

    for row in delete_status_df.itertuples():
        username = str(row.Username).strip().lower()
        entra = lookup.get(username)

        if row.Override:
            set_status(row.Index, row, 0, "override", None, None)

        # Empty accounts are removed whatever their affiliation: they hold nothing, and an SSO
        # account is recreated at its next login.
        elif username in empty_users:
            first_flag = row.FlagDate if row.DeleteStatus == 2 and not pd.isna(row.FlagDate) else CURRENT_DATE
            set_status(row.Index, row, 2, "owns nothing", first_flag)

        elif entra is None:
            continue

        elif entra[0] == 1 and entra[1] & whitelisted_group_ids:
            set_status(row.Index, row, 0, "current affiliation", None, None)

        elif row.DeleteStatus == 0:
            set_status(row.Index, row, 1, "not found in Entra" if entra[0] == 0 else "no current-affiliation group",
                       CURRENT_DATE)

        elif row.DeleteStatus == 1 and not pd.isna(row.FlagDate) and (CURRENT_DATE - pd.Timestamp(row.FlagDate).date()).days >= 30:
            set_status(row.Index, row, 2, "flagged 30+ days", delete_date=CURRENT_DATE)

    counts = delete_status_df['DeleteStatus'].value_counts().to_dict()
    moved = pd.DataFrame(changes, columns=["Username", "From", "To", "Reason"])
    print(f"Status now: 0 = {counts.get(0, 0)}, 1 = {counts.get(1, 0)}, 2 = {counts.get(2, 0)}")
    if not moved.empty:
        print("Changes this run:")
        print(moved.groupby(["From", "To", "Reason"]).size().to_string())

    reports_dir = os.path.join(SCRIPT_DIR, 'reports')
    os.makedirs(reports_dir, exist_ok=True)
    changes_path = os.path.join(reports_dir, f"DeleteStatus_changes_{CURRENT_DATE.strftime('%Y_%m_%d')}{'_dryrun' if dry_run else ''}.csv")
    moved.to_csv(changes_path, index=False)
    print(f"Wrote {len(moved)} changes to {changes_path}")

    max_fraction = float(getenv("MAX_NEW_FLAG_FRACTION") or 0.05)
    newly_flagged = sum(1 for c in changes if c["From"] == 0 and c["To"] == 1)
    if newly_flagged > max_fraction * unflagged_before:
        raise RuntimeError(
            f"Refusing to save: {newly_flagged} of {unflagged_before} unflagged members would be newly flagged, "
            f"more than {max_fraction:.0%}. Check the EntraID lookup before trusting it. Nothing was written.")

    if dry_run:
        print("Dry run: DeleteStatus table NOT updated.")
        return

    # Upload the updated delete status DataFrame back to the database
    delete_status_df.to_sql(DELETE_STATUS_TABLE_NAME, con=engine, if_exists='replace', index=False, dtype=deletestatus_sql_datatypes())


def Delete_Users(delete_status_df):
    """This function deletes users that have been marked for deletion for over 30 days."""

    print ("Deleting users that have been marked for deletion for over 30 days...")

    users_to_delete = delete_status_df[delete_status_df['DeleteStatus'] == 2]

    delete_count = 0

    for row in users_to_delete.itertuples():
        print (f"Deleting user {row.Username}")
        delete_count += 1
        # delete_user(row.Username)

    print(f"Deleted {delete_count} users.")


def Archive_DeleteStatus():
    """Copies the current DeleteStatus table into HIST_DeleteStatus, stamped with today's date, before
    this run replaces it. Until 2 October 2026 every run overwrote the table with no copy kept, so a
    run on a bad lookup could not be undone or even seen afterwards."""
    with engine.begin() as connection:
        if connection.execute(text(f"SELECT OBJECT_ID('{DELETE_STATUS_TABLE_NAME}')")).scalar() is None:
            return
        columns = [r[0] for r in connection.execute(text(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = :t ORDER BY ORDINAL_POSITION"),
            {"t": DELETE_STATUS_TABLE_NAME})]
        column_sql = ", ".join(f"[{c}]" for c in columns)
        if connection.execute(text("SELECT OBJECT_ID('HIST_DeleteStatus')")).scalar() is None:
            connection.execute(text(f"SELECT {column_sql}, CAST(GETDATE() AS DATE) AS archived_date "
                                    f"INTO HIST_DeleteStatus FROM {DELETE_STATUS_TABLE_NAME}"))
        else:
            connection.execute(text(f"INSERT INTO HIST_DeleteStatus ({column_sql}, archived_date) "
                                    f"SELECT {column_sql}, CAST(GETDATE() AS DATE) FROM {DELETE_STATUS_TABLE_NAME}"))
    print("Archived the current DeleteStatus table to HIST_DeleteStatus.")


def main(dry_run=False):
    if not dry_run:
        Archive_DeleteStatus()
    entraid_table_name = collect_entraid_table_name()

    delete_status_df = Update_DeleteStatus_Table(entraid_table_name, DELETE_STATUS_TABLE_NAME, dry_run)
    entraid_status_df = pd.read_sql(text(f"SELECT * FROM AGOL_EntraID_Status"), engine)

    member_table_name, item_table_name = collect_table_names()
    Calculate_Delete_Status(delete_status_df, entraid_status_df, get_empty_users(member_table_name, item_table_name), dry_run)
    # Delete_Users(delete_status_df)

    


# MAIN EXECUTION
###############################################################################################


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Recalculate DeleteStatus from the latest EntraID lookup.")
    parser.add_argument("--dry-run", action="store_true", help="report changes without writing to the database")
    main(dry_run=parser.parse_args().dry_run)
