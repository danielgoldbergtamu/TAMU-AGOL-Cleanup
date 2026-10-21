"""Copy the SQL Server catalog database into a DuckDB file, and prove the copy matches.

First step of the move to DuckDB (docs/data-model.md, section 7; decided 20 October 2026). Every table is copied
with its column types; the STATUS_CHANGES view is recreated in DuckDB's SQL. Then each table is compared with the
source: row count, and per column the number of non-null values, the sum of numeric values and the total length
of text values. The copy is written to a new file and moved into place only if every check passes.

This only reads SQL Server. It never touches ArcGIS Online. The DuckDB file holds the same named data as the
database: it belongs under data/ (git-ignored), never in a repository.

Usage (from the repo folder):
    python tools/migrate_to_duckdb.py [--out data/agol.duckdb] [--tables NAME ...]
"""
import argparse
import datetime
import json
import os
import sys
import time
import urllib.parse

import duckdb
import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHUNK = 100_000

# STATUS_CHANGES in DuckDB's SQL (the SQL Server version is in private/dashboard/build_dashboard_data.py)
STATUS_CHANGES_VIEW = """
CREATE OR REPLACE VIEW STATUS_CHANGES AS
WITH latest AS (   -- the latest snapshot of each date: a same-day rerun supersedes, without deleting, an earlier one
    SELECT s.* FROM SNAP_MemberStatus s
    WHERE s.run_id = (SELECT MAX(x.run_id) FROM SNAP_MemberStatus x WHERE x.as_of = s.as_of)),
ordered AS (
    SELECT s.*,
           LAG(as_of)            OVER (PARTITION BY username ORDER BY as_of, run_id) AS prev_as_of,
           LAG(classification)   OVER (PARTITION BY username ORDER BY as_of, run_id) AS prev_classification,
           LAG(unit)             OVER (PARTITION BY username ORDER BY as_of, run_id) AS prev_unit,
           LAG(projected_status) OVER (PARTITION BY username ORDER BY as_of, run_id) AS prev_projected_status
    FROM latest s)
SELECT username, prev_as_of AS changed_after, as_of AS changed_by,
       prev_classification, classification, prev_unit, unit, prev_projected_status, projected_status,
       feature_gb, items, run_id
FROM ordered
WHERE prev_as_of IS NOT NULL
  AND (COALESCE(prev_classification, '') <> COALESCE(classification, '')
       OR COALESCE(prev_unit, '') <> COALESCE(unit, '')
       OR COALESCE(prev_projected_status, -1) <> COALESCE(projected_status, -1))
"""

TEXT = {"nvarchar", "varchar", "nchar", "char", "text", "ntext", "sysname", "xml"}
NUMERIC = {"int", "bigint", "smallint", "tinyint", "float", "real", "decimal", "numeric", "money", "smallmoney"}


def duck_type(data_type, precision, scale):
    t = data_type.lower()
    if t in TEXT or t == "uniqueidentifier":
        return "VARCHAR"
    return {"int": "INTEGER", "bigint": "BIGINT", "smallint": "SMALLINT", "tinyint": "SMALLINT", "bit": "BOOLEAN",
            "float": "DOUBLE", "real": "FLOAT", "money": "DECIMAL(19,4)", "smallmoney": "DECIMAL(10,4)",
            "decimal": f"DECIMAL({precision},{scale})", "numeric": f"DECIMAL({precision},{scale})",
            "datetime": "TIMESTAMP", "datetime2": "TIMESTAMP", "smalldatetime": "TIMESTAMP", "date": "DATE",
            "time": "TIME", "datetimeoffset": "TIMESTAMPTZ", "varbinary": "BLOB", "binary": "BLOB",
            "image": "BLOB"}.get(t, "VARCHAR")


def q(name):
    return '"' + name.replace('"', '""') + '"'


def engine_from_env():
    load_dotenv(os.path.join(REPO, ".env"), override=True)
    url = (os.getenv("SQL_CONNECTION_STRING") or "").strip().strip('"').strip("'")
    if not url:
        sys.exit("SQL_CONNECTION_STRING is not set in .env")
    return create_engine(url if "://" in url else "mssql+pyodbc:///?odbc_connect=" + urllib.parse.quote_plus(url))


