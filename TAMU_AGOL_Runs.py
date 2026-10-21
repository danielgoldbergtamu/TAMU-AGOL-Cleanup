# File Name: TAMU_AGOL_Runs.py
# Description: Run tracking and append-only history, shared by every pipeline script.
# Date: 2026-10-03 (DuckDB since 2026-10-20)
#
# Two promises (Dan, 2 October 2026: "we do not delete prior runs or data when we initiate new runs"):
#
# 1. Every run is recorded and keeps its own files. A run gets an id, a row in the RUNS table (when, where,
#    who, what it used, how it ended) and a folder, reports/runs/<run id>/, for everything it writes. Steps
#    started by TAMU_AGOL_Main.py share the pipeline's id (environment variable AGOL_RUN_ID) and write into
#    sub-folders of its folder.
#
# 2. History is never deleted. Each history row carries a fingerprint (row_hash) of all its values and the
#    dates it was first and last seen. Loading a snapshot extends last_seen on rows whose values are
#    unchanged and adds a row for anything new or changed. This replaced a duplicate-removal query that
#    deleted every row identical to a newer one, which lost how long anything had been unchanged.
#
# The fingerprint is SHA-256 over the row's values as text. SQL Server computed it its own way; DuckDB
# recomputes every history table's fingerprints once (recorded in _meta) so the two never mix.

import datetime
import getpass
import json
import os
import socket

from agol_db import columns as table_columns, connect, exists, q, reports_dir

RUNS_DIR = os.path.join(reports_dir(), 'runs')
HISTORY_META_COLUMNS = ('updated_date', 'first_seen', 'last_seen', 'first_run_id', 'last_run_id', 'row_hash')
HASH_VERSION = 'duckdb-sha256-v1'


# Run tracking
###########################################################################################

