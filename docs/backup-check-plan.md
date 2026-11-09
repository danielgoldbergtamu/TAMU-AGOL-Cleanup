# Checking the Lifeboat backup before anything is deleted

Plan of 8 November 2026. **Pilot: items created 2010 to 2013**, the years restored to the Lifeboat first. The Lifeboat (the on-premises ArcGIS Enterprise portal that holds the backup;
its address is in the private configuration) now has backups made year by year. Before any layer is
deleted from ArcGIS Online, each one has to be shown to be in the Lifeboat, complete. This plan says how
that is checked, how the result is recorded, and how the delete step is made to depend on it.

The project's hard rule (`CLAUDE.md`, "Deleting") is unchanged: a passing check makes an item
**eligible**. Nothing is deleted until the maintainer has reviewed the specific set and approved it in
writing.

## What "backed up" means

An ArcGIS Online item is backed up when the Lifeboat holds an item that:

1. **is the same item**, matched by the source item id the backup tool records (a type keyword or the
   item's properties), or, if the tool records none, by owner, title, type and creation date, with any
   ambiguous match flagged and never counted as passing;
2. **has the same structure**: same item type; for a feature service, the same layers and tables, with
   the same fields (name and type), geometry type and spatial reference;
3. **has the same content**: for each layer and table, the same number of features and of attachments;
   for a file item (PDF, shapefile, file geodatabase, image, and so on), the same file size, plus a
   checksum of both copies for a sample;
4. **is not older than the original**: the ArcGIS Online item was not modified (data or definition)
   after the backup copy was made. If it was, the check fails with "changed since the backup", and the
   item needs backing up again;
5. **has its owner**: the owner's account exists in the Lifeboat (the same user, or the account it was
   mapped to, recorded in a reviewed mapping list), and the copy is owned by that account;
6. **has its groups and sharing**: every group the original is shared with exists in the Lifeboat (same
   group, matched by its source id or title and owner), the copy is shared with each of them, and its
   sharing level (private, organization, public) is the same. The group's owner and members are checked
   too, so the people who could use the layer in ArcGIS Online can use it in the Lifeboat;
7. **has everything it depends on**, and points at the Lifeboat copies of them, never back at ArcGIS
   Online:
   - a **view layer**: its source layer, backed up and passing;
   - a **web map or scene**: every layer, table and basemap it uses;
   - an **app, dashboard, StoryMap, Experience or survey**: every map, layer and item it uses;
   - a **layer**: its related tables, relationship classes, attribute domains, attachments, and any
     other service or item its definition or pop-ups refer to;
   - every item: its data (JSON definition or file), thumbnail and metadata.
   A reference that still points at an ArcGIS Online URL or item id fails the check, because it breaks
   when the original is deleted;
8. **is usable as it was**: opened as its owner would open it. A layer answers a query (a sample of
   features by id, with geometry and attributes equal to the original's) and draws (a small map image of
   its extent); a web map opens with every layer resolving; an app or dashboard loads its map and data.

Storage size is not compared for hosted feature layers: the Lifeboat's data store sizes the same data
differently. Feature and attachment counts are the reliable measure there.

Items that are not data (web maps, apps, dashboards, StoryMaps, notebooks) pass on 1, 2 and 4 to 8,
with their JSON definition present. A layer can only pass once everything that depends on it has been
found too, so deleting it never breaks a map or app that is kept: the check lists, for each layer, the
items that use it in ArcGIS Online and whether those are backed up or kept.

## Steps

1. **Read-only access to the Lifeboat.** A service account on the Lifeboat with view-only privileges,
   checked before every run the same way `check_read_only.py` checks the ArcGIS Online account. The
   check job never writes to either portal.
2. **Inventory the Lifeboat.** Every item: id, owner, title, type, created, modified, size, type
   keywords, the recorded source id, sharing level and groups; every user; every group with its owner
   and members. Kept per run in `LIFEBOAT_Items`, `LIFEBOAT_Users` and `LIFEBOAT_Groups` (one row per
   thing per run), like the ArcGIS Online inventories. The ArcGIS Online side already has its items,
   users, groups and group sharing (`group_inventory.py`).
3. **Match.** Each ArcGIS Online item, user and group is matched to its Lifeboat copy (rules 1, 5
   and 6). The result is one of: matched, not found, ambiguous.
4. **Map dependencies.** For each ArcGIS Online item, what it uses and what uses it: read from view
   sources, web map and app definitions (layer URLs and item ids), related tables and pop-up
   references. Kept as `ITEM_Dependency` (item, depends on, how). Each dependency must have a passing
   Lifeboat copy, and the copy must refer to the Lifeboat copy (rule 7).
