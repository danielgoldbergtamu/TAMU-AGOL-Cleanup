# File Name: TAMU_AGOL_EmailUsers.py
# Decription: This script accesses the AGOL catalog database, identifies users that have been flagged for future deletion 
#             (DeleteStatus = 1), users that will be deleted (DeleteStatus = 2), and current users that are over the 
#             specified storage quota. It then sends an email to each user (and their manager if applicable) to notify them 
#             of their status and next steps. This script is intended to be run after the TAMU_AGOL_DeleteStatus.py and 
#             TAMU_AGOL_UserQuotas.py scripts have been run, which will update the database with the appropriate DeleteStatus 
#             values and calculate storage usage for each user.
# Author: Dalton Peterson
# Date: 2026-04-23

from urllib.parse import quote_plus

import pandas as pd

from sqlalchemy import create_engine, text, Float
from sqlalchemy.dialects.mssql import NVARCHAR, DATETIME, BIT, INTEGER

import datetime
import os
from pathlib import Path

from os import getenv
from dotenv import load_dotenv

import smtplib
from email.mime.text import MIMEText



# GLOBAL VARIABLES & INITIALIZATION
############################################################################################


CURRENT_DATE = datetime.datetime.now().date()
SCRIPT_DIR = Path(__file__).resolve().parent
ENV_PATH = SCRIPT_DIR.parent / ".env"

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

USER_STORAGE_QUOTA_MB = int(getenv("USER_STORAGE_QUOTA_MB"))

# Dry Run mode to test logic without sending emails. To activate, set DRY_RUN to True in the .env file.

DRY_RUN = (getenv("DRY_RUN", "") or "").strip().lower() in ("1", "true", "yes", "y", "on")

SEND_EMAILS = all([
    (SENDER_EMAIL or "").strip(),
    (SENDER_PASSWORD or "").strip(),
    (SMTP_SERVER or "").strip(),
    (SMTP_PORT or "").strip()
]) and not DRY_RUN

if SEND_EMAILS:
    SMTP_PORT = int(SMTP_PORT)
else:
    print("SMTP send disabled: running in dry-run/preview mode (no emails will be sent).")


# FUNCTIONS
#############################################################################################

def collect_user_info():
    """This function collects user information from the database to use for emailing flagged and deleted users.

    It also gathers all historical manager email addresses from HIST_EntraIDStatus and stores them in a
    list-valued ``manageremails`` column for each username.
    Returns:
        user_info_df (DataFrame): A DataFrame containing user information for all users in the database.
    """
    with engine.connect() as connection:
        user_query = text(f"""
            SELECT 
                Username,
                Name,
                WorkingEmail,
                DeleteStatus
            FROM {DELETE_STATUS_TABLE_NAME} 
        """)
        user_info_df = pd.read_sql(user_query, connection)

        manager_query = text("""
            SELECT
                Username,
                ManagerEmail
            FROM HIST_EntraID_Status
        """)
        manageremails_df = pd.read_sql(manager_query, connection)

    if not manageremails_df.empty:
        manageremails_df = manageremails_df.dropna(subset=["ManagerEmail"])
        manageremails_df["ManagerEmail"] = manageremails_df["ManagerEmail"].astype(str).str.strip()
        manageremails_df = manageremails_df[manageremails_df["ManagerEmail"] != ""]

        manageremails_df = (
            manageremails_df
            .groupby("Username", as_index=False)["ManagerEmail"]
            .agg(lambda values: list(dict.fromkeys(values)))
            .rename(columns={"ManagerEmail": "manageremails"})
        )
    else:
        manageremails_df = pd.DataFrame(columns=["Username", "manageremails"])

    user_info_df = user_info_df.merge(manageremails_df, on="Username", how="left")
    user_info_df["manageremails"] = user_info_df["manageremails"].apply(
        lambda value: value if isinstance(value, list) else []
    )

    return user_info_df

