# Checking the Lifeboat backup before anything is deleted

Plan of 8 November 2026. The Lifeboat (the on-premises ArcGIS Enterprise portal that holds the backup;
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

## Order of work

1. Read-only Lifeboat account and its check (step 1); inventory and match for one year (steps 2–4).
   This shows how the backup tool records the source id, which decides the matching rule.
2. Comparison and records for that year (steps 5–6), and the site section (step 8).
3. Spot check of that year (step 7). The first year is the pilot: the first deletion set proposed for
   review comes only from it.
4. The remaining years, then the delete gate (step 9) before any delete script exists.
