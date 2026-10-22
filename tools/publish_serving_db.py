"""Publishes the website's read-only copy of the database: only the tables the site reads, into the site folder.

The daily job runs this last, after the dashboards are built. The website (web/app.py) opens the copy read-only;
it never sees the job's own database, which only the job may write (DuckDB allows a single writer).

What goes in: the snapshots (SNAP_*), the newest member and item reports, RUNS, the person tables the dashboard
names people from (HIST_Supervisors, HIST_AccountNetIDs, HIST_PersonNames), the account pages the builder writes
(DASH_*), plus the STATUS_CHANGES view. The rest of the history, and the raw directory and Entra lookups, stay with the job.

The copy is written beside its final name and moved into place, so the site never opens a half-written file.

    python tools/publish_serving_db.py            # writes <AGOL_OUTPUT_DIR>/agol-serve.duckdb
"""
import argparse
import os
import sys

import duckdb

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(REPO, ".env"), override=False)
import agol_db as db  # noqa: E402
sys.path.insert(0, os.path.join(REPO, "dashboard"))
from build_dashboard_data import STATUS_CHANGES_VIEW  # noqa: E402

SERVE_NAME = "agol-serve.duckdb"
TABLES = ["SNAP_MemberStatus", "SNAP_UnitUsage", "SNAP_RemovalSummary", "RUNS",
          "HIST_Supervisors", "HIST_AccountNetIDs", "HIST_PersonNames", "_meta",
          # the account pages, written by dashboard/build_full_dashboard.py
          "DASH_Account", "DASH_AccountItems", "DASH_Names", "DASH_ArcgisLogins", "DASH_PersonOf", "DASH_GroupList", "DASH_ViewData", "DASH_Scope", "DASH_Item"]
NEWEST = ["OrganizationMembers_", "OrganizationItems_"]


def main():
    parser = argparse.ArgumentParser(description="Publish the website's read-only copy of the database.")
    parser.add_argument("--out", help=f"file to write (default: <AGOL_OUTPUT_DIR>/{SERVE_NAME})")
    args = parser.parse_args()
    dest = args.out or os.path.join(db.output_dir(), SERVE_NAME)
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    tmp = dest + ".tmp"
    for leftover in (tmp, tmp + ".wal"):
        if os.path.exists(leftover):
            os.remove(leftover)

    with db.connect(read_only=True) as con:
        names = [t for t in TABLES if db.exists(con, t)] + [n for n in (db.newest(con, p) for p in NEWEST) if n]
    # an in-memory connection reads the job's database (read-only) and writes the copy
    with duckdb.connect() as con:
        con.execute(f"ATTACH '{db.db_path()}' AS job (READ_ONLY)")
        con.execute(f"ATTACH '{tmp}' AS serve")
        for name in names:
            con.execute(f"CREATE TABLE serve.{db.q(name)} AS SELECT * FROM job.{db.q(name)}")
        con.execute("USE serve")
        con.execute(STATUS_CHANGES_VIEW)
        con.execute("USE memory")
        con.execute("DETACH serve")
    os.replace(tmp, dest)
    print(f"published {dest}: {len(names)} tables ({', '.join(names)}), {os.path.getsize(dest) / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