def Email_Delete_Users(user_info_df):
    """This function emails the user and their manager (if applicable) to notify them that their account has been marked for deletion."""

    with open(os.path.join(SCRIPT_DIR, 'email_templates', 'Delete_EmailTemplate.txt'), 'r') as template_file:
        delete_email_template = template_file.read()

    subject = "Your ArcGIS Online Account Has Been Deleted"

    delete_user_info_df = user_info_df[user_info_df['DeleteStatus'] == 2]

    print(f"Sending deletion notification emails to {len(delete_user_info_df)} users...")

    # DRY_RUN mode to test logic. This block only runs if DRY_RUN is set to True, allowing you to see which emails would be sent without actually sending them. It will print the intended recipient and cc count for each email.
    
    if not SEND_EMAILS:
        for _, row in delete_user_info_df.iterrows():
            recipient_username = row["Username"]
            recipient_email = row["WorkingEmail"]
            recipient_manageremails = row["manageremails"]
            cc_recipients = [email for email in recipient_manageremails if email] if isinstance(recipient_manageremails, list) else []

            if pd.isna(recipient_email) or not str(recipient_email).strip():
                print(f"[DRY RUN] Skipping {recipient_username}: no recipient email on file.")
                continue

            print(f"[DRY RUN] DELETE -> {recipient_username} at {str(recipient_email).strip()} (cc_count={len(cc_recipients)})")
        return

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(SENDER_EMAIL, SENDER_PASSWORD)

        for _, row in delete_user_info_df.iterrows():
            recipient_username = row["Username"]
            recipient_name = row["Name"]
            recipient_email = row["WorkingEmail"]
            if pd.isna(recipient_email) or not str(recipient_email).strip():
                print(f"Skipping {recipient_username}: no recipient email on file.")
                continue

            recipient_email = str(recipient_email).strip()
            recipient_manageremails = row["manageremails"]
            cc_recipients = [email for email in recipient_manageremails if email] if isinstance(recipient_manageremails, list) else []

            print(f"emailing user {recipient_username} to notify them of account deletion...")

            # Construct email content
            body = delete_email_template.format(
                recipient_name=recipient_name,
                recipient_username=recipient_username
                )
            try:
                # Create MIMEText object
                msg = MIMEText(body)
                msg['Subject'] = subject
                msg['From'] = SENDER_EMAIL
                msg['To'] = recipient_email
                if cc_recipients:
                    msg['Cc'] = ", ".join(cc_recipients)

                # Send email using SMTP server
                server.send_message(msg, to_addrs=[recipient_email] + cc_recipients)

                print(f"Email sent to {recipient_username}.")
            except Exception as e:
                print(f"Failed to send email to {recipient_username} at {recipient_email}. Error: {e}")


def Email_Flagged_Users(user_info_df):
    """This function emails the user and their manager (if applicable) to notify them that their account has been marked for deletion."""

    with open(os.path.join(SCRIPT_DIR, 'email_templates', 'Flag_EmailTemplate.txt'), 'r') as template_file:
        flag_email_template = template_file.read()

    subject = "Action Needed: Your ArcGIS Online Account Has Been Flagged for Deletion"

    flag_user_info_df = user_info_df[user_info_df['DeleteStatus'] == 1]

    print(f"Sending flagging notification emails to {len(flag_user_info_df)} users...")

    if not SEND_EMAILS:
        for _, row in flag_user_info_df.iterrows():
            recipient_username = row["Username"]
            recipient_email = row["WorkingEmail"]
            recipient_manageremails = row["manageremails"]
            cc_recipients = [email for email in recipient_manageremails if email] if isinstance(recipient_manageremails, list) else []

            if pd.isna(recipient_email) or not str(recipient_email).strip():
                print(f"[DRY RUN] Skipping {recipient_username}: no recipient email on file.")
                continue

            print(f"[DRY RUN] FLAG -> {recipient_username} at {str(recipient_email).strip()} (cc_count={len(cc_recipients)})")
        return

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(SENDER_EMAIL, SENDER_PASSWORD)

        for _, row in flag_user_info_df.iterrows():
            recipient_username = row["Username"]
            recipient_name = row["Name"]
            recipient_email = row["WorkingEmail"]
            if pd.isna(recipient_email) or not str(recipient_email).strip():
                print(f"Skipping {recipient_username}: no recipient email on file.")
                continue

            recipient_email = str(recipient_email).strip()
            recipient_manageremails = row["manageremails"]
            cc_recipients = [email for email in recipient_manageremails if email] if isinstance(recipient_manageremails, list) else []

            print(f"emailing user {recipient_username} to notify them of account flagging...")

            # Construct email content
            body = flag_email_template.format(
                recipient_name=recipient_name,
                recipient_username=recipient_username
                )

            try:
                # Create MIMEText object
                msg = MIMEText(body)
                msg['Subject'] = subject
                msg['From'] = SENDER_EMAIL
                msg['To'] = recipient_email
                if cc_recipients:
                    msg['Cc'] = ", ".join(cc_recipients)

                # Send email using SMTP server
                server.send_message(msg, to_addrs=[recipient_email] + cc_recipients)

                print(f"Email sent to {recipient_username}.")
            except Exception as e:
                print(f"Failed to send email to {recipient_username} at {recipient_email}. Error: {e}")


