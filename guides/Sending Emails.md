# Managing Sent AGOL emails

Author: Dalton Peterson '26

Last updated: April 2026

## Background

When users are flagged for deletion, determined to be over the specified quota of feature storage, or are ready to be deleted, the user needs to be contacted in case they need to migrate their items or if there was an error in determining their affiliation. The script is designed to not send emails automatically when DeleteStatus is calculated and instead send emails when a user specifies after all the main scripts run sequentially.


## Notes

- Emails should be sent right after the script runs and not otherwise - to ensure that users recieve the correct email and don't get duplicates


## I. Sending Emails

1) Ensure the variable "DRY_RUN" is set to False in the .env file

    - DRY_RUN is designed to be used for testing the logic of the script without actually sending emails (i.e. determine who is going to get which email). If it is set to True, no emails will be sent.

2) Run TAMU_AGOL_Main.py

    - This ensures that the DeleteStatus field and number of items published per user are calculated based on the most recently generated AGOL report and that their EntraID affiliations are up-to-date.

3) Run /emails/TAMU_AGOL_EmailUsers.py

    - This will email everyone who has been deleted, will be deleted, and is over quota with the correct messages.


## II. Configuring Email Messages

4) Edit email templates in /emails/email_templates

    - Scripts import these files to use as templates to generate email text. Make sure when editing to keep variable names (defined in curly brackets, e.g. {recipient_username}) consistent with the variable names used in the script to make sure they are applied correctly.