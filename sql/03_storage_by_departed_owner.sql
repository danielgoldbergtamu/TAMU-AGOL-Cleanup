/*  Storage held by owners who are no longer in the identity provider, largest first.

    Why: lets the biggest departed owners be reviewed one at a time, in order of how much storage
    removing their content would free. Sizes are in MB as the item report gives them.
*/
DECLARE @MinFeatureMB FLOAT = 1000;      -- only owners holding more than about 1 GB

WITH owner_totals AS (
    SELECT i.[Owner],
           COUNT(*)                      AS item_count,
           SUM(i.[File Storage Size])    AS file_mb,
           SUM(i.[Feature Storage Size]) AS feature_mb
    FROM dbo.CUR_OrganizationItems i
    WHERE i.[In Recycle Bin] = 'No'
      AND i.[Owner] IN (SELECT s.[Username] FROM dbo.AGOL_EntraID_Status s WHERE s.[EntraID_Status] = 0)
    GROUP BY i.[Owner]
)
SELECT m.[Username], t.item_count, t.file_mb, t.feature_mb, m.[Last Login Date], m.[Login Type]
FROM owner_totals t
JOIN dbo.CUR_OrganizationMembers m ON m.[Username] = t.[Owner]
WHERE t.feature_mb > @MinFeatureMB
ORDER BY t.feature_mb DESC;
