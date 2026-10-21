"""Aggregates current AGOL usage by unit for the dashboard, and records this run's snapshot over time.

Reads the catalog database after a catalog run (newest member and item report tables, AGOL_EntraID_Status,
AGOL_MQS_Status, Whitelisted_EntraID_Groups, HIST_EntraID_Status, DeleteStatus), the department-to-unit
table, a group inventory folder, and optionally a delete-status dry run's changes file. Writes:

  dashboard_data_<date>.json   for build_dashboard.py (counts only; no person named)
  SNAP_MemberStatus            one row per member per run: classification, unit, role, items, GB,
                               current and projected delete status      (database, names people)
  SNAP_UnitUsage               one row per status/unit/sub-unit per run (database, counts only)
  STATUS_CHANGES (view)        every member whose classification, unit or projected delete status changed
                               between two consecutive snapshots, with both dates

Snapshots are append-only (Dan, 2 October 2026: nothing from an earlier run is deleted). A date that already
has a snapshot is not written again unless --again is given, so a repeated build cannot double-count.

Unit rules (decided 3 October 2026):
  current students   college of the primary major (directory), sub-unit = major code
  current employees  department (directory, else Entra) mapped by the unit table; else "Unmapped departments"
  departed members   department from earlier Entra lookups (history); for items, else the unit of a current
                     owner of a group the item is shared with; else "Unit unknown". Course codes are NOT used.

    python dashboard/build_dashboard_data.py --groups <run>/groups --out data [--changes <dry run>/DeleteStatus_changes.csv]

The database is DuckDB (agol_db.py); the department-to-unit table is read from AGOL_CONFIG_DIR (default private/config).
"""
import argparse
import collections
import csv
import datetime
import json
import os
import re
import sys
import pandas as pd
from dotenv import load_dotenv

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
load_dotenv(os.path.join(REPO, ".env"), override=False)   # before any path is read from the settings
import agol_db as db  # noqa: E402
from TAMU_AGOL_Runs import Run  # noqa: E402

UNIT_MAP = os.path.join(db.config_dir(), "department_unit_map_DRAFT.csv")
COLLEGE_UNIT = {
    "Engineering": "College of Engineering", "Arts & Sciences": "College of Arts & Sciences",
    "Agriculture & Life Sciences": "College of Agriculture & Life Sciences", "Architecture": "School of Architecture",
    "Mays Business School": "Mays Business School", "Education & Human Development": "School of Education & Human Development",
    "Marine Sci & Maritime St": "Texas A&M Galveston (Marine & Maritime)", "Public Health": "School of Public Health",
    "General Studies": "General Studies", "Bush School of Gov & Pub Serv": "Bush School of Government & Public Service",
    "Veterinary Med & Biomedical Sc": "College of Veterinary Medicine & Biomedical Sciences",
    "Perf Visual & Fine Arts": "School of Performance, Visualization & Fine Arts", "School of Law": "School of Law",
    "Medicine": "School of Medicine", "Nursing": "School of Nursing", "Exchange Program": "Exchange Program",
    "Qatar Campus": "Texas A&M Qatar"}
norm = lambda v: re.sub(r'(@[^@\s]+?)_[a-z0-9]+$', r'\1', (v or '').strip().lower())