5. **Compare and use.** For every matched item, rules 2 to 8: owner, groups and sharing; each
   layer's fields and counts of features and attachments on both sides; a sample of features compared;
   a small map image drawn; each map and app opened with its references resolved. This is the slow part (about 150,000 layers), so it runs per
   backup year and remembers results: an item is compared again only when either side changed.
6. **Record.** One `backup_check` row per item per check (`docs/data-model.md`), widened to say which
   test failed: `found`, `backup_item_id`, `structure_matches`, `counts_match`, `files_match`,
   `changed_since_backup`, `owner_ok`, `groups_ok`, `sharing_ok`, `dependencies_ok`, `usable`, `detail`. An item is backed up when its latest row passes every test.
7. **Spot check by a person.** For each backup year, a random sample (about 20 items across types and
   owners) is listed with links to both copies; the maintainer signs in to the Lifeboat as an ordinary
   user would, opens each one and the maps and apps that use it, and the sample's outcome is
   recorded. A year is not marked verified until its sample passes.
8. **Show it on the site.** A "Backup check" section on the Proposed removals page, by backup year:
   items and GB checked, passed, failed (by reason), not found, and the spot check's state; the failing
   items are listed so they can be backed up again. The deletion selection marks each layer as
   backed up or not, and the CSV it downloads carries the check's result and date.
9. **Gate the delete.** The delete script (AGOL-018) refuses any item whose latest `backup_check`
   does not pass, any item in a year not marked verified, and any set without the maintainer's written
   approval (`removal_set.approved_*`). Protected accounts are refused as before. It runs a dry run
   first and writes a results file of every item and what happened.
10. **After a delete,** the item stays in the ArcGIS Online recycle bin for its retention period; the
   next run confirms each deleted item is gone from ArcGIS Online and still present in the Lifeboat.

## Decided (8 November 2026)

- The backups were made with **GEO Jobe Backup My Org**. How it records the original item (same item id, a type
  keyword, or neither) is read from the Lifeboat inventory in step 2, and decides the matching rule.
- **Pilot: the oldest backed-up year.**
- **Depth:** structure and counts on every item; checksums and feature samples on a sample.
- **Runs on the maintainer's workstation first**, on the cluster later if it can reach the Lifeboat.
- **Access:** a view-only Lifeboat service account, its sign-in in `.env` (`LIFEBOAT_URL`, `LIFEBOAT_USERNAME`,
  `LIFEBOAT_PASSWORD`).

## First look (8 November 2026)

- The view-only Lifeboat account works: only view privileges, including the three "view all" ones (it needs a user
  type above Viewer to hold them).
- **Backup My Org keeps the original item ids.** Of the Lifeboat's 7,245 items, 6,742 have the id of an item
  still in ArcGIS Online, and 6,725 of those have the same owner. Matching (rule 1) is by item id; the Lifeboat's
  creation dates are the day of the restore, so the original dates come from ArcGIS Online.
- **Backed up so far: items created 2010 to 2017**, each year 94% to 100% of its ArcGIS Online items (2017: 5,481
  of 5,629); 2018 onward has barely started. The items of those years missing from the Lifeboat are the first
  thing to report.
- 503 Lifeboat items match no current ArcGIS Online item: deleted since the backup, or made in the Lifeboat.
- **Restored so far: items created 2010 to 2013** (the maintainer, 8 November 2026). Later years are being restored,
  expected to finish the week of 9 November 2026; their items already in the Lifeboat are from restores still under
  way. **A year counts as restored only when the maintainer says so**, recorded in a reviewed list in the private
  configuration (`lifeboat_restored_years.csv`: year, restored_on, said_by); items of other years can be read
  but are never offered for deletion. The backup is not run again, so each Lifeboat copy is read once and stored.

## Storage (decided 8 November 2026)

