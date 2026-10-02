# Method: cleaning up a university ArcGIS Online organization

How Texas A&M approached getting its ArcGIS Online (AGOL) organization back under its license
limits, written so another institution can follow it. Counts are Texas A&M's. No individual is
named anywhere in this repository.

## The problem

Esri stopped letting university organizations run over their limits. An organization still over at
renewal has no credits for storage or processing, which in practice switches AGOL off for everyone
on the license.

| Limit | Allowed | Texas A&M, early 2026 | Texas A&M, 1 October 2026 |
|---|---|---|---|
| Feature storage | 500 GB | 1.5–1.8 TB (three documents disagreed) | not yet re-measured |
| User accounts | 15,000 | 15,732 | 12,531 |

Most of the excess belonged to people who had left: students who graduated, staff who moved on.
They cannot be asked, and the institution usually cannot tell who should inherit their work.

## The rules shown to users

See [usage-rules.md](usage-rules.md). The core of it: AGOL is for short-term projects, not
long-term storage. The institution owns and manages what is published there, and keeping your own
data somewhere else is your responsibility.

## Classes of content, from safest to remove to most careful

Texas A&M's plan sorted the excess into five classes. Each gets a different level of care.

| # | Class | Texas A&M storage | Care |
|---|---|---|---|
| 1 | Private items whose owner has no active institutional account | 136 GB | Nobody but an admin can reach them. Remove after backup |
| 2 | Private items of recent alumni who still have a working address | 219 GB | Owner notified first |
| 3 | Shared (public or organization) items whose owner has no active account | 300 GB | Check dependencies. Anyone whose map, app or group uses it gets a week to respond |
| 4 | Shared items of recent alumni | 140 GB | Same dependency check and one-week window |
| 5 | Private items of **current** users not accessed in two years | 76 GB | Owner notified, removed after a week without reply |

Separately: **accounts that own nothing** (6,490 at the start) can be removed outright. They hold
nothing to back up, and a single sign-on account is recreated the next time its owner logs in.

## The process

1. **Snapshot.** Pull the organization's member and item reports. AGOL can generate them on a
   schedule, and `TAMU_AGOL_Catalog.py` loads the newest into a database with history tables, so
   every change is kept.
2. **Identity.** Look up every member in the identity provider (Entra ID at Texas A&M): are they
   still here, what is their department, and **who is their supervisor**. Do this on every
   snapshot, because the answers disappear once a person leaves. See "Supervisors" below.
3. **Select.** The queries in [`../sql/`](../sql/) turn the snapshot and the identity lookup into
   candidate lists, one per class.
4. **Notify.** Owners, and for employees their supervisor, are told what will happen and when.
   Students are emailed at the end of each semester to collect their own data.
5. **Deprecate.** Set the content private. It keeps its links inside maps and groups (moving it to
   a "holding pen" account would break them) and stops being visible. View dates then show whether
   anything still depends on it.
6. **Back up, then verify the backup.** Nothing with content is deleted before a verified backup
   exists. Texas A&M's backup is a standalone ArcGIS Enterprise portal (the "lifeboat") filled by a
   backup product.
7. **Delete**, with a dry run first, a protected list (administrators, the operator), and a results
   file naming every account or item and what happened to it.
8. **Repeat**, on a schedule. A cleanup that is not automated is one somebody has to remember to do
   again next year.

## Supervisors

Before a departed person's content is removed, their supervisor is the one told to collect what
the unit needs. The identity provider returns a supervisor for every employee, student employees
included, **but only while they are still employed**. Afterwards the field is empty, or the
account is gone.

So the lookup runs on every snapshot, and every supervisor ever seen is kept in a history table
(`HIST_Supervisors`, built by [`../tools/build_supervisor_history.py`](../tools/build_supervisor_history.py)).
A later blank never overwrites an earlier supervisor.

At Texas A&M, lookups only began in April 2026. Of 3,133 content owners who had already left by
July, 38 had a supervisor on record. **Start capturing supervisors before you need them.**

## The periodic process

The end state is a nightly or weekly job, not a one-off:

- a catalog of members, groups and items, each with a history table;
- a status code per member (0: still affiliated; 1: left, owner and supervisor notified; 2: past
  the notice period, removed), as in `TAMU_AGOL_DeleteStatus.py`;
- deprecation before deletion;
- an automatic email to anyone who uploads something very large, pointing at the usage rules.
