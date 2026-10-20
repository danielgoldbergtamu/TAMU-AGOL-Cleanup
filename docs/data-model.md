# Data model (proposal, for review)

**Status: proposal, 20 October 2026.** Nothing here is built yet. It replaces the structure the
cleanup inherited, which was built to answer one question ("who should be deleted now?") and makes the
other questions below hard or impossible. Review it before anything is created; the decision and action
tables (part 4) come first.

## 1. What the structure has to answer

1. **Who is still at the university, and when did that change?** For any account: current, no current
   affiliation, or not in the identity provider, on every run, and the run on which it changed.
2. **What does each unit use, and how is that changing?** Users, owners, items, feature and file storage
   by college or agency and by department, current and departed, run by run.
3. **What could be removed, and is it safe?** Removal candidates by group (empty accounts, status 2,
   status 1), each item's backup status, who was notified and when.
4. **What was done, and on whose authority?** For every account or item removed: the run that proposed
   it, the notices sent, the backup that holds it, the approval, and the result.
5. **Who should be told?** The supervisor or unit for a departed owner, from the last time it was known.

## 2. Principles

- **Everything hangs off a run.** Every observation and decision records the run it came from.
- **Append-only.** Rows are added, never updated in place or deleted. "Current" is the latest row. The
  one exception is a lookup table being corrected (a unit's name, a department's mapping), and even those
  keep their history.
- **Things and observations are separate.** A table of people says who someone *is*; a table of
  observations says what was *seen about them on a date*. That is what makes question 1 a query.
- **Decisions and actions are events.** A status change, a notice, a backup check and a removal are
  each a row with a time, a reason and a run, never a column that is overwritten.
- **Personal data stays in the database.** The repository holds code and this document only.

## 3. Things (one row per thing; slowly changing)

| Table | Key | Columns | Notes |
|---|---|---|---|
| `person` | `person_id` | NetID, UIN (if available), primary email, display name, first_seen, last_seen | One person can own several accounts. NetID changes are possible, so NetID is not the key |
| `account` | `username` | person_id, login type (SSO / ArcGIS), created, first_seen, last_seen | Links an old ArcGIS-login account to the same person's SSO account when the email shows it |
| `item` | `item_id` | owner username at first sight, type, title, created, first_seen, last_seen | Ownership changes are observations (part 5), not edits here |
| `group` | `group_id` | title, owner, access, created, first_seen, last_seen | |
| `unit` | `unit_id` | name, kind (college, agency, school, department, administrative), parent_unit_id, valid_from, valid_to | Departments roll up to colleges or agencies here, once, instead of in code |
| `unit_alias` | alias text | unit_id, source (Entra department name, directory college code, major code), valid_from, valid_to | Replaces the draft department-to-unit CSV; reviewed by a person |

## 4. Decisions and actions (append-only events) — build first

These are the evidence that any removal was proposed, communicated, backed up and approved.

### `status_event`
One row each time an account's delete status changes.

| Column | Meaning |
|---|---|
| event_id | key |
| username | account |
| run_id | the run that made the change |
| at | timestamp |
| from_status, to_status | 0 kept, 1 flagged, 2 listed for removal |
| reason | current affiliation / no current affiliation / not in Entra / owns nothing / flagged 30+ days / override |
| dry_run | true for a dry run (recorded, never acted on) |

The current status of an account is its latest non-dry-run event. This replaces the overwritten
`DeleteStatus` table and the changes files.

### `override`
| Column | Meaning |
|---|---|
| username, set_by, at, reason, expires (optional) | An account kept regardless of status. Added and ended by rows, never edited |

### `notice`
| Column | Meaning |
|---|---|
| notice_id, run_id, at | |
| recipient (email), recipient_role | owner / supervisor / unit contact / dependant of a shared item |
| about_username, about_item_id (optional) | |
| template, channel (email, CRM) | which message, and through what |
| sent_by | who sent it (a person; Claude only drafts) |
| response, response_at | reply recorded later as a further row |

### `backup_check`
| Column | Meaning |
|---|---|
| item_id, run_id, at | |
| backup_system | e.g. the Aggie Cloud lifeboat portal |
| found, backup_item_id, size_matches | the item exists in the backup, with the size it had in AGOL |

An item is backed up when its latest `backup_check` says found and size_matches.

