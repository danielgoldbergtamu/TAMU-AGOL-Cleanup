# File Name: TAMU_AGOL_UserQuotas.py
# Description: This script accesses the AGOL catalog database, calculates all users that have a total amount of feature
#             storage that exceeds the specified quota (based on assigned AGOL credits), and determines their account's
#             deletion status and sends an email to ask them to remove content or request more credits.
# Author: Dalton Peterson
# Date: 2026-04-02

import pandas as pd

from os import getenv
from dotenv import load_dotenv

import datetime
import os

from agol_db import connect, newest, q, read


# GLOBAL VARIABLES & INITIALIZATION
############################################################################################

load_dotenv()

CURRENT_DATE = datetime.datetime.now().date()
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, '.env')

DELETE_STATUS_TABLE_NAME = os.getenv("DELETE_STATUS_TABLE_NAME")
USER_STORAGE_QUOTA_MB = int(os.getenv("USER_STORAGE_QUOTA_MB") or 0)

SMTP_SERVER = os.getenv("SMTP_SERVER")
SMTP_PORT = os.getenv("SMTP_PORT")

SENDER_EMAIL = os.getenv("SENDER_EMAIL")
SENDER_PASSWORD = os.getenv("SENDER_PASSWORD")


# FUNCTIONS
#############################################################################################


def collect_table_names(con):
    """Names of the newest member and item report tables in the database."""
    return newest(con, "OrganizationMembers_"), newest(con, "OrganizationItems_")


def calculate_user_storage_quota(con, member_table_name, item_table_name):
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
        SUM(i."Feature Storage Size") AS total_feature_storage_mb
    FROM {q(member_table_name)} m
    JOIN {q(item_table_name)} i ON m.Username = i.Owner
    GROUP BY m.Username, m.Email, m.Name
    HAVING SUM(i."Feature Storage Size") > {USER_STORAGE_QUOTA_MB}
    """
    over_quota_users = read(con, query)

    print(f"Found {len(over_quota_users)} users that exceed the storage quota.")
    return over_quota_users


def main():
    from TAMU_AGOL_Runs import Run
    with connect() as con, Run(con, 'userquotas') as run:
        member_table_name, item_table_name = collect_table_names(con)
        over_quota_users = calculate_user_storage_quota(con, member_table_name, item_table_name)

        over_quota_df = over_quota_users.sort_values(by='total_feature_storage_mb', ascending=False)
        over_quota_csv = run.path('UsersOverQuota.csv')   # this run's folder: never overwrites an earlier list
        over_quota_df.to_csv(over_quota_csv, index=False)
        run.note(users_over_quota=len(over_quota_df))
        print(f"Saved over-quota report to {over_quota_csv}")

    # email_over_quota_users(over_quota_users)

if __name__ == "__main__":
    main()