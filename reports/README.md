# Reports Directory

This is a file directory where reports will be written to when scripts are called. Files written to this directory include:

1) OrganizationItems_date.csv: Most recent AGOL Item report (written from TAMU_AGOL_Catalog.py)

2) OrganizationMembers_date.csv: Most recent AGOL Member report (written from TAMU_AGOL_Catalog.py)

3) AGOL_EntraID_Status.csv: Most recent EntraID Status report based on the written member report (written from TAMU_AGOL_EntraID.ps1)

4) error_EntraID_Users.txt: Users that were attempted to be checked by EntraID but failed (written from TAMU_AGOL_EntraID.ps1)

5) UsersToDelete_date.csv: Users that have been identified to be deleted (written from TAMU_AGOL_DeleteUsers.py)

6) UsersOverQuota_date.csv: Users that are currently above the determined quota for AGOL feature storage (written from TAMU_AGOL_UserQuotas.py)