def Email_Quota_Users(over_quota_users):
    """This function sends an email notification to users that exceed the storage quota, asking them to reduce their storage usage or request more credits.
    Args:
        over_quota_users (DataFrame): A DataFrame containing users that exceed the storage quota, along with their total storage used.
    """
    print(f"Sending email notifications to {len(over_quota_users)} users that exceed the storage quota of {USER_STORAGE_QUOTA_MB} MB...")

    if not SEND_EMAILS:
        for _, row in over_quota_users.iterrows():
            recipient_username = row['Username']
            recipient_email = row['Email']
            if pd.isna(recipient_email) or not str(recipient_email).strip():
                print(f"[DRY RUN] Skipping {recipient_username}: no recipient email on file.")
                continue

            print(f"[DRY RUN] QUOTA -> {recipient_username} at {str(recipient_email).strip()}")
        return


    # Load email template from folder
    with open(os.path.join(SCRIPT_DIR, 'email_templates', 'Quota_EmailTemplate.txt'), 'r') as template_file:
        email_template = template_file.read()

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(SENDER_EMAIL, SENDER_PASSWORD)

        for index, row in over_quota_users.iterrows():
            recipient_username = row['Username']
            recipient_name = row['Name']
            recipient_email = row['Email']
            total_feature_storage_mb = row['total_feature_storage_mb']

            # Construct email content
            subject = "Action Needed: ArcGIS Online Storage Quota Exceeded"
            body = email_template.format(
                recipient_name = recipient_name,
                total_feature_storage_mb = f"{total_feature_storage_mb:.2f}",
                USER_STORAGE_QUOTA_MB = f"{USER_STORAGE_QUOTA_MB:.2f}",
                recipient_username = recipient_username
            )

            try:
                # Create MIMEText object
                msg = MIMEText(body)
                msg['Subject'] = subject
                msg['From'] = SENDER_EMAIL
                msg['To'] = recipient_email

                # Send email using SMTP server
                server.send_message(msg)

                print(f"Email sent to {recipient_username} at {recipient_email} regarding storage quota.")
            except Exception as e:
                print(f"Failed to send email to {recipient_username} at {recipient_email}. Error: {e}")


def main():
    user_info_df = collect_user_info()
    Email_Delete_Users(user_info_df)
    Email_Flagged_Users(user_info_df)

    reports_dir = SCRIPT_DIR.parent / "reports"
    quota_report_files = sorted(reports_dir.glob("UsersOverQuota*.csv"))

    if not quota_report_files:
        raise FileNotFoundError(f"No report files found matching UsersOverQuota*.csv in {reports_dir}")

    first_quota_report = quota_report_files[0]

    Email_Quota_Users(pd.read_csv(first_quota_report))


# MAIN EXECUTION
#############################################################################################


if __name__ == "__main__":
    main()