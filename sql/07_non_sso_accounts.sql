/*  Accounts that sign in with an ArcGIS login instead of the institution's single sign-on.

    Why: all users are being moved to SSO logins. The identity lookup cannot reliably match
    these accounts, and their owners have to move their content to their SSO account.
*/
DECLARE @LoginType NVARCHAR(20) = 'arcgis';

SELECT m.[Username], m.[Role], m.[Last Login Date], m.[Date Created], m.[# of Items Owned], m.[# of Groups Owned]
FROM dbo.CUR_OrganizationMembers m
WHERE m.[Login Type] = @LoginType
ORDER BY m.[Last Login Date];
