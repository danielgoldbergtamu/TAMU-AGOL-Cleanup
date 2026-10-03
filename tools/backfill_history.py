"""Rebuild first-seen dates (and lost states) in a history table from older snapshots.

Before 3 October 2026, loading a snapshot deleted every history row identical to a newer one, so a row
that had not changed for months kept only its latest date. Older snapshots (database backups, report
CSVs) still show when each state was first seen. For every source row this script fingerprints the
values exactly as TAMU_AGOL_Runs does and then:

  - history row with the same fingerprint: first_seen moves earlier and last_seen later, if the source
    says so. Nothing else changes.
  - no such history row: the state was lost by the old cleanup (or never loaded), so it is added back,
    dated from the source.

Nothing is ever deleted. A source must have every attribute column the history table has, or it is
skipped; run this before the history gains new columns from newer reports.

Usage (ArcGIS Pro clone Python, from the repo folder):
    python tools/backfill_history.py --db AGOLCleanup --history HIST_OrganizationMembers \
        --table "AGOLCleanup-2026-04-28|HIST_OrganizationMembers" \
        --csv P:/Data/ArcGIS-Online-Cleanup/OrganizationMembers_2026-03-30.csv --csv-date 2026-03-30
"""
import argparse
import os
import sys
import urllib.parse

import pandas as pd
from sqlalchemy import create_engine, text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from TAMU_AGOL_Runs import _attribute_columns, _columns, _hash_expression, prepare_history_table  # noqa: E402

SERVER = r"localhost\MSSQLSERVER01"


def engine_for(database):
    odbc = (f"Driver={{ODBC Driver 17 for SQL Server}};Server={SERVER};Database={database};"
            "Trusted_Connection=yes;Encrypt=yes;TrustServerCertificate=yes;")
    return create_engine("mssql+pyodbc:///?odbc_connect=" + urllib.parse.quote_plus(odbc))


def source_columns(connection, database, table):
    rows = connection.execute(text(f"""
        SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, NUMERIC_SCALE
        FROM [{database}].INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = :t ORDER BY ORDINAL_POSITION"""),
        {"t": table}).fetchall()
    out = {}
    for name, dtype, length, precision, scale in rows:
        if dtype in ('nvarchar', 'varchar', 'nchar', 'char', 'varbinary', 'binary'):
            out[name] = f"{dtype}({'max' if length in (-1, None) else length})"
        elif dtype in ('decimal', 'numeric'):
            out[name] = f"{dtype}({precision},{scale})"
        else:
            out[name] = dtype
    return out


