# Texas A&M ArcGIS Online Cleanup

Author: Dalton Peterson - Technician, Texas A&M GIS Help Desk

Last Updated: April 2026

Managed by Texas A&M GIS Help Desk: tx.ag/GISHelpDesk

## Background

This repo represents current efforts to automate the process of managing Texas A&M's ArcGIS Online Enterprise. It includes scripts and configurables to crate a catalog of all users on Texas A&M's AGOL currently and previously. Cataloging, management and quering of users is done using a MS SQL Server Database hosted locally on the device running the scripts.

These scripts take Generated ArcGIS Online Reports, generates a CSV of users to delete based on University affiliation, and also notifies users of excessive storage usage. It is designed to run automatically after each semester at Texas A&M.

To learn more about Texas A&M's ArcGIS Online policies, visit: https://service.tamu.edu/TDClient/36/Portal/KB/ArticleDet?ID=1701


## Dependencies

1) ArcGIS Pro 3.3 with ArcGIS API for Python

2) ArcGIS Online account with admin priviledges 
    - Scripts currently configured to use OS authentication (need to be logged into ArcGIS on device running scripts)

3) MS SQL Server Database
    - Need to create a MS SQL Server and save the connection string to the .env with the following tables created before running scripts:
        - HIST_OrganizationMembers (Schema should mirror generated AGOL Reports with added updated_date column)
        - HIST_OrganizationItems (Schema should mirror generated AGOL Reports with added updated_date column)

4) .env
    - Configure a .env file based on the example provided to include desired table names, storage quotas, MS SQL connection strings, and SMTP configuration for automated emails

5) ArcGIS Online report generation
    - Generate a new report for Organization Items and Members before running the scripts.

6) Microsoft EntraID and Mg Graph Users (PowerShell 7.0+)
    - Texas A&M uses EntraID for identity management, so this script accesses the Mg Graph library in PowerShell 7.0 to document users and their affiliation within the University. This is used to determine whether or not to delete a user.


## Database Tables

1) OrganizationItems_date
    - Mirror of the most recent ArcGIS Item Report

2) OrganizationMembers_date
    - Mirror of the most recent ArcGIS Member Report

3) AGOL_EntraID_Status
    - Documentation on organization affiliations based on the most recently published AGOL Member report

4) HIST_OrganizationItems
    - Catalog of all previous OrganizationItem reports handled by the script. Used to track the history of items in AGOL. This table needs to be created prior to running scripts

5) HIST_OrganizationMembers
    - Catalog of all previous OrganizationMember reports handled by the script. Used to track the history of members in AGOL. This table needs to be created prior to running scripts

6) HIST_EntraID_Status
    - Catalog of all previous EntraID Status reports handled by the script. Used to track the history of AGOL Member affiliations. This table needs to be created prior to running scripts

6) DeleteStatus
    - Catalog of previous and current users with a status field to determine whether or not to delete the user (DeleteStatus).
        - DeleteStatus = 0; User is not flagged to be deleted (current user)
        - DeleteStatus = 1; User is flagged to be deleted and email + supervisor is contacted (user is no longer affiliated with the University)
        - DeleteStatus = 2; User has been flagged to be deleted for a month and will be deleted
    - Name for this table defined in .env

7) Whitelisted_EntraID_Groups
    - List of EntraID security group names and ID's that are "whitelisted" (used to identify a current user; i.e. Group Name = Student, Group ID = 1234)
    - Name for this table defined in .env


## Scripts & Sequence

TAMU_AGOL_Main.py calls all other scripts in the following order:

1) TAMU_AGOL_Catalog.py
    - Takes previous member, items, and entraid reports (if still present in Database), and adds them to their associated history tables.
    - Collects new AGOL reports and adds them as database tables.
    - Uses current member report to generate new EntraID Status table (Using TAMU_AGOL_EntraID.ps1) and adds it as a new table in the database.

2) TAMU_AGOL_DeleteStatus.py
    - Uses current member and entraid reports to determine which users should be flagged for deletion and which ones have been flagged for a month and should be deleted.
    - Emails users and their associated managers notifying them if they have been scheduled to be deleted.

3) TAMU_AGOL_DeleteUsers.py
    - Collects all users identified for deletion and adds their username to a csv to use to delete them.
    - Also adds all users who have 0 items published.

4) TAMU_AGOL_UserQuotas.py
    - Finds all users that exceed the allowed feature storage per user and emails them asking them to reduce storage use.


## Credits

Daniel W. Goldberg, PhD (Texas A&M University, Dept. of Geography): Project manager, GIS Help Desk Lead

Dalton Peterson (Texas A&M University, Dept. of Geography): Lead developer, GIS Help Desk Technician

Sam Palmer (University of Florida, GeoPlan Center): Inspiration for User/Item Database structure



