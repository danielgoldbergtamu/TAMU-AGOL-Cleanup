/*  Departed owners (not in the identity provider) with items nobody has ever viewed, who have
    not logged in since a cutoff. Never-viewed content of a departed owner is the safest content
    to remove, but a former supervisor may still want some of it: notify first.

    "Never viewed" means Date Last Viewed is empty. The item report has kept view dates only
    since 2019, so "never" means "not since 2019".
*/
DECLARE @LastLoginBefore DATETIME = '2023-01-01';
DECLARE @LoginType NVARCHAR(20) = 'enterprise';   -- SSO accounts, whose identity lookup is reliable

SELECT m.[Username], m.[Last Login Date], m.[# of Items Owned],
       COUNT(i.[Item ID])            AS never_viewed_items,
       SUM(i.[Feature Storage Size]) AS never_viewed_feature_mb
FROM dbo.CUR_OrganizationItems i
JOIN dbo.CUR_OrganizationMembers m ON m.[Username] = i.[Owner]
JOIN dbo.AGOL_EntraID_Status s ON s.[Username] = m.[Username]
WHERE i.[Date Last Viewed] IS NULL
  AND i.[In Recycle Bin] = 'No'
  AND s.[EntraID_Status] = 0
  AND m.[Login Type] = @LoginType
  AND m.[Last Login Date] < @LastLoginBefore
GROUP BY m.[Username], m.[Last Login Date], m.[# of Items Owned]
ORDER BY never_viewed_feature_mb DESC;