def ensure_runs_table(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS RUNS (
            run_id VARCHAR NOT NULL PRIMARY KEY, parent_run_id VARCHAR, kind VARCHAR NOT NULL,
            started_at TIMESTAMP NOT NULL, finished_at TIMESTAMP, status VARCHAR NOT NULL,
            machine VARCHAR, os_user VARCHAR, run_dir VARCHAR, details VARCHAR)""")


class Run:
    """One recorded run. Use as a context manager so a failure is recorded as a failure:

        with Run(con, 'catalog') as run:
            df.to_csv(run.path('members.csv'))
            run.note(member_report='OrganizationMembers_2026-10-02')

    con may be None: the run then opens the database only for a moment to record its start and its end, so
    that a step can hand the database to a sub-process (DuckDB allows one writer at a time).
    """

    def __init__(self, con, kind, dry_run=False):
        self.con = con
        self.kind = kind + ('-dryrun' if dry_run else '')
        self.parent_run_id = os.environ.get('AGOL_RUN_ID') or None
        self.started_at = datetime.datetime.now()
        stamp = self.started_at.strftime('%Y%m%d-%H%M%S')
        if self.parent_run_id:
            self.run_id = f"{self.parent_run_id}.{self.kind}"
            self.dir = os.path.join(RUNS_DIR, self.parent_run_id, self.kind)
        else:
            self.run_id = f"{stamp}-{self.kind}"
            self.dir = os.path.join(RUNS_DIR, self.run_id)
        os.makedirs(self.dir, exist_ok=True)
        self.details = {}
        self._execute(ensure_runs_table)
        self._execute(lambda c: c.execute("INSERT INTO RUNS (run_id, parent_run_id, kind, started_at, status, machine, os_user, run_dir) "
                    "VALUES (?, ?, ?, ?, 'running', ?, ?, ?)",
                    [self.run_id, self.parent_run_id, self.kind, self.started_at, socket.gethostname(), getpass.getuser(), self.dir]))
        print(f"Run {self.run_id} started; files go to {self.dir}")

    def _execute(self, work):
        if self.con is not None:
            return work(self.con)
        with connect() as con:
            return work(con)

    def path(self, filename):
        return os.path.join(self.dir, filename)

    def note(self, **values):
        self.details.update({k: (str(v) if not isinstance(v, (int, float, str, bool, type(None), list, dict)) else v)
                             for k, v in values.items()})

    def finish(self, status='succeeded'):
        self._execute(lambda c: c.execute("UPDATE RUNS SET finished_at = ?, status = ?, details = ? WHERE run_id = ?",
                                          [datetime.datetime.now(), status, json.dumps(self.details, default=str), self.run_id]))
        print(f"Run {self.run_id} {status}.")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.note(error=f"{exc_type.__name__}: {exc}")
        self.finish('failed' if exc_type else 'succeeded')
        return False


# Append-only history
###########################################################################################

def _attribute_columns(cols):
    return [(n, t) for n, t in cols if n not in HISTORY_META_COLUMNS]


def _hash_expression(history_columns, alias, source_types):
    """SHA-256 (hex) over every non-meta history column, in name order, each value cast to the history column's
    type and then to text, joined by a control character; NULL is written <NULL>."""
    parts = []
    for name, sql_type in sorted(history_columns, key=lambda c: c[0]):
        src = source_types.get(name)
        ref = "NULL" if src is None else f"{alias}.{q(name)}"
        if src is not None and src != sql_type:
            ref = f"TRY_CAST({ref} AS {sql_type})"
        parts.append(f"COALESCE(CAST({ref} AS VARCHAR), '<NULL>')")
    return f"sha256(concat_ws(chr(31), {', '.join(parts)}))"


def _meta(con, key, value=None):
    con.execute("CREATE TABLE IF NOT EXISTS _meta (key VARCHAR PRIMARY KEY, value VARCHAR)")
    if value is None:
        row = con.execute("SELECT value FROM _meta WHERE key = ?", [key]).fetchone()
        return row[0] if row else None
    con.execute("INSERT OR REPLACE INTO _meta VALUES (?, ?)", [key, value])


def prepare_history_table(con, history_table):
    """Make sure a history table has its meta columns, and fingerprints computed this way (HASH_VERSION).
    Rows that never had dates get first_seen = last_seen = updated_date. Safe to call every run."""
    existing = dict(table_columns(con, history_table))
    for column, sql_type in (('first_seen', 'DATE'), ('last_seen', 'DATE'), ('first_run_id', 'VARCHAR'),
                             ('last_run_id', 'VARCHAR'), ('row_hash', 'VARCHAR')):
        if column not in existing:
            con.execute(f"ALTER TABLE {q(history_table)} ADD COLUMN {q(column)} {sql_type}")
    if existing.get('row_hash', 'VARCHAR') != 'VARCHAR':   # SQL Server's VARBINARY, carried over as BLOB
        con.execute(f"ALTER TABLE {q(history_table)} ALTER row_hash TYPE VARCHAR USING NULL")
    if _meta(con, f"row_hash:{history_table}") == HASH_VERSION:
        return False
    cols = _attribute_columns(table_columns(con, history_table))
    expression = _hash_expression(cols, 'h', dict(cols))
    has_updated = 'updated_date' in existing
    con.execute(f"""
        UPDATE {q(history_table)} AS h SET row_hash = {expression},
               first_seen = COALESCE(first_seen, {'CAST(updated_date AS DATE)' if has_updated else 'NULL'}),
               last_seen  = COALESCE(last_seen,  {'CAST(updated_date AS DATE)' if has_updated else 'NULL'}),
               first_run_id = COALESCE(first_run_id, 'before-run-tracking'),
               last_run_id  = COALESCE(last_run_id, 'before-run-tracking')""")
    _meta(con, f"row_hash:{history_table}", HASH_VERSION)
    print(f"  {history_table}: fingerprints computed ({HASH_VERSION}).")
    return True


def merge_snapshot_into_history(con, history_table, snapshot_table, run_id, seen_date):
    """Adds a snapshot table to its history table without deleting anything.

    Rows whose values match an existing history row extend that row's last_seen; anything new or changed
    becomes a new row. Columns the snapshot has and the history lacks are added to the history first.
    Returns (rows extended, rows added)."""
    if not exists(con, history_table):
        con.execute(f"CREATE TABLE {q(history_table)} AS SELECT * FROM {q(snapshot_table)} LIMIT 0")
    prepare_history_table(con, history_table)

    snapshot_columns = _attribute_columns(table_columns(con, snapshot_table))
    history_types = dict(table_columns(con, history_table))
    for name, sql_type in snapshot_columns:
        if name not in history_types:
            con.execute(f"ALTER TABLE {q(history_table)} ADD COLUMN {q(name)} {sql_type}")
            print(f"  {history_table}: added new column {name} from {snapshot_table}")

    history_columns = _attribute_columns(table_columns(con, history_table))
    snapshot_types = dict(snapshot_columns)
    con.execute(f"CREATE OR REPLACE TEMP TABLE _snap AS SELECT {_hash_expression(history_columns, 's', snapshot_types)} AS row_hash, s.* "
                f"FROM {q(snapshot_table)} s")

    extended = con.execute(f"""
        UPDATE {q(history_table)} SET
               last_seen = CASE WHEN last_seen IS NULL OR last_seen < ? THEN ? ELSE last_seen END,
               first_seen = CASE WHEN first_seen IS NULL OR first_seen > ? THEN ? ELSE first_seen END,
               updated_date = CASE WHEN updated_date IS NULL OR updated_date < ? THEN ? ELSE updated_date END,
               last_run_id = ?
        WHERE row_hash IN (SELECT row_hash FROM _snap)""",
        [seen_date, seen_date, seen_date, seen_date, seen_date, seen_date, run_id]).fetchone()[0]

    insert_columns = [n for n, _ in history_columns if n in snapshot_types]
    column_sql = ", ".join(q(n) for n in insert_columns)
    source_sql = ", ".join(f"s.{q(n)}" for n in insert_columns)
    added = con.execute(f"""
        INSERT INTO {q(history_table)} ({column_sql}, updated_date, first_seen, last_seen, first_run_id, last_run_id, row_hash)
        SELECT {source_sql}, ?, ?, ?, ?, ?, s.row_hash
        FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY row_hash) AS rn FROM _snap) s
        WHERE s.rn = 1 AND s.row_hash NOT IN (SELECT row_hash FROM {q(history_table)} WHERE row_hash IS NOT NULL)""",
        [seen_date, seen_date, seen_date, run_id, run_id]).fetchone()[0]
    con.execute("DROP TABLE _snap")
    print(f"  {history_table}: {extended} rows still current (last_seen extended), {added} new or changed rows added")
    return extended, added
