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
   item needs backing up again.

Storage size is not compared for hosted feature layers: the Lifeboat's data store sizes the same data
differently. Feature and attachment counts are the reliable measure there.

Items that are not data (web maps, apps, dashboards, StoryMaps, notebooks) pass on 1 and 2 plus
their JSON definition being present. Their references to layers are listed so a broken reference
after the deletes is known in advance.

## Steps

1. **Read-only access to the Lifeboat.** A service account on the Lifeboat with view-only privileges,
   checked before every run the same way `check_read_only.py` checks the ArcGIS Online account. The
   check job never writes to either portal.
2. **Inventory the Lifeboat.** Every item: id, owner, title, type, created, modified, size, type
   keywords and the recorded source id. Kept per run in `LIFEBOAT_Items` (one row per item per run),
   like the ArcGIS Online inventories.
3. **Match.** Each ArcGIS Online item is matched to its Lifeboat copy (rule 1). The result is
   one of: matched, not found, ambiguous.
4. **Compare.** For every matched item, rules 2 to 4: query each layer's count of features and
   attachments and its fields on both sides. This is the slow part (about 150,000 layers), so it runs per
   backup year and remembers results: an item is compared again only when either side changed.
5. **Record.** One `backup_check` row per item per check (`docs/data-model.md`), widened to say which
   test failed: `found`, `backup_item_id`, `structure_matches`, `counts_match`, `files_match`,
   `changed_since_backup`, `detail`. An item is backed up when its latest row passes every test.
6. **Spot check by a person.** For each backup year, a random sample (about 20 items across types and
   owners) is listed with links to both copies; the maintainer opens them, and the sample's outcome is
   recorded. A year is not marked verified until its sample passes.
7. **Show it on the site.** A "Backup check" section on the Proposed removals page, by backup year:
   items and GB checked, passed, failed (by reason), not found, and the spot check's state; the failing
   items are listed so they can be backed up again. The deletion selection marks each layer as
   backed up or not, and the CSV it downloads carries the check's result and date.
8. **Gate the delete.** The delete script (AGOL-018) refuses any item whose latest `backup_check`
   does not pass, any item in a year not marked verified, and any set without the maintainer's written
   approval (`removal_set.approved_*`). Protected accounts are refused as before. It runs a dry run
   first and writes a results file of every item and what happened.
9. **After a delete,** the item stays in the ArcGIS Online recycle bin for its retention period; the
   next run confirms each deleted item is gone from ArcGIS Online and still present in the Lifeboat.

## Order of work

1. Read-only Lifeboat account and its check (step 1); inventory and match for one year (steps 2–3).
   This shows how the backup tool records the source id, which decides the matching rule.
2. Comparison and records for that year (steps 4–5), and the site section (step 7).
3. Spot check of that year (step 6). The first year is the pilot: the first deletion set proposed for
   review comes only from it.
4. The remaining years, then the delete gate (step 8) before any delete script exists.
