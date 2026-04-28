# How to Override when an account is scheduled to be deleted

Author: Dalton Peterson '26

Last updated: April 2026

## Background

The scripts in this repo are designed to give a graduated student/ former staff member a month to contact the GIS Help Desk before their items are deleted for good. Thus, a way to "override" scheduled deletion is necessary in case we need to migrate their items or if their was an error in identifying their affiliation within the University.

There is a built-in Override in TAMU_AGOL_DeleteStatus.py that removes users from consideration from deletion when enabled. 

[Code within script that applies override](images/override_code.png)

## I. Determining if an account has an Override

1) Open Microsoft SQL Server Management Studio (SSMS) and connect to the AGOL Cleanup Database

2) In the "DeleteStatus" table (Generated/updated by TAMU_AGOL_DeleteStatus.py), find the user that you would like to set an override for

    - Use a query like:
        SELECT *
        FROM dbo.DeleteStatus
        WHERE [WorkingEmail] = 'youremail'

3) Locate the column "Override"
    - If it is set to "0", then the user doesn't have an override and can be flagged or deleted 
    - If it is set to "1", then the user has an override, and won't be flagged or deleted

    [User with Override column identified](images/override_verify.png)
    - In this case, I do not have an override on my account. When I graduate, my account will be flagged for deletion and then will be deleted a month later.

## II. Applying an Override

4) Update the "Override" column in the DeleteStatus table to equal 1

    - Use a query like:
        UPDATE dbo.DeleteStatus
        SET Override = 1
        WHERE WorkingEmail = 'youremail'

5) Verify that the override has been applied

    - Use a query like:
        SELECT *
        FROM dbo.DeleteStatus
        WHERE [WorkingEmail] = 'youremail'

6) Next time the script runs, the Override will be detected and DeleteStatus will be set to 0 (user will not be considered for deletion)