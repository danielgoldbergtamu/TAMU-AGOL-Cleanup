/*  Users the identity lookup could not find at all: no active account, not even a former
    student or affiliate. These are the people most clearly no longer at the institution.

    Reads AGOL_EntraID_Status, written by the identity lookup on every catalog run
    (EntraID_Status: 1 = found, 0 = not found).
*/
SELECT s.[Username], s.[EntraID_Status], s.[ManagerEmail], s.[UserDepartment], s.[updated_date]
FROM dbo.AGOL_EntraID_Status s
WHERE s.[EntraID_Status] = 0;
