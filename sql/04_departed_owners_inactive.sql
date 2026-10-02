/*  Owners not in the identity provider who have not logged in since a cutoff, and who own items
    OR groups. Their content is the first to be disabled (made private), which keeps its hooks in
    maps and groups and shows, through view dates, whether anyone still depends on it.

    Corrected 2 October 2026. The April version required items AND groups, although its comment
    said "or". It matched 1,021 users where the intended condition matches 2,631: the 1,589 owners
    of items without groups were never included in the April disable pass.

    Optional: restrict to accounts whose username matches a pattern (for example class accounts).
*/
DECLARE @LastLoginBefore DATETIME = '2025-01-01';
DECLARE @UsernameLike NVARCHAR(100) = '%';        -- e.g. '%BAO%' for one family of class accounts

SELECT m.[Username], m.[Last Login Date], m.[# of Items Owned], m.[# of Groups Owned], m.[Role], m.[User Type]
FROM dbo.AGOL_EntraID_Status s
JOIN dbo.CUR_OrganizationMembers m ON m.[Username] = s.[Username]
WHERE s.[EntraID_Status] = 0
  AND (m.[# of Items Owned] <> 0 OR m.[# of Groups Owned] <> 0)
  AND m.[Last Login Date] < @LastLoginBefore
  AND m.[Username] LIKE @UsernameLike
ORDER BY m.[Last Login Date];