### `removal_set` and `removal`
| `removal_set` column | Meaning |
|---|---|
| set_id, created_at, run_id | the proposed set, from a dry run |
| criteria | which group (empty accounts, status 2 content, ...) and the rules used |
| accounts, items, feature_gb | its size when proposed |
| **approved_by, approved_at, approval_text** | **the maintainer's own words approving this set after reviewing it. Nothing is removed without this** |

| `removal` column | Meaning |
|---|---|
| set_id, username or item_id, at, action (deprecate / disable / delete / reassign) | one row per thing acted on |
| backup_check_id | the backup check that cleared it (content only) |
| result, error | what happened |

**The tooling refuses** to act on an item without a passing backup check, or on any set without an
approval row. This puts the project's hard rule (`CLAUDE.md`, "Deleting") into the data as well as the
process.

## 5. Observations (one row per thing per run, or per change)

| Table | Grain | Columns |
|---|---|---|
| `account_obs` | account × run | role, user type, last login, items owned, groups owned, account status |
| `identity_obs` | account × run | found in Entra, matched address, **is_current_student, is_current_employee, is_instructor, is_former_student, is_alumni, is_affiliate** (yes/no, derived from groups at load), department, supervisor email, supervisor department |
| `identity_group` | account × group × run | the raw Entra group IDs, kept so the yes/no columns can be re-derived |
| `directory_obs` | account × run | found, college, major, classification, employee department, title |
| `item_obs` | item × run, **only when something changed** | owner, feature MB, file MB, share level, groups shared with, views, last viewed, in recycle bin |
| `item_group` | item × group, with first_seen / last_seen | which groups an item is shared to |

Unchanged items are not rewritten every run: an item's row covers the runs until it changes (as the
current history tables already do). Accounts and identity are written every run; that is what makes
status-over-time exact to the run.

## 6. Derived per run (rebuildable)

| Table | Grain | Columns |
|---|---|---|
| `account_status` | account × run | classification, unit_id, sub-unit, how the unit was assigned, items, GB, projected status |
| `unit_usage` | unit × status × run | members, owners, items, feature GB, file GB |

These are the snapshot tables started on 20 October (`SNAP_MemberStatus`, `SNAP_UnitUsage`) under their
final names. They can always be rebuilt from parts 3–5, but are stored so a dashboard never has to.

## 7. Engine: DuckDB by default

| | DuckDB | SQL Server (today) |
|---|---|---|
| Install | a Python package; the database is one file | a server, with Windows services and drivers |
| Moving between work and home | copy one file (check-out / check-in, below) | not practical; backups are 600 MB–1 GB |
| Other universities | `pip install` | need a SQL Server |
| Size | columnar and compressed; tens of millions of rows is routine | fine |
| Several people writing at once | no (one writer) | yes |
| Dashboards (Power BI, ArcGIS) | through Parquet exports or ODBC | direct |

**Recommendation: DuckDB.** The work has one writer at a time, needs to move between two machines, and is
meant to be reusable elsewhere, which are the cases DuckDB suits. The cost is that two people cannot
write at once.

**Check-out and check-in between machines.** The database file lives on the machine that holds it. A
`db_checkout` / `db_checkin` pair moves it: check-in closes it, records a checksum and the holder, and
copies it out (over Remote Desktop drive redirection, or TAMU OneDrive if approved for this data);
check-out refuses unless the file is checked in and the checksum matches. One holder at a time, always.

## 8. Migration, in stages

1. **Decision and action tables (part 4)** in the current database, used by the delete-status step and
   the removal tools. Needed before any removal; nothing else depends on them.
2. **Units** (`unit`, `unit_alias`), loaded from the reviewed department-to-unit table.
3. **Things and observations** (parts 3 and 5), filled from what already exists: the history tables, the
   supervisor history, the snapshots from 3 October, and each new run.
4. **Switch the scripts** to read and write the new tables, one at a time, each checked against the old
   result before the old path is removed.
5. **Move to DuckDB**, if confirmed, once the new tables are the only ones in use.
6. **Retire the original tables** after a final backup, kept in `C:/Data/ArcGIS-Online`.

## 9. Questions for the maintainer

- Which identity flags matter beyond current student, current employee and former student (for example
  instructor, affiliate, retiree)?
- Who, besides the maintainer, may approve a removal set?
- How long should `notice` keep message text, and should it store the text or only the template?
- Is TAMU OneDrive approved for moving the database file between machines, or is Remote Desktop the only route?
