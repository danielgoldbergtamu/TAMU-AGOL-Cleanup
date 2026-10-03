# File Name: TAMU_AGOL_Runs.py
# Description: Run tracking and append-only history, shared by every pipeline script.
# Date: 2026-10-03
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

import datetime
import getpass
import json
import os
import socket

from sqlalchemy import text

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RUNS_DIR = os.path.join(SCRIPT_DIR, 'reports', 'runs')
HISTORY_META_COLUMNS = ('updated_date', 'first_seen', 'last_seen', 'first_run_id', 'last_run_id', 'row_hash')


# Run tracking
###########################################################################################

def ensure_runs_table(engine):
    with engine.begin() as connection:
        connection.execute(text("""
            IF OBJECT_ID('dbo.RUNS') IS NULL
            CREATE TABLE dbo.RUNS (
                run_id        NVARCHAR(60)  NOT NULL PRIMARY KEY,
                parent_run_id NVARCHAR(60)  NULL,
                kind          NVARCHAR(60)  NOT NULL,
                started_at    DATETIME2     NOT NULL,
                finished_at   DATETIME2     NULL,
                status        NVARCHAR(20)  NOT NULL,
                machine       NVARCHAR(100) NULL,
                os_user       NVARCHAR(100) NULL,
                run_dir       NVARCHAR(400) NULL,
                details       NVARCHAR(MAX) NULL)"""))


class Run:
    """One recorded run. Use as a context manager so a failure is recorded as a failure:

        with Run(engine, 'catalog') as run:
            df.to_csv(run.path('members.csv'))
            run.note(member_report='OrganizationMembers_2026-10-02')
    """

    def __init__(self, engine, kind, dry_run=False):
        self.engine = engine
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
        ensure_runs_table(engine)
        with engine.begin() as connection:
            connection.execute(text("""
                INSERT INTO dbo.RUNS (run_id, parent_run_id, kind, started_at, status, machine, os_user, run_dir)
                VALUES (:id, :parent, :kind, :started, 'running', :machine, :user, :dir)"""),
                {"id": self.run_id, "parent": self.parent_run_id, "kind": self.kind, "started": self.started_at,
                 "machine": socket.gethostname(), "user": getpass.getuser(), "dir": self.dir})
        print(f"Run {self.run_id} started; files go to {self.dir}")

    def path(self, filename):
        return os.path.join(self.dir, filename)

    def note(self, **values):
        self.details.update({k: (str(v) if not isinstance(v, (int, float, str, bool, type(None), list, dict)) else v)
                             for k, v in values.items()})

    def finish(self, status='succeeded'):
        with self.engine.begin() as connection:
            connection.execute(text("UPDATE dbo.RUNS SET finished_at = :f, status = :s, details = :d WHERE run_id = :id"),
                               {"f": datetime.datetime.now(), "s": status,
                                "d": json.dumps(self.details, default=str), "id": self.run_id})
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

def _columns(connection, table):
    """[(name, sql_type)] for a table, in column order, with full type text for CAST."""
    rows = connection.execute(text("""
        SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, NUMERIC_SCALE
        FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = :t ORDER BY ORDINAL_POSITION"""), {"t": table}).fetchall()
    out = []
    for name, dtype, length, precision, scale in rows:
        if dtype in ('nvarchar', 'varchar', 'nchar', 'char', 'varbinary', 'binary'):
            sql_type = f"{dtype}({'max' if length in (-1, None) else length})"
        elif dtype in ('decimal', 'numeric'):
            sql_type = f"{dtype}({precision},{scale})"
        else:
            sql_type = dtype
        out.append((name, sql_type))
    return out


def _value_text(column, sql_type, source_alias, source_type):
    """Text of one value for the fingerprint, cast to the history column's type first so a snapshot
    and the history agree however each stored it. Floats and dates use full-precision styles."""
    ref = f"{source_alias}.[{column}]"
    if source_type is None:
        ref = "NULL"
    elif source_type != sql_type:
        ref = f"TRY_CAST({ref} AS {sql_type})"
    base = sql_type.split('(')[0]
    if base in ('float', 'real'):
        value = f"CONVERT(NVARCHAR(60), {ref}, 3)"
    elif base in ('datetime', 'datetime2', 'date', 'smalldatetime', 'datetimeoffset'):
        value = f"CONVERT(NVARCHAR(40), {ref}, 126)"
    else:
        value = f"CAST({ref} AS NVARCHAR(MAX))"
    return f"ISNULL({value}, N'<NULL>')"


def _hash_expression(history_columns, source_alias, source_types):
    """SHA-256 over every non-meta history column, in name order, joined by a control character."""
    parts = [_value_text(name, sql_type, source_alias, source_types.get(name))
             for name, sql_type in sorted(history_columns, key=lambda c: c[0])]
    return f"HASHBYTES('SHA2_256', CONCAT_WS(NCHAR(31), {', '.join(parts)}))"


def _attribute_columns(columns):
    return [(n, t) for n, t in columns if n not in HISTORY_META_COLUMNS]


