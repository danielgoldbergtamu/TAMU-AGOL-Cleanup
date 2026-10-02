/*  Users who own nothing and have not logged in for a while: candidates for account removal.

    Why: the license caps the number of accounts. An account with no items and no groups has
    nothing to back up, and an SSO account is recreated at its next login, so removing it costs
    the user nothing. Tested 7 April 2026 by deleting a test SSO account and logging back in.
    Safeguard: the ArcGIS API refuses to delete a user who owns content or groups.

    Role and login type keep administrators and non-SSO accounts out of the list.
*/
DECLARE @LastLoginBefore DATETIME = '2025-01-01';
DECLARE @Role NVARCHAR(100) = 'Publisher with Notebooks';
DECLARE @LoginType NVARCHAR(20) = 'enterprise';   -- SSO accounts only

SELECT m.[Username], m.[Last Login Date], m.[Date Created], m.[Role], m.[User Type]
FROM dbo.CUR_OrganizationMembers m
WHERE m.[# of Items Owned] = 0
  AND m.[# of Groups Owned] = 0
  AND m.[# of Groups Total] <= 1          -- only the default group
  AND m.[Role] = @Role
  AND m.[Login Type] = @LoginType
  AND m.[Last Login Date] < @LastLoginBefore
ORDER BY m.[Date Created];
