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


def Update_DeleteStatus_Table(entraid_table_name, delete_status_table_name):
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

        # Upload the updated delete status DataFrame back to the database
        updated_delete_status_df.to_sql(delete_status_table_name, con=engine, if_exists='replace', index=False, dtype=deletestatus_sql_datatypes())
    
    return updated_delete_status_df if not new_users_df.empty else delete_status_df



def Calculate_Delete_Status(delete_status_df,entraid_status_df, empty_users):
    """This function calculates the deletion status for each user based on their EntraID status and updated dates, updates the database, and sends notification emails as needed."""

    print ("Calculating delete status for users in DeleteStatus table...")

    whitelisted_groups_df = pd.read_sql(text(f"SELECT * FROM {WHITELISTED_ENTRAID_GROUPS_TABLE_NAME}"), engine)
    whitelisted_group_ids = whitelisted_groups_df['ID'].tolist()

    unflagged_user_count = 0
    flagged_user_count = 0
    deleted_user_count = 0

    for row in delete_status_df.itertuples():

        try:
            user_entraid_info = entraid_status_df[entraid_status_df['Username'] == row.Username]
        except IndexError:
            # If user is not found in the EntraID status table, assume they have already been deleted or are otherwise not active and set their status to 2 (marked for deletion)
            delete_status_df.at[row.Index, 'DeleteStatus'] = 2
            delete_status_df.at[row.Index, 'DeleteDate'] = CURRENT_DATE
            continue

        groups_value = user_entraid_info['Groups'].iloc[0] if not user_entraid_info.empty else None
        if pd.isna(groups_value) or not str(groups_value).strip():
            user_entraid_groups = []
        else:
            user_entraid_groups = [group_id.strip() for group_id in str(groups_value).split(',') if group_id.strip()]
        user_entraid_status = user_entraid_info['EntraID_Status'].iloc[0] if not user_entraid_info.empty else None
        
        # If a user has an override, remove potential flags and move to next user
        if row.Override:
            delete_status_df.at[row.Index, 'DeleteStatus'] = 0
            delete_status_df.at[row.Index, 'FlagDate'] = None
            delete_status_df.at[row.Index, 'DeleteDate'] = None
            unflagged_user_count += 1
            print(f"User {row.Username} has an override enabled. Setting DeleteStatus to 0 and skipping further checks.")

        # Delete all users with 0 items published regardless of EntraID Status, as they can automatically remake their account by logging into ArcGIS Pro.
        elif row.Username in empty_users:
            delete_status_df.at[row.Index, 'DeleteStatus'] = 2
            delete_status_df.at[row.Index, 'FlagDate'] = CURRENT_DATE
            flagged_user_count += 1
            print(f"User {row.Username} has 0 items published. Setting DeleteStatus to 2.")

        # Determine if an unflagged user should be flagged based on EntraID affiliations
        elif row.DeleteStatus == 0:
            if user_entraid_status == 0:
                delete_status_df.at[row.Index, 'DeleteStatus'] = 1
                delete_status_df.at[row.Index, 'FlagDate'] = CURRENT_DATE
                unflagged_user_count += 1
            elif user_entraid_status == 1:
                if any(group_id in whitelisted_group_ids for group_id in user_entraid_groups):
                        delete_status_df.at[row.Index, 'DeleteStatus'] = 0
                        delete_status_df.at[row.Index, 'FlagDate'] = None
                        delete_status_df.at[row.Index, 'DeleteDate'] = None
                        unflagged_user_count += 1
                        continue
                else:
                    delete_status_df.at[row.Index, 'DeleteStatus'] = 1
                    delete_status_df.at[row.Index, 'FlagDate'] = CURRENT_DATE
                    flagged_user_count += 1
        
        # Determine if flagged users should be marked for deletion based on how long they have been flagged
        elif row.DeleteStatus == 1 and row.FlagDate and (CURRENT_DATE - pd.Timestamp(row.FlagDate).date()).days >= 30:
            delete_status_df.at[row.Index, 'DeleteStatus'] = 2
            delete_status_df.at[row.Index, 'DeleteDate'] = CURRENT_DATE
            deleted_user_count += 1
        else:
            unflagged_user_count += 1 if row.DeleteStatus == 0 else 0
            flagged_user_count += 1 if row.DeleteStatus == 1 else 0
            deleted_user_count += 1 if row.DeleteStatus == 2 else 0

    print(f"Unflagged users: {unflagged_user_count}")
    print(f"Flagged users: {flagged_user_count}")
    print(f"Users to delete: {deleted_user_count}")

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


def main():
    entraid_table_name = collect_entraid_table_name()
    delete_status_df = Update_DeleteStatus_Table(entraid_table_name, DELETE_STATUS_TABLE_NAME)
    entraid_status_df = pd.read_sql(text(f"SELECT * FROM AGOL_EntraID_Status"), engine)

    Calculate_Delete_Status(delete_status_df,entraid_status_df)
    Delete_Users(delete_status_df)

    


# MAIN EXECUTION
###############################################################################################


if __name__ == "__main__":
    main()