def profile_sql(table, columns, dialect):
    """One row: COUNT(*), then per column non-null count, numeric sum or total text length."""
    parts = ["COUNT(*)"]
    for name, data_type in columns:
        t = data_type.lower()
        col = f"[{name}]" if dialect == "mssql" else q(name)
        parts.append(f"COUNT({col})")
        if t in NUMERIC or t == "bit":
            expr = f"CAST({col} AS FLOAT)" if dialect == "mssql" else f"CAST({col} AS DOUBLE)"
            parts.append(f"SUM({expr})")
        elif t in TEXT or t == "uniqueidentifier":
            # SQL Server's LEN ignores trailing spaces and counts a character outside the Basic Multilingual Plane
            # (an emoji, say) as 2; measure the same way on the DuckDB side
            parts.append(f"SUM(CAST(LEN({col}) AS BIGINT))" if dialect == "mssql" else
                         rf"SUM(LENGTH(RTRIM({col})) + LENGTH(REGEXP_REPLACE(RTRIM({col}), '[^\x{{10000}}-\x{{10FFFF}}]', '', 'g')))")
        elif t in ("datetime", "datetime2", "smalldatetime", "date"):
            parts.append(f"SUM(CAST(DATEDIFF(day, '2000-01-01', {col}) AS BIGINT))" if dialect == "mssql"
                         else f"SUM(DATE_DIFF('day', DATE '2000-01-01', CAST({col} AS DATE)))")
    src = f"dbo.[{table}]" if dialect == "mssql" else q(table)
    return f"SELECT {', '.join(parts)} FROM {src}"


def check_column(columns, index):
    """(column, data type) that check number index of profile_sql belongs to; (None, None) for the row count."""
    i = 1
    for name, data_type in columns:
        t = data_type.lower()
        span = 2 if (t in NUMERIC or t == "bit" or t in TEXT or t == "uniqueidentifier"
                     or t in ("datetime", "datetime2", "smalldatetime", "date")) else 1
        if i <= index < i + span:
            return name, data_type
        i += span
    return None, None