The check keeps its own database on the maintainer's workstation (`data/lifeboat-check.duckdb`, never committed):
the Lifeboat inventories, the dependency map and every check result, kept as history (the newest counts). Each
result records what it was based on (the ArcGIS Online item's and the Lifeboat item's modified times, and the
check's version), and an item is checked again only when one of them changes. The ArcGIS Online side comes from the
daily job's inventory. After a check, the per-item results are exported and handed to the cluster through an inbox,
like the Entra lookup; the daily job records them and the site shows them.

**Built 9 November 2026.** On the workstation, `tools/lifeboat_check.py` checks a range of years and
`tools/lifeboat_export.py` writes its results into one DuckDB file (every check row, the ArcGIS Online groups the
items are shared with and their members, the runs, and the reviewed list of restored years).
kubernetes-config's `16-send-lifeboat-check.ps1` does both steps and copies the file into the cluster's Lifeboat
inbox (`/data/lifeboat-inbox`). The next daily or dashboards-only run adds the rows its database does not have yet
(`pipeline_daily.lifeboat_results`; nothing is replaced), and the site's read-only copy carries them.

## Pilot result: 2010 to 2013 (9 November 2026)

106 ArcGIS Online items were created in those years. **100 passed every test.** A full check takes about
2.5 minutes; the Lifeboat copies are read once and stored, so a later check reads only the ArcGIS Online side.

Six failed, and are real backup problems: two items are not in the Lifeboat (a shapefile and an operation view);
one feature service's copy has no layers, so the web map that uses it still points at ArcGIS Online; one app's copy
is not shared with its group; one map package's file is a different size. **Decided (9 November 2026): those six are
left out of any delete for now**, kept as they are, and the site shows them as "backup failed". They become
candidates only if a later check passes.

Of the 100 that passed, the review page counts 83 that nobody current can use (owner left; private, or shared only
with groups that have no current member) and 17 still in use.

## The review page (asked for 8 November 2026)

A page of the full edition where the maintainer reviews proposed deletes and chooses which to do, one layer at a
time or by owner. For each layer it shows:

- **the layer**: type, size, created, modified, last viewed and views, sharing level, delete protection,
  content status (authoritative, deprecated);
- **its owner** and their status (Entra, delete status, protected), unit and supervisor;
- **who else can use it**: each group it is shared with, the group's owner and members and their statuses
  (how many are current), and whether the group is a course or unit group;
- **what it depends on and what depends on it** (views, maps, apps, surveys, sites), with each one's owner,
  status and whether it is kept;
- **the backup check**: pass or fail, by test, with links to both copies;
- **things a delete would break or lose** (below), as warnings.

Also weighed, as warnings (decided 8 November 2026):

- use a dependency map cannot see: public layers that may be embedded or called by URL elsewhere, and edits to
  the data by other people (the data's last edit date, not the item's). **Last viewed and view counts are not
  used**: nearly every layer is read daily by bots, so they say nothing about use;
- field and mobile use: Survey123 forms, Field Maps and offline areas, QuickCapture and Workforce projects, sync
  replicas;
- owner and organization intent: delete protection, authoritative or deprecated status, and organization settings
  that point at the item (basemap gallery, templates, home page, featured content);
- groups: shown with how many members are current. **A layer none of whose users (owner, group members) is
  current is not in use by anyone**, a course's instructor included; no separate course-group rule;
- research data, records retention and legal holds are not checked by the site: users have been told not to keep
  research data in ArcGIS Online;
- transfer instead of delete is not a separate step: when a supervisor is known, they are notified before the
  delete, as already planned (the 30-day notice).

**Built 9 November 2026**: the Backup check page of the full edition (`#backup`; `queries.backup`). Each item has a
standing: *backup failed* (never offered), *year not restored* (never offered), *in use* (the owner is current, it is
shared with the organization or the public, or one of its groups has a current member), or *no current users*. Its
details show the tests, what differs, the owner with status, supervisor and protection, each group with every
member's status, what it uses, and what uses it. **"Used by" counts only the items checked so far**: a map made in
a later year that uses a 2012 layer shows once that year is checked. Not there yet: organization settings that point
at an item, and the data's last edit date.

The choices made on the page are a proposal; the CSV it produces is reviewed and approved by the maintainer in
writing before anything is deleted (`CLAUDE.md`, "Deleting").

## Order of work

1. Read-only Lifeboat account and its check (step 1); inventory and match for one year (steps 2–4).
   This shows how the backup tool records the source id, which decides the matching rule.
2. Comparison and records for that year (steps 5–6), and the site section (step 8).
3. Spot check of that year (step 7). The first year is the pilot: the first deletion set proposed for
   review comes only from it.
   **Restore test first** (decided 8 November 2026): one ArcGIS Online item from the pilot that looks completely
   safe is proposed; once the maintainer approves deleting that one item in writing, it is deleted, and the
   maintainer restores it from the Lifeboat to ArcGIS Online and checks it works. No other delete is proposed until
   that restore has worked.
4. The remaining years, then the delete gate (step 9) before any delete script exists.
