"""The daily job: refresh everything from ArcGIS Online and rebuild the dashboards. Read-only in ArcGIS Online.

Steps, each its own process (DuckDB has one writer at a time; each step opens the database, writes, and closes):

  0. read-only check   stops the run unless the service account can only view (tools/check_read_only.py); each
                       later step that signs in checks again
  1. group inventory   which items are shared to which groups (tools/group_inventory.py)
  2. Entra lookup      the newest lookup file left in the inbox (AGOL_ENTRA_INBOX, default data/entra-inbox), its
                       history recorded once; if there is none, the lookup already in the database is used again.
                       (Until TAMU IT grants an app registration, the lookup needs a person's Microsoft sign-in, so
                       it is run on a workstation: python TAMU_AGOL_EntraID.py ... --no-history, and the .csv and
                       .people.csv it writes are copied to the inbox.)
  3. catalog           the newest AGOL reports, the directory (MQS) lookup, history (TAMU_AGOL_Catalog.py)
  4. delete status     a DRY RUN only: projected statuses, nothing saved (TAMU_AGOL_DeleteStatus.py --dry-run)
  5. removal summary   counts and review lists (tools/removal_summary.py)
  6. snapshot          one row per member for this report date (dashboard/build_dashboard_data.py); a rerun on the
                       same date adds another, and the newest is the one shown
  7. dashboards        both editions' data, into the database (DASH_* tables) (dashboard/build_full_dashboard.py)
  8. publish-db        the website's read-only copy of the tables it reads (tools/publish_serving_db.py); before it,
                       the Lifeboat check's results left in the Lifeboat inbox (AGOL_LIFEBOAT_INBOX, default
                       data/lifeboat-inbox; tools/lifeboat_export.py on the workstation) are added, for the review page

Nothing here changes or removes anything in ArcGIS Online, and it never saves a delete status: removals stay a
separate, manual step after the maintainer's written approval (CLAUDE.md, "Deleting").

Usage:
    python pipeline_daily.py [--skip-groups] [--members-csv F --items-csv F] [--mqs-csv F]   (files: for tests)
    python pipeline_daily.py --dashboards-only    only steps 7 and 8, from what the database already holds (and
                                                  the Lifeboat inbox)
"""
import argparse
import datetime
import glob
import hashlib
import json
import os
import subprocess
import sys

from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, ".env"), override=True)

import agol_db as db  # noqa: E402
from TAMU_AGOL_Runs import Run  # noqa: E402


def step(name, args, run):
    """Runs one step as its own process, its output in <run>/<name>.log; stops the job if it fails."""
    log_path = run.path(f"{name}.log")
    print(f"[{datetime.datetime.now():%H:%M:%S}] {name}: {' '.join(os.path.basename(a) if i == 0 else a for i, a in enumerate(args))}", flush=True)
    with open(log_path, "w", encoding="utf-8") as log:
        result = subprocess.run([sys.executable, "-u", *args], cwd=SCRIPT_DIR, stdout=log, stderr=subprocess.STDOUT)
    if result.returncode != 0:
        tail = open(log_path, encoding="utf-8", errors="replace").read()[-1500:]
        raise RuntimeError(f"step '{name}' failed (exit {result.returncode}); see {log_path}\n{tail}")


def entra_lookup(run):
    """The Entra lookup file for this run: the newest one in the inbox (its history recorded the first time it is
    seen), else the lookup already loaded, written out again."""
    inbox = os.environ.get("AGOL_ENTRA_INBOX") or os.path.join(SCRIPT_DIR, "data", "entra-inbox")
    found = sorted((p for p in glob.glob(os.path.join(inbox, "*.csv")) if not p.endswith(".people.csv")), key=os.path.getmtime)
    if found:
        path = found[-1]
        digest = hashlib.sha256(open(path, "rb").read()).hexdigest()[:16]
        with db.connect() as con:
            con.execute("CREATE TABLE IF NOT EXISTS _meta (key VARCHAR PRIMARY KEY, value VARCHAR)")
            seen = con.execute("SELECT value FROM _meta WHERE key = ?", [f"entra_ingested:{digest}"]).fetchone()
        if not seen:
            from TAMU_AGOL_EntraID import record_history_from_files
            record_history_from_files(path)
            with db.connect() as con:
                con.execute("INSERT OR REPLACE INTO _meta VALUES (?, ?)", [f"entra_ingested:{digest}", f"{path} at {datetime.datetime.now():%Y-%m-%d %H:%M}"])
            print(f"recorded the history of {os.path.basename(path)}")
        run.note(entra_lookup=path, entra_new=not seen)
        return path
    path = run.path("AGOL_EntraID_Status_previous.csv")
    with db.connect() as con:
        if not db.exists(con, "AGOL_EntraID_Status"):
            raise RuntimeError(f"no Entra lookup in {inbox} and none in the database yet")
        con.execute(f"COPY (SELECT * FROM AGOL_EntraID_Status) TO '{path.replace(chr(39), chr(39) * 2)}' (HEADER, DELIMITER ',')")
    run.note(entra_lookup="the lookup already in the database (no new file in the inbox)")
    print("no new Entra lookup in the inbox: using the one already loaded")
    return path