STATUS_CHANGES_VIEW = """
CREATE OR REPLACE VIEW STATUS_CHANGES AS
WITH latest AS (   -- the latest snapshot of each date: a same-day rerun supersedes, without deleting, an earlier one
    SELECT s.* FROM SNAP_MemberStatus s
    WHERE s.run_id = (SELECT MAX(x.run_id) FROM SNAP_MemberStatus x WHERE x.as_of = s.as_of)),
ordered AS (
    SELECT s.*,
           LAG(as_of)              OVER (PARTITION BY username ORDER BY as_of, run_id) AS prev_as_of,
           LAG(classification)     OVER (PARTITION BY username ORDER BY as_of, run_id) AS prev_classification,
           LAG(unit)               OVER (PARTITION BY username ORDER BY as_of, run_id) AS prev_unit,
           LAG(projected_status)   OVER (PARTITION BY username ORDER BY as_of, run_id) AS prev_projected_status
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--groups", required=True, help="group inventory folder (groups.csv, group_items.csv)")
    parser.add_argument("--out", required=True, help="folder for dashboard_data_<date>.json")
    parser.add_argument("--changes", help="DeleteStatus_changes.csv of this run's dry run (projected status)")
    parser.add_argument("--removal-summary", help="removal_summary.csv of this run (tools/removal_summary.py), kept with the snapshot")
    parser.add_argument("--again", action="store_true", help="write a snapshot even if this date already has one")
    parser.add_argument("--no-snapshot", action="store_true", help="only write the dashboard JSON")
    args = parser.parse_args()

    load_dotenv(os.path.join(REPO, ".env"), override=True)
    con = db.connect()
    if True:
        q = lambda sql: con.execute(sql).fetchall()
        members_table, items_table = db.newest(con, "OrganizationMembers_"), db.newest(con, "OrganizationItems_")
        whitelist = {r[0].strip().lower() for r in q("SELECT ID FROM Whitelisted_EntraID_Groups")}
        entra = {r[0].strip().lower(): r for r in q("SELECT Username, EntraID_Status, Groups, UserDepartment FROM AGOL_EntraID_Status")}
        mqs = {r[0].strip().lower(): r for r in q("SELECT Username, enrolledPrimaryMajor1CollegeName, tamuEduPersonPrimaryMajor, tamuEduPersonDepartmentName FROM AGOL_MQS_Status")}
        hist_dept = {}
        for u, d in q("SELECT Username, UserDepartment FROM HIST_EntraID_Status WHERE COALESCE(UserDepartment,'')<>'' ORDER BY last_seen DESC"):
            hist_dept.setdefault(norm(u), d.strip())
        members = {r[0].strip().lower(): r for r in q(f'SELECT Username, "Role", "Login Type" FROM {db.q(members_table)}')}
        items = q(f'SELECT "Item ID", "Owner", COALESCE("Feature Storage Size",0), COALESCE("File Storage Size",0), "In Recycle Bin" FROM {db.q(items_table)}')
        delete_status = {r[0].strip().lower(): r[1] for r in q("SELECT Username, DeleteStatus FROM DeleteStatus")}

    projected = dict(delete_status)
    if not args.changes:
        # No changes file given: use the newest delete-status dry run of the report's own date, if any, so a
        # pipeline that runs the dry run just before this records projected status without being told.
        report_day = re.search(r"(\d{4})_(\d{2})_(\d{2})", members_table)
        runs_dir = os.path.join(db.reports_dir(), "runs")
        if report_day and os.path.isdir(runs_dir):
            prefix = "".join(report_day.groups())
            candidates = sorted(d for d in os.listdir(runs_dir) if d.startswith(prefix) and d.endswith("deletestatus-dryrun")
                                and os.path.exists(os.path.join(runs_dir, d, "DeleteStatus_changes.csv")))
            if candidates:
                args.changes = os.path.join(runs_dir, candidates[-1], "DeleteStatus_changes.csv")
                print(f"using the dry run of the same day for projected status: {args.changes}")
    if args.changes:
        # A real run adds every looked-up member missing from DeleteStatus at status 0 before assessing them;
        # a dry run saves no rows, and its changes file lists only those whose status then moves.
        for u in entra:
            projected.setdefault(u, 0)
        with open(args.changes, encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                projected[row["Username"].strip().lower()] = int(row["To"])

    unitmap = {r["department"]: r["unit"] for r in csv.DictReader(open(UNIT_MAP, encoding="utf-8")) if r["unit"]}
    group_owner = {r["group_id"]: r["owner"].strip().lower() for r in csv.DictReader(open(os.path.join(args.groups, "groups.csv"), encoding="utf-8"))}
    item_groups = collections.defaultdict(set)
    for r in csv.DictReader(open(os.path.join(args.groups, "group_items.csv"), encoding="utf-8")):
        item_groups[r["item_id"]].add(r["group_id"])

    def classification(u):
        e = entra.get(u)
        if not e:
            return "not looked up"
        if str(e[1]).strip() not in ("1", "True"):
            return "not in Entra"
        groups = {x.strip().lower() for x in (e[2] or "").split(",") if x.strip()}
        return "current" if groups & whitelist else "no current affiliation"

    def department(d):
        """(unit, department) for an Entra/directory department name. "Retired - X" is X: retired faculty and staff
        belong to the department they retired from (Dan, 20 October 2026); its college is X's, else the Retired name's."""
        base = re.sub(r"(?i)^retired\s*-\s*", "", d).strip()
        return unitmap.get(base) or unitmap.get(d) or "Unmapped departments", base

    def person(u):
        m, e = mqs.get(u), entra.get(u)
        if classification(u) == "current":
            if m and m[1]:
                return "current", COLLEGE_UNIT.get(m[1], m[1]), m[2] or "major not given", "student college (directory)"
            d = ((m[3] if m else "") or (e[3] if e else "") or "").strip()
            if d:
                unit, d = department(d)
                return "current", unit, d, "employee department"
            return "current", "No unit recorded", "", "none"
        d = hist_dept.get(norm(u))
        if d:
            unit, d = department(d)
            return "departed", unit, d, "department from our history"
        return "departed", None, "", "none"

    def group_unit(item_id):
        found = set()
        for g in item_groups.get(item_id, ()):
            o = group_owner.get(g)
            if o and classification(o) == "current":
                _, unit, sub, _ = person(o)
                if unit != "No unit recorded":
                    found.add((unit, sub))
        return next(iter(found)) if len(found) == 1 else None

    agg = collections.defaultdict(lambda: {"members": set(), "owners": set(), "items": 0, "feature_gb": 0.0, "file_gb": 0.0})
    basis = collections.Counter()
    per_owner = collections.defaultdict(lambda: [0, 0.0, 0.0])
    for item_id, owner, feature, file_size, in_bin in items:
        if str(in_bin).strip().lower() in ("yes", "true", "1"):
            continue
        u = owner.strip().lower()
        status, unit, sub, how = person(u)
        if status == "departed" and unit is None:
            g = group_unit(item_id)
            unit, sub, how = (g[0], g[1], "group shared with (owner's unit)") if g else ("Unit unknown", "", "none")
        a = agg[(status, unit, sub)]
        a["owners"].add(u); a["items"] += 1; a["feature_gb"] += feature / 1024; a["file_gb"] += file_size / 1024
        basis[(status, how)] += feature / 1024
        po = per_owner[u]; po[0] += 1; po[1] += feature / 1024; po[2] += file_size / 1024
    for u in members:
        status, unit, sub, _ = person(u)
        agg[(status, unit or "Unit unknown", sub)]["members"].add(u)

    rows = [{"status": s, "unit": unit, "sub": sub, "members": len(a["members"]), "owners": len(a["owners"]), "items": a["items"],
             "feature_gb": round(a["feature_gb"], 3), "file_gb": round(a["file_gb"], 3)} for (s, unit, sub), a in agg.items()]
    as_of = re.search(r"(\d{4})_(\d{2})_(\d{2})", members_table)
    as_of = "-".join(as_of.groups()) if as_of else datetime.date.today().isoformat()
    data = {"as_of": as_of,
            "sources": f"AGOL reports {members_table} and {items_table}; Entra and TAMU directory lookups of the same run; "
                       f"Entra history since April; group inventory; draft department-to-unit table",
            "rows": rows, "basis_gb": {f"{s}|{b}": round(g, 1) for (s, b), g in basis.items()}}
    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, f"dashboard_data_{as_of}.json")
    json.dump(data, open(path, "w", encoding="utf-8"))
    by_status = collections.Counter()
    for r in rows:
        by_status[r["status"]] += r["feature_gb"]
    print(f"wrote {path}: {len(rows)} rows; feature GB {dict((k, round(v, 1)) for k, v in by_status.items())}")

    if args.no_snapshot:
        return
    exists = db.exists(con, "SNAP_MemberStatus") and \
        con.execute("SELECT COUNT(*) FROM SNAP_MemberStatus WHERE as_of = ?", [as_of]).fetchone()[0] > 0
    if exists and not args.again:
        print(f"snapshot for {as_of} already exists; not written again (use --again for an extra snapshot)")
        return

    with Run(con, "snapshot") as run:
        member_rows = []
        for u, (username, role, login) in members.items():
            status, unit, sub, how = person(u)
            po = per_owner.get(u, [0, 0.0, 0.0])
            member_rows.append({"run_id": run.run_id, "as_of": as_of, "username": username, "classification": classification(u),
                                "status": status, "unit": unit or "Unit unknown", "sub_unit": sub, "unit_basis": how,
                                "role": role, "login_type": login, "items": po[0], "feature_gb": round(po[1], 4),
                                "file_gb": round(po[2], 4), "delete_status": delete_status.get(u), "projected_status": projected.get(u)})
        members_frame = pd.DataFrame(member_rows)
        units_frame = pd.DataFrame(rows).rename(columns={"sub": "sub_unit"})
        units_frame.insert(0, "as_of", as_of)
        units_frame.insert(0, "run_id", run.run_id)
        text_cols = {c_: "VARCHAR" for c_ in ("run_id", "username", "classification", "status", "unit", "sub_unit", "unit_basis", "role", "login_type")}
        db.write(con, "SNAP_MemberStatus", members_frame, mode="append",
                 types={**text_cols, "as_of": "DATE", "items": "INTEGER", "feature_gb": "DOUBLE", "file_gb": "DOUBLE",
                        "delete_status": "INTEGER", "projected_status": "INTEGER"})
        db.write(con, "SNAP_UnitUsage", units_frame, mode="append",
                 types={"run_id": "VARCHAR", "as_of": "DATE", "status": "VARCHAR", "unit": "VARCHAR", "sub_unit": "VARCHAR",
                        "members": "INTEGER", "owners": "INTEGER", "items": "INTEGER", "feature_gb": "DOUBLE", "file_gb": "DOUBLE"})
        if args.removal_summary:   # the removal groups, so the website can read them from the database
            removal = pd.read_csv(args.removal_summary)[["group", "users", "items", "feature_gb", "file_gb"]]
            removal.insert(0, "as_of", as_of)
            removal.insert(0, "run_id", run.run_id)
            db.write(con, "SNAP_RemovalSummary", removal, mode="append",
                     types={"run_id": "VARCHAR", "as_of": "DATE", "group": "VARCHAR", "users": "INTEGER", "items": "INTEGER",
                            "feature_gb": "DOUBLE", "file_gb": "DOUBLE"})
        con.execute(STATUS_CHANGES_VIEW)
        run.note(as_of=as_of, members=len(members_frame), unit_rows=len(units_frame), changes_file=args.changes)
        print(f"snapshot {run.run_id}: {len(members_frame)} members and {len(units_frame)} unit rows for {as_of}")


if __name__ == "__main__":
    main()