def close(a, b):
    if a is None or b is None:
        return a is None and b is None or (a in (None, 0) and b in (None, 0))
    a, b = float(a), float(b)
    return abs(a - b) <= max(1e-6, 1e-9 * max(abs(a), abs(b)))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default=os.path.join(REPO, "data", "agol.duckdb"))
    parser.add_argument("--tables", nargs="*", help="only these tables (default: all)")
    args = parser.parse_args()

    engine = engine_from_env()
    with engine.connect() as c:
        tables = [r[0] for r in c.execute(text("SELECT name FROM sys.tables ORDER BY name"))]
        cols = pd.read_sql(text("SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, NUMERIC_PRECISION, NUMERIC_SCALE, ORDINAL_POSITION "
                                "FROM INFORMATION_SCHEMA.COLUMNS ORDER BY TABLE_NAME, ORDINAL_POSITION"), c)
    if args.tables:
        tables = [t for t in tables if t in set(args.tables)]

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    tmp = args.out + ".new"
    for leftover in (tmp, tmp + ".wal"):
        if os.path.exists(leftover):
            os.remove(leftover)
    duck = duckdb.connect(tmp)
    report, failures, started = [], [], time.time()

    for table in tables:
        t0 = time.time()
        tc = cols[cols["TABLE_NAME"] == table]
        columns = list(zip(tc["COLUMN_NAME"], tc["DATA_TYPE"]))
        ddl = ", ".join(f"{q(n)} {duck_type(d, p, s)}" for n, d, p, s in
                        zip(tc["COLUMN_NAME"], tc["DATA_TYPE"], tc["NUMERIC_PRECISION"].fillna(18).astype(int), tc["NUMERIC_SCALE"].fillna(0).astype(int)))
        duck.execute(f"CREATE TABLE {q(table)} ({ddl})")
        select_list = ", ".join(f"[{n}]" for n, _ in columns)
        copied = 0
        with engine.connect() as c:
            for chunk in pd.read_sql(text(f"SELECT {select_list} FROM dbo.[{table}]"), c, chunksize=CHUNK):
                duck.register("chunk", chunk)
                duck.execute(f"INSERT INTO {q(table)} SELECT * FROM chunk")
                duck.unregister("chunk")
                copied += len(chunk)
        with engine.connect() as c:
            src = list(c.execute(text(profile_sql(table, columns, "mssql"))).fetchone())
        dst = list(duck.execute(profile_sql(table, columns, "duckdb")).fetchone())
        bad = [i for i, (a, b) in enumerate(zip(src, dst)) if not close(a, b)]
        # A text-length check can disagree on identical text (SQL Server's LEN counts some characters differently).
        # Then compare that column's actual values, as sorted multisets, and accept only an exact match.
        verified = []
        for i in list(bad):
            name, data_type = check_column(columns, i)
            if name and data_type.lower() in TEXT:
                with engine.connect() as c:
                    a = sorted(v for (v,) in c.execute(text(f"SELECT [{name}] FROM dbo.[{table}] WHERE [{name}] IS NOT NULL")))
                b = sorted(v for (v,) in duck.execute(f"SELECT {q(name)} FROM {q(table)} WHERE {q(name)} IS NOT NULL").fetchall())
                if a == b:
                    bad.remove(i)
                    verified.append(name)
        ok = not bad and len(src) == len(dst)
        report.append({"table": table, "rows": int(src[0]), "copied": copied, "columns": len(columns), "checks": len(src),
                       "ok": ok, "mismatched_checks": bad, "text_compared_value_by_value": verified,
                       "seconds": round(time.time() - t0, 1)})
        print(f"{'ok  ' if ok else 'FAIL'} {table:55} {int(src[0]):>10,} rows  {len(src):>3} checks  {time.time() - t0:6.1f}s", flush=True)
        if not ok:
            failures.append(table)

    if "SNAP_MemberStatus" in tables:
        duck.execute(STATUS_CHANGES_VIEW)
        with engine.connect() as c:
            src_n = c.execute(text("SELECT COUNT(*) FROM dbo.STATUS_CHANGES")).scalar()
        dst_n = duck.execute("SELECT COUNT(*) FROM STATUS_CHANGES").fetchone()[0]
        ok = src_n == dst_n
        report.append({"view": "STATUS_CHANGES", "rows": src_n, "duckdb_rows": dst_n, "ok": ok})
        print(f"{'ok  ' if ok else 'FAIL'} {'STATUS_CHANGES (view)':55} {src_n:>10,} rows (DuckDB {dst_n:,})")
        if not ok:
            failures.append("STATUS_CHANGES")

    duck.execute("CREATE TABLE _migration (migrated_at TIMESTAMP, source VARCHAR, report VARCHAR)")
    duck.execute("INSERT INTO _migration VALUES (?, ?, ?)", [datetime.datetime.now(), "SQL Server AGOLCleanup", json.dumps(report)])
    duck.execute("CHECKPOINT")
    duck.close()

    report_path = os.path.splitext(args.out)[0] + "_migration_report.json"
    json.dump({"migrated_at": datetime.datetime.now().isoformat(timespec="seconds"), "tables": report,
               "failures": failures, "minutes": round((time.time() - started) / 60, 1)}, open(report_path, "w"), indent=1)
    if failures:
        print(f"\n{len(failures)} failed: {failures}. The copy is left at {tmp} for inspection; {args.out} is unchanged.")
        sys.exit(1)
    if os.path.exists(args.out):
        os.replace(args.out, args.out + ".previous")
    os.replace(tmp, args.out)
    size = os.path.getsize(args.out) / 1024 / 1024
    print(f"\nAll {len(report)} objects match. Wrote {args.out} ({size:,.0f} MB) in {(time.time() - started) / 60:.1f} min; report {report_path}")


if __name__ == "__main__":
    main()