# what each table's rows are told apart by: a row the database already has is never written again or replaced
LIFEBOAT_KEYS = {"LIFEBOAT_Check": ["run_id", "item_id"], "AGOL_Group": ["run_id", "group_id"], "LIFEBOAT_Runs": ["run_id"],
                 "LIFEBOAT_RestoredYears": ["exported_at", "year"]}


def lifeboat_results(run):
    """The Lifeboat check's results (8 November 2026): each file in the inbox is taken once, and only the rows the
    database does not have yet are added. Nothing is replaced or deleted, so the history of every check is kept.
    The check itself runs on the workstation (tools/lifeboat_check.py), which can reach the Lifeboat."""
    inbox = os.environ.get("AGOL_LIFEBOAT_INBOX") or os.path.join(SCRIPT_DIR, "data", "lifeboat-inbox")
    found = sorted(glob.glob(os.path.join(inbox, "*.duckdb")), key=os.path.getmtime)
    taken = []
    for path in found:
        digest = hashlib.sha256(open(path, "rb").read()).hexdigest()[:16]
        with db.connect() as con:
            con.execute("CREATE TABLE IF NOT EXISTS _meta (key VARCHAR PRIMARY KEY, value VARCHAR)")
            if con.execute("SELECT 1 FROM _meta WHERE key = ?", [f"lifeboat_ingested:{digest}"]).fetchone():
                continue
            con.execute(f"ATTACH '{path.replace(chr(39), chr(39) * 2)}' AS lb (READ_ONLY)")
            added = {}
            try:
                for t, keys in LIFEBOAT_KEYS.items():
                    if not con.execute("SELECT COUNT(*) FROM duckdb_tables() WHERE database_name = 'lb' AND table_name = ?", [t]).fetchone()[0]:
                        continue
                    # not db.exists: with the inbox file attached, that finds its table as well
                    if not con.execute("SELECT COUNT(*) FROM duckdb_tables() WHERE database_name = current_database() AND table_name = ?", [t]).fetchone()[0]:
                        con.execute(f"CREATE TABLE {db.q(t)} AS SELECT * FROM lb.{db.q(t)} LIMIT 0")
                    have = {c for (c,) in con.execute(f"SELECT column_name FROM duckdb_columns() WHERE database_name = current_database() AND table_name = ?", [t]).fetchall()}
                    for c, typ in con.execute("SELECT column_name, data_type FROM duckdb_columns() WHERE database_name = 'lb' AND table_name = ?", [t]).fetchall():
                        if c not in have:   # a column a newer check added
                            con.execute(f"ALTER TABLE {db.q(t)} ADD COLUMN {db.q(c)} {typ}")
                    on = " AND ".join(f"o.{db.q(k)} IS NOT DISTINCT FROM n.{db.q(k)}" for k in keys)
                    before = con.execute(f"SELECT COUNT(*) FROM {db.q(t)}").fetchone()[0]
                    con.execute(f"INSERT INTO {db.q(t)} BY NAME SELECT n.* FROM lb.{db.q(t)} n WHERE NOT EXISTS (SELECT 1 FROM {db.q(t)} o WHERE {on})")
                    added[t] = con.execute(f"SELECT COUNT(*) FROM {db.q(t)}").fetchone()[0] - before
            finally:
                con.execute("DETACH lb")
            con.execute("INSERT OR REPLACE INTO _meta VALUES (?, ?)", [f"lifeboat_ingested:{digest}", f"{path} at {datetime.datetime.now():%Y-%m-%d %H:%M}"])
        print(f"Lifeboat check results from {os.path.basename(path)}: added {added}")
        taken.append(os.path.basename(path))
    run.note(lifeboat_results=taken)
    return taken


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--skip-groups", action="store_true", help="use the newest group inventory instead of a new one")
    parser.add_argument("--members-csv", help="load this member report instead of fetching the newest (tests)")
    parser.add_argument("--items-csv", help="load this item report instead of fetching the newest (tests)")
    parser.add_argument("--mqs-csv", help="use this directory (MQS) lookup instead of a new one (tests)")
    parser.add_argument("--dashboards-only", action="store_true",
                        help="only rebuild the site's data from what the database holds, and publish it (no ArcGIS Online)")
    args = parser.parse_args()
    if args.dashboards_only:
        return rebuild_dashboards()

    try:
        with Run(None, "daily") as run:
            os.environ["AGOL_RUN_ID"] = run.run_id   # every step records itself under this run and writes into its folder
            started = datetime.datetime.now()
            step("read-only-check", ["tools/check_read_only.py"], run)   # first, before anything is read
            groups = run.path("groups")
            if args.skip_groups:
                found = sorted(glob.glob(os.path.join(db.reports_dir(), "runs", "**", "groups", "groups.csv"), recursive=True), key=os.path.getmtime)
                if not found:
                    raise RuntimeError("--skip-groups: no earlier group inventory found")
                groups = os.path.dirname(found[-1])
            else:
                step("groups", ["tools/group_inventory.py", "--out", groups], run)

            catalog = ["TAMU_AGOL_Catalog.py", "--entraid-csv", entra_lookup(run)]
            if args.members_csv and args.items_csv:
                catalog += ["--members-csv", args.members_csv, "--items-csv", args.items_csv]
            if args.mqs_csv:
                catalog += ["--mqs-csv", args.mqs_csv]
            step("catalog", catalog, run)

            step("deletestatus", ["TAMU_AGOL_DeleteStatus.py", "--dry-run"], run)
            changes = os.path.join(run.dir, "deletestatus-dryrun", "DeleteStatus_changes.csv")
            removal = run.path("removal")
            step("removal", ["tools/removal_summary.py", "--changes", changes, "--out", removal], run)
            # --again: a rerun on the same report date adds its own snapshot, and the site uses the newest, so a change
            # such as a new account link shows at once; earlier snapshots are kept (Dan, 22 October 2026)
            step("snapshot", ["dashboard/build_dashboard_data.py", "--again", "--groups", groups, "--out", run.path("dashboard-data"), "--changes", changes,
                              "--removal-summary", os.path.join(removal, "removal_summary.csv")], run)
            step("dashboards", ["dashboard/build_full_dashboard.py", "--removal-summary", os.path.join(removal, "removal_summary.csv"),
                                "--groups", groups], run)
            lifeboat_results(run)
            step("publish-db", ["tools/publish_serving_db.py"], run)

            status = {"run_id": run.run_id, "started": started.isoformat(timespec="seconds"),
                      "finished": datetime.datetime.now().isoformat(timespec="seconds"), "groups": groups}
            os.makedirs(db.output_dir(), exist_ok=True)
            json.dump(status, open(os.path.join(db.output_dir(), "last_run.json"), "w"), indent=1)
            run.note(**status)
            print(f"daily run {run.run_id} finished in {(datetime.datetime.now() - started).seconds // 60} min")

    finally:
        publish_final()


