"""Writes the Lifeboat check's results into one file for the cluster, where the review page shows them.

Why: the check (tools/lifeboat_check.py) runs on the maintainer's workstation, which can reach the Lifeboat; the site
runs on the cluster. This copies what the review page needs from the check's own database into a small DuckDB
file, which kubernetes-config's 16-send-lifeboat-check.ps1 puts in the cluster's Lifeboat inbox. The next daily
run (or a dashboards-only run) adds the rows it does not have yet to its database (pipeline_daily.lifeboat_results).

What goes in, every row so far (the cluster keeps only rows it does not already have; nothing is replaced):
  LIFEBOAT_Check           one row per item per check
  AGOL_Group               the ArcGIS Online groups those items are shared with, and their members
  LIFEBOAT_Runs            one row per check run
  LIFEBOAT_RestoredYears   the reviewed list of years restored to the Lifeboat (private config,
                           lifeboat_restored_years.csv: year, restored_on, said_by); items of other years are never
                           offered for deletion, whatever their check says

The file names people and items: it goes only to the cluster, never into a repository or an email.

Usage (ArcGIS Pro clone Python):
    python tools/lifeboat_export.py --restored-years C:\\TAMU\\TAMU-AGOL-Cleanup\\private\\config\\lifeboat_restored_years.csv
"""
import argparse
import csv
import datetime
import os
import sys

import duckdb

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import agol_db as db  # noqa: E402

TABLES = ["LIFEBOAT_Check", "AGOL_Group", "LIFEBOAT_Runs"]


def restored_years(path):
    """The reviewed list, checked: a year, the date it was restored (or said to be), and who said so."""
    rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
    if not rows or not {"year", "restored_on", "said_by"} <= set(rows[0]):
        raise SystemExit(f"{path}: needs the columns year, restored_on, said_by. Nothing was written.")
    for r in rows:
        if not r["year"].strip().isdigit() or not r["said_by"].strip():
            raise SystemExit(f"{path}: every row needs a year and who said it was restored ({r}). Nothing was written.")
    return [(int(r["year"]), r["restored_on"].strip(), r["said_by"].strip()) for r in rows]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default=os.path.join(db.SCRIPT_DIR, "data", "lifeboat-check.duckdb"))
    ap.add_argument("--restored-years", required=True, help="the reviewed lifeboat_restored_years.csv")
    ap.add_argument("--out", help="file to write (default data/lifeboat-outbox/lifeboat-check_<date-time>.duckdb)")
    args = ap.parse_args()
    years = restored_years(args.restored_years)
    out = args.out or os.path.join(db.SCRIPT_DIR, "data", "lifeboat-outbox", f"lifeboat-check_{datetime.datetime.now():%Y%m%d-%H%M%S}.duckdb")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    if os.path.exists(out):
        raise SystemExit(f"{out} exists already. Nothing was written.")
    with duckdb.connect(out) as con:
        con.execute(f"ATTACH '{args.db}' AS src (READ_ONLY)")
        for t in TABLES:
            if con.execute("SELECT COUNT(*) FROM duckdb_tables() WHERE database_name = 'src' AND table_name = ?", [t]).fetchone()[0]:
                con.execute(f"CREATE TABLE {db.q(t)} AS SELECT * FROM src.{db.q(t)}")
        con.execute("CREATE TABLE LIFEBOAT_RestoredYears (year INTEGER, restored_on VARCHAR, said_by VARCHAR, exported_at TIMESTAMP)")
        now = datetime.datetime.now()
        con.executemany("INSERT INTO LIFEBOAT_RestoredYears VALUES (?, ?, ?, ?)", [(y, o, s, now) for y, o, s in years])
        counts = {t: con.execute(f"SELECT COUNT(*) FROM {db.q(t)}").fetchone()[0]
                  for (t,) in con.execute("SELECT table_name FROM duckdb_tables() WHERE database_name = current_database()").fetchall()}
        con.execute("DETACH src")
    print(f"wrote {out}: {counts}; restored years {[y for y, _, _ in years]}")
    print(out)   # the last line is the file, for the hand-over script


if __name__ == "__main__":
    main()
