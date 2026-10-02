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

## Toolchain direction

The target is **Python only**. The ArcGIS API for Python handles exports and deletes, Microsoft
Graph is called from Python for identity, and DuckDB holds the catalog and queries. For now:

- `TAMU_AGOL_EntraID.ps1` is the last PowerShell step. **It downloads the whole tenant on every
  run** (`Get-MgUser -All`). Its replacement looks up only AGOL members, batched.
- SQL Server (`AGOLCleanup`) holds the catalog until DuckDB replaces it.
- GEO Jobe stays the item-delete tool until the API deleter is proven.

Run Python with ArcGIS Pro's interpreter (`arcgispro-py3`), which has `arcgis`. The Python on
`PATH` does not. Pro must be signed in to the organization as an administrator for `GIS("home")`.

## Git

Commit freely with a message that says why. **Pushing is the maintainer's**: hand over the push
command and don't run it.
