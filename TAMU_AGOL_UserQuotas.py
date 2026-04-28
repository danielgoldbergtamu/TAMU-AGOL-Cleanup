# File Name: TAMU_AGOL_UserQuotas.py
# Description: This script accesses the AGOL catalog database, calculates all users that have a total amount of feature
#             storage that exceeds the specified quota (based on assigned AGOL credits), and determines their account's
#             deletion status and sends an email to ask them to remove content or request more credits.
# Author: Dalton Peterson
# Date: 2026-04-02

from urllib.parse import quote_plus
from sqlalchemy import create_engine
from sqlalchemy import text

import pandas as pd

from os import getenv
from dotenv import load_dotenv

import datetime
import os

from arcgis.gis import GIS


# GLOBAL VARIABLES & INITIALIZATION
############################################################################################

load_dotenv()

CURRENT_DATE = datetime.datetime.now().date()
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, '.env')

DELETE_STATUS_TABLE_NAME = os.getenv("DELETE_STATUS_TABLE_NAME")
USER_STORAGE_QUOTA_MB = int(os.getenv("USER_STORAGE_QUOTA_MB"))

SMTP_SERVER = os.getenv("SMTP_SERVER")
SMTP_PORT = os.getenv("SMTP_PORT")

SENDER_EMAIL = os.getenv("SENDER_EMAIL")
SENDER_PASSWORD = os.getenv("SENDER_PASSWORD")


# Connect to SQL Server using SQLALchemy
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
    print("Collecting the names of the most recent member and item report tables from the database...")
    with engine.connect() as connection:
        cursor_item = connection.execute(text("SELECT name FROM sys.tables WHERE name LIKE 'OrganizationItems_%'"))
        previous_item_reports = cursor_item.fetchall()
        
        cursor_member = connection.execute(text("SELECT name FROM sys.tables WHERE name LIKE 'OrganizationMembers_%'"))
        previous_member_reports = cursor_member.fetchall()

    # Return the most recent table names
    member_table_name = previous_member_reports[-1][0] if previous_member_reports else None
    item_table_name = previous_item_reports[-1][0] if previous_item_reports else None

    return member_table_name, item_table_name


def calculate_user_storage_quota(member_table_name, item_table_name):
    """This function calculates the total storage used by each user and identifies those that exceed the specified quota.
    Args:
        member_table_name (str): The name of the member report table to query for user information.
        item_table_name (str): The name of the item report table to query for item information.
        entraid_table_name (str): The name of the EntraID groups table to query for group information.
    Returns:
        over_quota_users (DataFrame): A DataFrame containing users that exceed the storage quota, along with their total storage used.
    """

    # Query to calculate total storage used by each user
    print(f"Calculating total storage used by each user and identifying those that exceed the quota of {USER_STORAGE_QUOTA_MB} MB...")
    query = f"""
    SELECT 
        m.Username,
        m.Email,
        m.Name,
        SUM(i.[Feature Storage Size]) AS total_feature_storage_mb
    FROM {member_table_name} m
    JOIN {item_table_name} i ON m.Username = i.Owner
    GROUP BY m.Username, m.Email, m.Name
    HAVING SUM(i.[Feature Storage Size]) > {USER_STORAGE_QUOTA_MB}
    """

    with engine.connect() as connection:
        result = connection.execute(text(query))
        over_quota_users = pd.DataFrame(result.fetchall(), columns=result.keys())

    print(f"Found {len(over_quota_users)} users that exceed the storage quota.")
    return over_quota_users


def main():
    member_table_name, item_table_name = collect_table_names()
    over_quota_users = calculate_user_storage_quota(member_table_name, item_table_name)

    reports_dir = os.path.join(SCRIPT_DIR, 'reports')
    print(f"Saving report to {reports_dir}")
    over_quota_df = over_quota_users.sort_values(by='total_feature_storage_mb', ascending=False)
    over_quota_csv = os.path.join(reports_dir, f'UsersOverQuota_{CURRENT_DATE.strftime("%Y_%m_%d")}.csv')
    over_quota_df.to_csv(over_quota_csv, index=False)

    # email_over_quota_users(over_quota_users)

if __name__ == "__main__":
    main()