def backfill(connection, history, qualified_source, source_types, label, fixed_date=None):
    history_columns = _attribute_columns(_columns(connection, history))
    missing = [n for n, _ in history_columns if n not in source_types]
    if missing:
        print(f"  SKIP {label}: lacks history columns {missing}")
        return 0, 0
    expression = _hash_expression(history_columns, 's', source_types)
    date_sql = f"CAST('{fixed_date}' AS DATE)" if fixed_date else (
        "CAST(s.updated_date AS DATE)" if 'updated_date' in source_types else "CAST(NULL AS DATE)")
    connection.execute(text("IF OBJECT_ID('tempdb..#bf') IS NOT NULL DROP TABLE #bf"))
    connection.execute(text(f"SELECT {expression} AS row_hash, {date_sql} AS seen, s.* INTO #bf FROM {qualified_source} s"))
    connection.execute(text("CREATE INDEX IX_bf ON #bf (row_hash)"))

    widened = connection.execute(text(f"""
        UPDATE h SET first_seen = CASE WHEN b.min_seen IS NOT NULL AND (h.first_seen IS NULL OR b.min_seen < h.first_seen) THEN b.min_seen ELSE h.first_seen END,
                     last_seen  = CASE WHEN b.max_seen IS NOT NULL AND (h.last_seen IS NULL OR b.max_seen > h.last_seen) THEN b.max_seen ELSE h.last_seen END
        FROM dbo.[{history}] h
        JOIN (SELECT row_hash, MIN(seen) AS min_seen, MAX(seen) AS max_seen FROM #bf GROUP BY row_hash) b ON b.row_hash = h.row_hash
        WHERE (b.min_seen IS NOT NULL AND (h.first_seen IS NULL OR b.min_seen < h.first_seen))
           OR (b.max_seen IS NOT NULL AND (h.last_seen IS NULL OR b.max_seen > h.last_seen))""")).rowcount

    names = [n for n, _ in history_columns]
    column_sql = ", ".join(f"[{n}]" for n in names)
    cast_sql = ", ".join(f"TRY_CAST(s.[{n}] AS {t})" if source_types[n] != t else f"s.[{n}]" for n, t in history_columns)
    restored = connection.execute(text(f"""
        INSERT INTO dbo.[{history}] ({column_sql}, updated_date, first_seen, last_seen, first_run_id, last_run_id, row_hash)
        SELECT {cast_sql}, s.max_seen, s.min_seen, s.max_seen, :label, :label, s.row_hash
        FROM (SELECT *, MIN(seen) OVER (PARTITION BY row_hash) AS min_seen, MAX(seen) OVER (PARTITION BY row_hash) AS max_seen,
                     ROW_NUMBER() OVER (PARTITION BY row_hash ORDER BY (SELECT 0)) AS rn FROM #bf) s
        WHERE s.rn = 1 AND NOT EXISTS (SELECT 1 FROM dbo.[{history}] h WHERE h.row_hash = s.row_hash)"""),
        {"label": f"backfill:{label}"[:60]}).rowcount
    connection.execute(text("DROP TABLE #bf"))
    print(f"  {label}: {widened} history rows got earlier/later dates, {restored} lost states restored")
    return widened, restored


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", required=True, help="database holding the history table")
    parser.add_argument("--history", required=True, help="history table, e.g. HIST_OrganizationMembers")
    parser.add_argument("--table", action="append", default=[], help='"database|table" on the same server')
    parser.add_argument("--csv", action="append", default=[], help="report CSV")
    parser.add_argument("--csv-date", action="append", default=[], help="date of the matching --csv (YYYY-MM-DD)")
    args = parser.parse_args()

    engine = engine_for(args.db)
    with engine.begin() as connection:
        before = connection.execute(text(f"SELECT COUNT(*) FROM dbo.[{args.history}]")).scalar()
        prepare_history_table(connection, args.history)
        for spec in args.table:
            database, table = spec.split("|")
            types = source_columns(connection, database, table)
            backfill(connection, args.history, f"[{database}].dbo.[{table}]", types, f"{database}.{table}")
        for i, path in enumerate(args.csv):
            date = args.csv_date[i]
            staging = "BACKFILL_STAGING"
            frame = pd.read_csv(path, dtype=str, keep_default_na=False, na_values=[""])
            connection.execute(text(f"IF OBJECT_ID('dbo.{staging}') IS NOT NULL DROP TABLE dbo.{staging}"))
            frame.to_sql(staging, connection, index=False)
            types = source_columns(connection, args.db, staging)
            backfill(connection, args.history, f"dbo.[{staging}]", types, os.path.basename(path), fixed_date=date)
            connection.execute(text(f"DROP TABLE dbo.{staging}"))
        after = connection.execute(text(f"SELECT COUNT(*) FROM dbo.[{args.history}]")).scalar()
        if after < before:
            raise RuntimeError(f"{args.history} shrank from {before} to {after}; rolling back")
        undated = connection.execute(text(f"SELECT COUNT(*) FROM dbo.[{args.history}] WHERE first_seen IS NULL")).scalar()
        print(f"{args.history}: {before} rows before, {after} after; {undated} rows still without a date")


if __name__ == "__main__":
    main()
