# File Name: TAMU_AGOL_DeleteEmptyUsers.py
# Decription: This script accesses the AGOL catalog database, finds all users that have been tagged for deletion (DeleteStatus = 2)
#             or have 0 items published, and compiles a list of users to delete. The list is saved as a CSV in the reports directory.
# Author: Dalton Peterson
# Date: 2026-04-02

from urllib.parse import quote_plus

import pandas as pd
from arcgis.gis import GIS
from sqlalchemy import create_engine
from sqlalchemy import text
from dotenv import load_dotenv
import datetime
import os
from os import getenv

# GLOBAL VARIABLES & INITIALIZATION
############################################################################################

CURRENT_DATE = datetime.datetime.now().date()
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, '.env')

DELETE_STATUS_TABLE_NAME = getenv("DELETE_STATUS_TABLE_NAME")

# Connect to SQL Server using SQLALchemy
load_dotenv()
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

# Connect to AGOL using user credentials
gis = GIS("home")
print(f'connected to ArcGIS online as {gis.users.me.username}')


# FUNCTIONS
#############################################################################################


def collect_table_names():
    """This function collects the names of the most recent member and item report tables from the DB.
    Returns:
        member_table_name (str): The name of the most recent member report table.
        item_table_name (str): The name of the most recent item report table.
    """
    with engine.connect() as connection:
        cursor_item = connection.execute(text("SELECT name FROM sys.tables WHERE name LIKE 'OrganizationItems_%'"))
        previous_item_reports = cursor_item.fetchall()
        
        cursor_member = connection.execute(text("SELECT name FROM sys.tables WHERE name LIKE 'OrganizationMembers_%'"))
        previous_member_reports = cursor_member.fetchall()

    # Return the most recent table names
    member_table_name = previous_member_reports[-1][0] if previous_member_reports else None
    item_table_name = previous_item_reports[-1][0] if previous_item_reports else None

    return member_table_name, item_table_name


def get_users_tagged_for_deletion():
    """This function queries the AGOL catalog database to find all users that have been tagged for deletion (DeleteStatus = 2).
    Returns:
        users_to_delete (list): A list of usernames that have been tagged for deletion.
    """
    print('Querying database for users tagged for deletion...')

    query = f"""
    SELECT Username
    FROM {DELETE_STATUS_TABLE_NAME}
    WHERE DeleteStatus = 2
    """
    with engine.connect() as connection:
        result = connection.execute(text(query))
        users_to_delete = [row[0] for row in result.fetchall()]

    print(f'Found {len(users_to_delete)} users tagged for deletion.')
    
    return users_to_delete


def main():
    member_table_name, item_table_name = collect_table_names()
    users_tagged_for_deletion = get_users_tagged_for_deletion()

    # Combine the two lists and remove duplicates
    users_to_delete = set(users_tagged_for_deletion)

    reports_dir = os.path.join(SCRIPT_DIR, 'reports')
    
    users_to_delete_df = pd.DataFrame(sorted(users_to_delete), columns=['Username'])
    users_to_delete_csv = os.path.join(reports_dir, f'UsersToDelete_{CURRENT_DATE.strftime("%Y_%m_%d")}.csv')
    users_to_delete_df.to_csv(users_to_delete_csv, index=False)

    print(f'Found {len(users_to_delete)} users to delete.')
    print(f'Saved users-to-delete list to {users_to_delete_csv}')



# MAIN EXECUTION
##############################################################################################


if __name__ == "__main__":
    main()