def publish_final():
    """Once more after a run is marked succeeded or failed, so the site's copy of RUNS shows how it ended (the
    publish-db step inside the run copies it while the run still reads "running"). After a failure the site keeps
    the last good data and shows the failed run."""
    result = subprocess.run([sys.executable, "-u", "tools/publish_serving_db.py"], cwd=SCRIPT_DIR)
    if result.returncode != 0:
        print("the final publish failed; the site keeps the copy published during the run")


def newest(pattern):
    found = sorted(glob.glob(os.path.join(db.reports_dir(), "runs", *pattern), recursive=True), key=os.path.getmtime)
    if not found:
        raise RuntimeError(f"nothing found for {'/'.join(pattern)} under the run folders")
    return found[-1]


def rebuild_dashboards():
    """--dashboards-only (22 October 2026): rebuild the site's data from what the database already holds (the newest
    snapshot, removal summary and group inventory) and publish it. It reads nothing from ArcGIS Online, the
    directory or Entra, so it takes minutes, not a full run: for a change to how the dashboards are built."""
    try:
        with Run(None, "dashboards") as run:
            os.environ["AGOL_RUN_ID"] = run.run_id
            started = datetime.datetime.now()
            groups = os.path.dirname(newest(("**", "groups", "groups.csv")))
            removal = newest(("**", "removal", "removal_summary.csv"))
            step("dashboards", ["dashboard/build_full_dashboard.py", "--removal-summary", removal, "--groups", groups], run)
            lifeboat_results(run)
            step("publish-db", ["tools/publish_serving_db.py"], run)
            run.note(groups=groups, removal_summary=removal)
            print(f"dashboards rebuilt in {(datetime.datetime.now() - started).seconds // 60} min, from {groups} and {removal}")
    finally:
        publish_final()


if __name__ == "__main__":
    main()
