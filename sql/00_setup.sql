/*  Setup: point the selection queries at the newest report tables, and create the group tables.

    The catalog step (TAMU_AGOL_Catalog.py) loads each AGOL report into a dated table such as
    OrganizationMembers_2026_10_01. The queries in this folder read two synonyms instead, so they
    never need editing when a new report arrives. Re-run this script after each catalog run.

    Group tables (identity-provider group object IDs, never personal data):
      Whitelisted_EntraID_Groups  groups whose members are CURRENTLY affiliated (student, staff,
                                  faculty, instructor, student worker...). Also read by
                                  TAMU_AGOL_DeleteStatus.py.
      Former_EntraID_Groups       groups that mark FORMER affiliation (alumni, former student).
    Fill both with your own tenant's group IDs. Keep that insert script out of this repository.
*/
DECLARE @members SYSNAME = (SELECT TOP 1 name FROM sys.tables WHERE name LIKE 'OrganizationMembers[_]20%' ORDER BY name DESC);
DECLARE @items   SYSNAME = (SELECT TOP 1 name FROM sys.tables WHERE name LIKE 'OrganizationItems[_]20%'   ORDER BY name DESC);

IF OBJECT_ID('dbo.CUR_OrganizationMembers', 'SN') IS NOT NULL DROP SYNONYM dbo.CUR_OrganizationMembers;
IF OBJECT_ID('dbo.CUR_OrganizationItems', 'SN') IS NOT NULL DROP SYNONYM dbo.CUR_OrganizationItems;
EXEC ('CREATE SYNONYM dbo.CUR_OrganizationMembers FOR dbo.' + @members);
EXEC ('CREATE SYNONYM dbo.CUR_OrganizationItems FOR dbo.' + @items);
PRINT 'CUR_OrganizationMembers -> ' + @members;
PRINT 'CUR_OrganizationItems   -> ' + @items;

IF OBJECT_ID('dbo.Whitelisted_EntraID_Groups') IS NULL
    CREATE TABLE dbo.Whitelisted_EntraID_Groups ([Group Name] NVARCHAR(256) NOT NULL, [ID] NVARCHAR(64) NOT NULL);
IF OBJECT_ID('dbo.Former_EntraID_Groups') IS NULL
    CREATE TABLE dbo.Former_EntraID_Groups ([Group Name] NVARCHAR(256) NOT NULL, [ID] NVARCHAR(64) NOT NULL);
