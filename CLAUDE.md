# TAMU AGOL Cleanup: working rules

This repository is the single home for Texas A&M's ArcGIS Online cleanup: the code, the method,
and the record of what was run. It replaced the Foreman project `ArcGIS-Online-Cleanup` on
2 October 2026. **It is public**, and other universities are meant to reuse it.

## The one rule that cannot bend: no organization data in git

Exports, Entra lookups, candidate lists and run results name real people, most of them students
who have left and cannot be asked. **None of it is ever committed.**

- Real data lives outside version control: `reports/` (ignored), `private/` (ignored), or
  `P:/Data/ArcGIS-Online-Cleanup`.
- `tools/leak_check.py` runs before every commit through `.githooks/pre-commit`. On a fresh clone,
  enable it with `git config core.hooksPath .githooks`. **Never bypass it with `--no-verify`.** If
  a line is genuinely safe (a format string, a documented example), end it with
  `leak-check: allow`.
- Test data is synthetic and lives in `tests/fixtures/`.
- Writing about the work goes in `docs/` and uses counts, never names.

## Deleting

These come from the project's experience and are not optional:

1. **No content is deleted before a verified backup exists.** Backup, then verify the backup,
   then delete. An approved contract is not a completed backup.
2. **Deprecate before deleting.** Set an item private first and keep its hooks in maps and
   groups. Give dependants a week to reply. Do not shorten that because a deadline is close.
3. **Empty accounts are the exception.** An account with no items and no groups has nothing to
   back up, and an SSO account is recreated at its next login. These can be removed without the
   backup, after a fresh snapshot.
4. **Every delete run has a dry run first, a protected list (administrators, the operator), and a
   results file** naming each user or item and what happened to it.
5. **A live delete runs only on an explicit go-ahead in chat, for that run.** Approval for one run
   does not carry over to the next.
6. **Snapshots go stale.** Re-derive candidates from a fresh export before any live run.

## Supervisors are captured while they still can be

**Before a departed person's content is removed, their supervisor is the one told to collect
it.** Entra returns a department for faculty and staff, and a supervisor (manager) for every
employee, student employees included. **It stops returning them once the person leaves.** So:

- The identity lookup runs on **every** snapshot, not only when a cleanup is due.
- Every supervisor ever seen for a person is **kept in a history table and never overwritten by
  a blank**. The most recent non-empty supervisor is the one notified.
- Old lookups (earlier status exports, run files, SQL Server history tables) are merged into that
  history, not discarded. For many people who have already left, they are the only record.
- Check that the supervisor is still at the university before notifying them.
- **Students with no supervisor have no fallback contact.** Students are emailed at the end of
  each semester to collect their own data, and that notice is their warning. Don't invent a
  department or instructor contact for them.
- The merged history lives in SQL Server as `HIST_Supervisors`, built by
  `tools/build_supervisor_history.py` from every earlier lookup.

## Nothing from an earlier run is deleted

Dan, 2 October 2026: "we do not delete prior runs or data when we initiate new runs."

- Every run is recorded in the `RUNS` table and writes only into its own folder, `reports/runs/<run id>/`
  (`TAMU_AGOL_Runs.Run`). Pipeline steps share the pipeline's id through `AGOL_RUN_ID`.
- History tables (`HIST_OrganizationMembers`, `HIST_OrganizationItems`, `HIST_EntraID_Status`) are
  append-only. Each row has a fingerprint (`row_hash`) and `first_seen` / `last_seen`. A new snapshot
  extends unchanged rows and adds new or changed ones (`merge_snapshot_into_history`). **Never add a query
  that deletes history rows.**
- `DeleteStatus` is copied to `HIST_DeleteStatus` with the run id before every real run.
- `tools/backfill_history.py` rebuilt first-seen dates from the March and April snapshots on 3 October
  2026. Run it only before the history gains new columns, and only after a backup.

## Toolchain direction

The target is **Python only**. The ArcGIS API for Python handles exports and deletes, Microsoft
Graph is called from Python for identity, and DuckDB holds the catalog and queries. For now:

- The PowerShell lookup (`TAMU_AGOL_EntraID.ps1`) was retired on 3 October 2026, after a
  person-by-person comparison with `TAMU_AGOL_EntraID.py` on the same 12,531 members agreed everywhere
  except its two bugs. It is in the git history. The toolchain has no PowerShell now.
- SQL Server (`AGOLCleanup`) holds the catalog until DuckDB replaces it.
- GEO Jobe stays the item-delete tool until the API deleter is proven.

Run Python with ArcGIS Pro's interpreter (`arcgispro-py3`), which has `arcgis`. The Python on
`PATH` does not. Pro must be signed in to the organization as an administrator for `GIS("home")`.

## Git

Commit freely with a message that says why, then **push both repositories yourself** (Dan,
2 October 2026: he wants commits and pushes done for him, not handed over). Before every push of
this public repository, run `python tools/leak_check.py --all` and stop if it fails. Never
force-push, and never push with the hook bypassed.