def prepare_history_table(connection, history_table):
    """One-time upgrade of an existing history table: add first_seen/last_seen/run ids/row_hash and fill
    them for the rows already there (first_seen = last_seen = updated_date, the only date they carry).
    Safe to call every run: it does nothing once the columns exist."""
    existing = {n for n, _ in _columns(connection, history_table)}
    if 'row_hash' in existing:
        return False
    for column, sql_type in (('first_seen', 'DATE'), ('last_seen', 'DATE'), ('first_run_id', 'NVARCHAR(60)'),
                             ('last_run_id', 'NVARCHAR(60)'), ('row_hash', 'VARBINARY(32)')):
        if column not in existing:
            connection.execute(text(f"ALTER TABLE dbo.[{history_table}] ADD [{column}] {sql_type} NULL"))
    columns = _attribute_columns(_columns(connection, history_table))
    types = dict(columns)
    expression = _hash_expression(columns, 'h', types)
    has_updated = 'updated_date' in existing
    connection.execute(text(f"""
        UPDATE h SET row_hash = {expression},
                     first_seen = {'CAST(h.updated_date AS DATE)' if has_updated else 'NULL'},
                     last_seen  = {'CAST(h.updated_date AS DATE)' if has_updated else 'NULL'},
                     first_run_id = 'before-run-tracking', last_run_id = 'before-run-tracking'
        FROM dbo.[{history_table}] h"""))
    connection.execute(text(f"CREATE INDEX [IX_{history_table}_row_hash] ON dbo.[{history_table}] (row_hash)"))
    print(f"Upgraded {history_table} to first/last-seen history.")
    return True


def merge_snapshot_into_history(connection, history_table, snapshot_table, run_id, seen_date):
    """Adds a snapshot table to its history table without deleting anything.

    Rows whose values match an existing history row extend that row's last_seen; anything new or changed
    becomes a new row. Columns the snapshot has and the history lacks are added to the history first.
    Returns (rows extended, rows added)."""
    if connection.execute(text(f"SELECT OBJECT_ID('dbo.[{history_table}]')")).scalar() is None:
        connection.execute(text(f"SELECT TOP 0 * INTO dbo.[{history_table}] FROM dbo.[{snapshot_table}]"))
        prepare_history_table(connection, history_table)
    else:
        prepare_history_table(connection, history_table)

    snapshot_columns = _attribute_columns(_columns(connection, snapshot_table))
    history_names = {n for n, _ in _columns(connection, history_table)}
    for name, sql_type in snapshot_columns:
        if name not in history_names:
            connection.execute(text(f"ALTER TABLE dbo.[{history_table}] ADD [{name}] {sql_type} NULL"))
            print(f"  {history_table}: added new column [{name}] from {snapshot_table}")

    history_columns = _attribute_columns(_columns(connection, history_table))
    snapshot_types = dict(snapshot_columns)
    expression = _hash_expression(history_columns, 's', snapshot_types)

    connection.execute(text("IF OBJECT_ID('tempdb..#snap') IS NOT NULL DROP TABLE #snap"))
    connection.execute(text(f"SELECT {expression} AS row_hash, s.* INTO #snap FROM dbo.[{snapshot_table}] s"))
    connection.execute(text("CREATE INDEX IX_snap_hash ON #snap (row_hash)"))

    extended = connection.execute(text(f"""
        UPDATE h SET last_seen = CASE WHEN h.last_seen IS NULL OR h.last_seen < :d THEN :d ELSE h.last_seen END,
                     first_seen = CASE WHEN h.first_seen IS NULL OR h.first_seen > :d THEN :d ELSE h.first_seen END,
                     updated_date = CASE WHEN h.updated_date IS NULL OR h.updated_date < :d THEN :d ELSE h.updated_date END,
                     last_run_id = :run
        FROM dbo.[{history_table}] h
        WHERE h.row_hash IN (SELECT row_hash FROM #snap)"""), {"d": seen_date, "run": run_id}).rowcount

    insert_columns = [n for n, _ in history_columns if n in snapshot_types]
    column_sql = ", ".join(f"[{n}]" for n in insert_columns)
    source_sql = ", ".join(f"s.[{n}]" for n in insert_columns)
    added = connection.execute(text(f"""
        INSERT INTO dbo.[{history_table}] ({column_sql}, updated_date, first_seen, last_seen, first_run_id, last_run_id, row_hash)
        SELECT {source_sql}, :d, :d, :d, :run, :run, s.row_hash
        FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY row_hash ORDER BY (SELECT 0)) AS rn FROM #snap) s
        WHERE s.rn = 1 AND NOT EXISTS (SELECT 1 FROM dbo.[{history_table}] h WHERE h.row_hash = s.row_hash)"""),
        {"d": seen_date, "run": run_id}).rowcount
    connection.execute(text("DROP TABLE #snap"))
    print(f"  {history_table}: {extended} rows still current (last_seen extended), {added} new or changed rows added")
    return extended, added
