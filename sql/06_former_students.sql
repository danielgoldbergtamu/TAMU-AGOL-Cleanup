/*  People still in the identity provider who are marked as former students or alumni, and who
    hold no current affiliation (not enrolled, not employed, not instructing).

    They are emailed at the end of each semester to collect their own data. Those who were
    student employees also have a supervisor in HIST_Supervisors, who is told as well.

    Group membership comes from AGOL_EntraID_Status.Groups. Former_EntraID_Groups and
    Whitelisted_EntraID_Groups hold your tenant's group IDs (see 00_setup.sql).
*/
SELECT s.[Username], s.[WorkingEmail], s.[ManagerEmail], s.[ManagerDepartment], s.[updated_date]
FROM dbo.AGOL_EntraID_Status s
WHERE s.[EntraID_Status] = 1
  AND EXISTS     (SELECT 1 FROM dbo.Former_EntraID_Groups f      WHERE s.[Groups] LIKE '%' + f.[ID] + '%')
  AND NOT EXISTS (SELECT 1 FROM dbo.Whitelisted_EntraID_Groups w WHERE s.[Groups] LIKE '%' + w.[ID] + '%');
