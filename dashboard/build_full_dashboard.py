"""Builds the AGOL cleanup dashboard's data, in two editions, from the catalog database.

  full edition        names people and layer titles; signed-in access only.
  published edition   every person and layer title replaced by its unit and a rank. Refused if it would name anyone.

The data goes into the database (DASH_* tables), from which tools/publish_serving_db.py makes the website's
read-only copy and the website answers /api/v1/... (web/app.py, dashboard/queries.py). The page itself,
full_template.html, holds no data; the website serves it. Each edition's data is also written to AGOL_OUTPUT_DIR
(default data/, git-ignored) as dashboard_full.json and dashboard_published.json, for checking.

Reads: the newest snapshot (SNAP_MemberStatus, SNAP_UnitUsage), the newest item report table, the
STATUS_CHANGES view, RUNS, HIST_Supervisors, and the newest removal_summary.csv under reports/runs.
Run it after a run's snapshot has been written:

    python dashboard/build_full_dashboard.py
"""
import collections
import csv
import datetime
import glob
import json
import os
import re
import sys

import pandas as pd
from dotenv import load_dotenv

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
load_dotenv(os.path.join(REPO, ".env"), override=False)   # before any path is read from the settings
import agol_db as db  # noqa: E402
FEATURE_LIMIT_GB, ACCOUNT_LIMIT = 500, 15000
# Degree (major code) -> department, from the TAMU catalogs (20 October 2026, Dan's review pending). Rows with
# high or medium confidence place a degree under its department; the rest stay their own entry, marked unconfirmed.
MAJOR_MAP = os.path.join(db.config_dir(), "major_department_map.csv")
# Staff department names that mean the college itself (dean's office, college administration), Dan's review pending
COLLEGE_LEVEL = os.path.join(db.config_dir(), "college_level_departments.csv")
STAFF, COLLEGE_LEVEL_DEPT = "Faculty & staff", "College-level"
# Layers of a departed owner with no department, placed in a department through the groups they are shared with
# (often class groups), are their own group under it, never its faculty & staff (Dan, 22 October 2026).
VIA_GROUP = "Shared to its groups (owner left)"
sys.path.insert(0, HERE)
from build_dashboard_data import COLLEGE_UNIT  # noqa: E402  (the directory college -> unit table the snapshot uses)
# The website's data (21 October 2026): the page is a shell, and asks the site for everything. Per edition, the
# views the page takes whole (DASH_ViewData; "base" is every small section and the index of unit views), each
# college, department and degree view (DASH_Scope), and, full edition only, the account pages and group lists.
SITE_VIEWS = ("history", "people", "review")
NOT_IN_BASE = {"history", "people", "review", "scopes", "acct", "acct_items", "group_lists", "names", "accounts", "person_of", "person_n"}
SCOPE_TABLES = {"top_items", "top_users", "types", "viewed", "trend", "removal", "classes", "current_accounts"}   # left out of the index
GROUP_LIST_COLUMNS = ["user", "classification", "unit", "sub_unit", "items", "feature_gb", "file_gb", "last_login", "projected",
                      "supervisor", "rgroup", "login"]
STANDARD_ROLES = {"Publisher with Notebooks", "User", "Viewer", "Data Editor", "Publisher"}
norm = lambda v: re.sub(r'(@[^@\s]+?)_[a-z0-9]+$', r'\1', (v or '').strip().lower())


def bucket_days(days, edges):
    for limit, label in edges:
        if days is not None and days <= limit:
            return label
    return edges[-1][1] if days is not None else "never"


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Build both dashboard editions from the newest snapshot.")
    parser.add_argument("--removal-summary", help="removal_summary.csv to use (default: the newest one of the report's date)")
    parser.add_argument("--groups", help="group inventory folder to use (default: the newest one)")
    args = parser.parse_args()
    load_dotenv(os.path.join(REPO, ".env"), override=True)
    with db.connect() as c:
        def read(sql, *p):
            """A query as a DataFrame, with DATE columns as dates (DuckDB hands them to pandas as midnight timestamps)."""
            rel = c.execute(sql, list(p))
            frame = rel.df()
            for (name, type_code, *_rest) in rel.description:
                if str(type_code).upper() == "DATE" and name in frame:
                    frame[name] = frame[name].dt.date
            return frame
        as_of = c.execute("SELECT MAX(as_of) FROM SNAP_MemberStatus").fetchone()[0]
        snap = read("SELECT * FROM SNAP_MemberStatus WHERE as_of = ? ORDER BY run_id, username", as_of)
        snap = snap[snap["run_id"] == snap["run_id"].max()]
        unit_usage = read("SELECT * FROM SNAP_UnitUsage ORDER BY run_id, status, unit, sub_unit")
        unit_usage = unit_usage[unit_usage["run_id"] == unit_usage.groupby("as_of")["run_id"].transform("max")]   # latest run per date
        trend_members = read("SELECT as_of, run_id, classification, status, COUNT(*) accounts, SUM(items) items, "
                             "SUM(feature_gb) feature_gb, SUM(file_gb) file_gb FROM SNAP_MemberStatus GROUP BY as_of, run_id, classification, status")
        items_table, members_table = db.newest(c, "OrganizationItems_"), db.newest(c, "OrganizationMembers_")
        items = read('SELECT "Item ID" AS "item_id", "Title" AS "title", "Item Type" AS "item_type", "Owner" AS "owner", "Share Level" AS "share", '
                     '"# of Groups shared with" AS "groups_n", "Date Created" AS "created", "Date Modified" AS "modified", "Date Last Viewed" AS "last_viewed", "View Counts" AS "views", '
                     'COALESCE("Feature Storage Size",0)/1024.0 AS "feature_gb", COALESCE("File Storage Size",0)/1024.0 AS "file_gb", '
                     f'"In Recycle Bin" AS "in_bin" FROM {db.q(items_table)} ORDER BY "Item ID"')
        members = read('SELECT Username, Name, Email, "Login Type" AS "login", "Last Login Date" AS "last_login", "User Type" AS "user_type", '
                       f'"# of Groups Owned" AS "groups_owned" FROM {db.q(members_table)} ORDER BY Username')
        changes = read("SELECT * FROM STATUS_CHANGES WHERE changed_by = ? ORDER BY username", as_of)
        for col in ("prev_projected_status", "projected_status"):   # whole numbers, blank when missing
            changes[col] = changes[col].astype("Int64").astype(object).where(changes[col].notna(), None)
        # each account's state in every snapshot (the latest run of each date), for the change lists' history
        snap_history = read("SELECT username, as_of, classification, projected_status, unit FROM SNAP_MemberStatus s "
                            "WHERE run_id = (SELECT MAX(x.run_id) FROM SNAP_MemberStatus x WHERE x.as_of = s.as_of)")
        runs = read("SELECT run_id, kind, status, started_at, finished_at FROM RUNS ORDER BY started_at DESC LIMIT 25")
        sup = read("SELECT UserEmail, ManagerEmail, LastSeen FROM HIST_Supervisors")
        account_netids = read("SELECT Username, NetID, LastSeen FROM HIST_AccountNetIDs") if db.exists(c, "HIST_AccountNetIDs") else None
        entra_names = read("SELECT Email, FirstName, LastName, DisplayName, LastSeen FROM HIST_PersonNames") if db.exists(c, "HIST_PersonNames") else None

    now = pd.Timestamp(as_of)
    # Items in the AGOL recycle bin are left out of every total, and shown on their own (Dan, 21 October 2026):
    # whether AGOL still counts them against the storage limit until they are deleted for good is not settled.
    in_bin = items["in_bin"].astype(str).str.strip().str.lower().isin(["yes", "true", "1"])
    binned = items[in_bin].copy()
    items = items[~in_bin].copy()
    snap["key"] = snap["username"].str.strip().str.lower()
    items["key"] = items["owner"].str.strip().str.lower()
    members["key"] = members["Username"].str.strip().str.lower()
    snap = snap.merge(members[["key", "last_login", "user_type", "groups_owned"]], on="key", how="left")
    owner_info = snap.set_index("key")[["classification", "status", "unit", "sub_unit", "projected_status", "role"]]
    items = items.join(owner_info, on="key")
    latest_sup = sup.sort_values("LastSeen").groupby(sup["UserEmail"].str.lower())["ManagerEmail"].last()
    snap["supervisor"] = snap["key"].map(norm).map(latest_sup)
    items["supervisor"] = items["key"].map(norm).map(latest_sup)

    # Names for the full edition (20 October 2026): the latest first and last name from Entra (HIST_PersonNames),
    # else the name on the AGOL account. Keyed like the supervisor history (netid@domain, no AGOL suffix).
    person_names = {}
    for key, name in zip(members["Username"].map(norm), members["Name"]):
        if isinstance(name, str) and name.strip():
            person_names[key] = name.strip()
    for key, name in zip(members["Email"].map(norm), members["Name"]):
        if isinstance(name, str) and name.strip():
            person_names.setdefault(key, name.strip())
    if entra_names is not None and len(entra_names):
        for row_ in entra_names.sort_values("LastSeen").itertuples(index=False):
            full = " ".join(p_ for p_ in (row_.FirstName, row_.LastName) if isinstance(p_, str) and p_.strip()) or (row_.DisplayName or "")
            if full.strip():
                person_names[norm(row_.Email)] = full.strip()

    # ArcGIS-login accounts (any username, e.g. a class or project account) belong to the person whose email
    # is on the account; the full edition shows that email under the account (Dan, 20 October 2026).
    arcgis_email = {norm(u): e.strip().lower() for u, e, lt in zip(members["Username"], members["Email"], members["login"])
                    if lt == "arcgis" and isinstance(e, str) and "@" in e}

    # People (20 October 2026): every account belongs to the person whose NetID the Entra lookup matched
    # (HIST_AccountNetIDs), or, for a NetID account, the NetID in its username; a person can have several.
    person_of = {k: norm(k) for k, lt in zip(snap["key"], snap["login_type"]) if lt == "enterprise" and "@" in k}
    if account_netids is not None and len(account_netids):
        for u, netid in account_netids.sort_values("LastSeen")[["Username", "NetID"]].itertuples(index=False):
            person_of[u.strip().lower()] = netid.strip().lower()
    # the maintainer's links last, so they win (agol_db.account_owner_links; Dan, 22 October 2026)
    owner_links = db.account_owner_links()
    protected = db.protected_accounts()   # never put up for deletion (Dan, 23 October 2026)
    db.apply_owner_links(person_of, owner_links)
    linked = {l["account"]: f'linked by hand on {l.get("decided_on", "")}' + (f': {l["note"]}' if l.get("note") else "") for l in owner_links}
    snap["person"] = snap["key"].map(person_of)
    multi = snap[snap["person"].notna()].groupby("person").filter(lambda g: len(g) > 1)

    def people(named):
        rows = []
        for i, (p, g) in enumerate(sorted(multi.groupby("person"), key=lambda pg: -pg[1]["feature_gb"].sum())):
            current = bool((g["classification"] == "current").any())
            extra = g[(g["login_type"] == "arcgis")] if current else g.iloc[0:0]
            unit = g["unit"].dropna().iloc[0] if g["unit"].notna().any() else "Unit unknown"
            rows.append({"rank": i + 1, "person": p if named else f"Person #{i + 1}", "unit": unit, "current": current,
                         "n": len(g), "n_current": int((g["classification"] == "current").sum()),
                         "arcgis": int((g["login_type"] == "arcgis").sum()), "items": int(g["items"].sum()),
                         "feature_gb": r(g["feature_gb"].sum(), 3), "file_gb": r(g["file_gb"].sum(), 3),
                         "consolidate": len(extra), "consolidate_gb": r(extra["feature_gb"].sum(), 3),
                         "statuses": ", ".join(sorted({status_label.get(s, "not assessed") for s in g["projected_status"]})),
                         "accounts": [{"user": a["username"], "login": a["login_type"], "items": int(a["items"]),
                                       "feature_gb": r(a["feature_gb"], 3), "file_gb": r(a["file_gb"], 3), "projected": status_label.get(a["projected_status"], "not assessed")}
                                      for _, a in g.sort_values("feature_gb", ascending=False).iterrows()] if named else []})
        cons = [x for x in rows if x["consolidate"]]
        return {"people": len(rows), "accounts": int(sum(x["n"] for x in rows)), "feature_gb": r(sum(x["feature_gb"] for x in rows), 3),
                "current_with_arcgis": len(cons), "consolidate_accounts": int(sum(x["consolidate"] for x in cons)),
                "consolidate_gb": r(sum(x["consolidate_gb"] for x in cons), 3),
                "linked_arcgis": int(((snap["login_type"] == "arcgis") & snap["person"].notna()).sum()),
                "arcgis_total": int((snap["login_type"] == "arcgis").sum()), "list": rows[:400],
                "current_people": sum(1 for x in rows if x["current"]), "current_accounts": current_counts(multi)}

    contact = dict(zip(members["key"], members["Email"].fillna("")))
    by_owner = {k: g for k, g in items.sort_values("feature_gb", ascending=False).groupby("key")}

    def account_pages(keys, max_items=250):
        """Details and items (largest first, up to max_items) of each account in keys; full edition only."""
        acct, acct_items = {}, {}
        for _, a in snap[snap["key"].isin(keys)].iterrows():
            k = a["key"]
            g = by_owner.get(k)
            acct[k] = {"user": a["username"], "email": contact.get(k, ""), "login": a["login_type"], "role": a["role"],
                       "classification": a["classification"], "unit": a["unit"], "sub_unit": a["sub_unit"] if isinstance(a["sub_unit"], str) else "",
                       "person": a["person"] if isinstance(a["person"], str) else "", "last_login": date_str(a["last_login"]),
                       "groups_owned": int(pd.to_numeric(a["groups_owned"], errors="coerce") or 0), "items": int(a["items"]),
                       "feature_gb": r(a["feature_gb"], 3), "file_gb": r(a["file_gb"], 3),
                       "status": status_label.get(a["delete_status"], "none yet"), "projected": status_label.get(a["projected_status"], "not assessed"),
                       "group": a["rgroup"], "supervisor": a["supervisor"] if isinstance(a["supervisor"], str) else "",
                       "linked": linked.get(k, ""),
                       "protected": (f'protected by the maintainer on {protected[k].get("decided_on", "")}' + (f': {protected[k]["note"]}' if protected[k].get("note") else "")) if k in protected else "",
                       "shown_items": 0 if g is None else min(len(g), max_items)}
            if g is not None:
                acct_items[k] = [[x.item_id, x.title, x.item_type, r(x.feature_gb, 3), r(x.file_gb, 3), x.share, date_str(x.last_viewed), date_str(x.created)]
                                 for x in g.head(max_items).itertuples(index=False)]
        return acct, acct_items

    def recycle_bin(named):
        """What is in the AGOL recycle bin: counts, and the items (largest first; titles and owners only when named)."""
        b = binned.sort_values("feature_gb", ascending=False)
        owner_unit = dict(zip(snap["key"], snap["unit"]))
        owner_sub = {k: (s if (named or st == "current") and isinstance(s, str) else "") for k, s, st in zip(snap["key"], snap["sub_unit"], snap["status"])}
        return {"items": len(b), "feature_gb": r(b["feature_gb"].sum(), 3), "file_gb": r(b["file_gb"].sum(), 3),
                "owners": int(b["owner"].str.strip().str.lower().nunique()),
                "list": [{"title": x.title if named else f"{x.item_type} #{i + 1}", "id": x.item_id if named else "", "type": x.item_type,
                          "owner": x.owner if named else "", "unit": owner_unit.get(str(x.owner).strip().lower(), "Unit unknown"),
                          "sub_unit": owner_sub.get(str(x.owner).strip().lower(), ""),
                          "feature_gb": r(x.feature_gb, 3), "file_gb": r(x.file_gb, 3)}
                         for i, x in enumerate(b.head(200).itertuples(index=False))]}

    def change_history(named):
        """Every member's state in every snapshot (the latest run of each date): classification, unit and projected
        delete status, stored only on the dates it changes, so the page can compare any two snapshots. Names and
        supervisors only in the full edition."""
        h = snap_history.copy()
        h["key"] = h["username"].str.strip().str.lower()
        h["d"] = h["as_of"].astype(str).str[:10]
        dates = sorted(h["d"].unique())
        di = {d: i for i, d in enumerate(dates)}
        classes = sorted(h["classification"].fillna("-").unique())
        units = sorted(h["unit"].fillna("").unique())
        ci, ui = {c: i for i, c in enumerate(classes)}, {u: i for i, u in enumerate(units)}
        gb_now, items_now = dict(zip(snap["key"], snap["feature_gb"])), dict(zip(snap["key"], snap["items"]))
        rows = []
        for k, g in h.groupby("key", sort=True):
            states = {di[x.d]: (ci[x.classification if isinstance(x.classification, str) else "-"], ui[x.unit if isinstance(x.unit, str) else ""],
                                -1 if pd.isna(x.projected_status) else int(x.projected_status)) for x in g.itertuples(index=False)}
            entries, prev = [], "absent"
            for d in range(len(dates)):
                s = states.get(d)
                cur = s if s is not None else "absent"
                if cur != prev:
                    entries.append([d, *s] if s is not None else [d])   # [date index] alone: no longer a member
                    prev = cur
            sup = latest_sup.get(norm(k)) if named else None
            rows.append([g["username"].iloc[0] if named else "", r(gb_now.get(k, 0) or 0, 3), int(items_now.get(k, 0) or 0),
                         sup if isinstance(sup, str) else "", entries])
        return {"dates": dates, "classes": classes, "units": units, "rows": rows}

    def supv(row, named):  # the owner's latest supervisor on record; the published edition says only whether there is one
        s = row["supervisor"] if isinstance(row["supervisor"], str) else ""
        return s if named else ("on record" if s else "")

    def days_since(series):
        return (now - pd.to_datetime(series, errors="coerce")).dt.days

    items["viewed_days"] = days_since(items["last_viewed"])
    snap["login_days"] = days_since(snap["last_login"])
    view_edges = [(365, "within 1 year"), (730, "1-2 years"), (1825, "2-5 years"), (10 ** 6, "over 5 years")]
    login_edges = [(90, "within 90 days"), (365, "90 days - 1 year"), (730, "1-2 years"), (10 ** 6, "over 2 years")]
    items["viewed_bucket"] = items["viewed_days"].apply(lambda d: "never viewed" if pd.isna(d) else bucket_days(d, view_edges))
    snap["login_bucket"] = snap["login_days"].apply(lambda d: "never logged in" if pd.isna(d) else bucket_days(d, login_edges))

    # Removal groups: the most recently written removal summary of this date (tools/removal_summary.py).
    day = pd.Timestamp(as_of).strftime("%Y%m%d")
    summaries = [args.removal_summary] if args.removal_summary else         sorted(glob.glob(os.path.join(db.reports_dir(), "runs", f"{day}*", "**", "removal_summary.csv"), recursive=True), key=os.path.getmtime)
    removal = list(csv.DictReader(open(summaries[-1], encoding="utf-8"))) if summaries else []

    def r(x, n=3):   # GB to 3 decimals (1 MB), so the page can show 0.01 GB (10 MB)
        return round(float(x), n)

    total = {"accounts": len(snap), "owners": int((snap["items"] > 0).sum()), "items": len(items),
             "feature_gb": r(items["feature_gb"].sum()), "file_gb": r(items["file_gb"].sum())}
    # Current accounts (in Entra with a current affiliation), and how recently accounts signed in.
    def current_counts(df):
        is_cur = df["classification"] == "current"
        signed_in = pd.to_datetime(df["last_login"], errors="coerce")
        within = lambda days: signed_in >= now - pd.Timedelta(days=days)
        return {"members": len(df), "current": int(is_cur.sum()), "netid": int((is_cur & (df["login_type"] == "enterprise")).sum()),
                "arcgis": int((is_cur & (df["login_type"] == "arcgis")).sum()), "owning": int((is_cur & (df["items"] > 0)).sum()),
                "d90": int(within(90).sum()), "d90_current": int((within(90) & is_cur).sum()),
                "d365": int(within(365).sum()), "d365_current": int((within(365) & is_cur).sum()),
                "current_idle_year": int((is_cur & ~within(365)).sum())}

    current_accounts = current_counts(snap)
    trend = []
    for (d, run_id), g in trend_members.groupby(["as_of", "run_id"]):
        trend.append({"as_of": str(d), "accounts": int(g["accounts"].sum()), "feature_gb": r(g["feature_gb"].sum()), "file_gb": r(g["file_gb"].sum()),
                      "current_accounts": int(g.loc[g["status"] == "current", "accounts"].sum()),
                      "current_feature_gb": r(g.loc[g["status"] == "current", "feature_gb"].sum()),
                      "departed_feature_gb": r(g.loc[g["status"] == "departed", "feature_gb"].sum())})
    trend = sorted({t["as_of"]: t for t in trend}.values(), key=lambda t: t["as_of"])
    classes = [{"classification": k, "accounts": len(g), "owners": int((g["items"] > 0).sum()), "items": int(g["items"].sum()),
                "feature_gb": r(g["feature_gb"].sum()), "file_gb": r(g["file_gb"].sum())} for k, g in snap.groupby("classification")]
    latest_units = unit_usage[unit_usage["run_id"] == unit_usage.loc[unit_usage["as_of"] == unit_usage["as_of"].max(), "run_id"].max()]
    units = latest_units[["status", "unit", "sub_unit", "members", "owners", "items", "feature_gb", "file_gb"]].fillna("").to_dict("records")
    unit_trend = []
    for (d, unit), g in unit_usage.groupby(["as_of", "unit"]):
        unit_trend.append({"as_of": str(d), "unit": unit, "current_gb": r(g.loc[g["status"] == "current", "feature_gb"].sum()),
                           "departed_gb": r(g.loc[g["status"] == "departed", "feature_gb"].sum()), "members": int(g["members"].sum())})
    item_types = [{"type": k, "items": len(g), "feature_gb": r(g["feature_gb"].sum()), "file_gb": r(g["file_gb"].sum())}
                  for k, g in items.groupby("item_type")]
    viewed = [{"bucket": b, "status": s or "unknown", "items": len(g), "feature_gb": r(g["feature_gb"].sum())}
              for (b, s), g in items.fillna({"status": "unknown"}).groupby(["viewed_bucket", "status"])]
    logins = [{"bucket": b, "classification": k, "accounts": len(g)} for (b, k), g in snap.groupby(["login_bucket", "classification"])]
    roles = [{"role": k, "accounts": len(g)} for k, g in snap.groupby(snap["role"].fillna("unknown"))]
    logintypes = [{"login_type": k, "accounts": len(g)} for k, g in snap.groupby(snap["login_type"].fillna("unknown"))]

    top_items = items.sort_values("feature_gb", ascending=False).head(100)
    top_users = snap[snap["items"] > 0].sort_values("feature_gb", ascending=False).head(100)
    status_label = {0: "kept", 1: "flagged", 2: "listed for removal"}
    change_counts = []
    if len(changes):
        changes["projected_label"] = changes["projected_status"].map(status_label)
        changes["prev_projected_label"] = changes["prev_projected_status"].map(status_label)
        for (a, b), g in changes.groupby([changes["prev_classification"].fillna("-"), changes["classification"].fillna("-")]):
            if a != b:
                change_counts.append({"kind": "classification", "from": a, "to": b, "accounts": len(g), "feature_gb": r(g["feature_gb"].sum())})
        for (a, b), g in changes.groupby([changes["prev_projected_label"].fillna("none"), changes["projected_label"].fillna("none")]):
            if a != b:
                change_counts.append({"kind": "projected delete status", "from": a, "to": b, "accounts": len(g), "feature_gb": r(g["feature_gb"].sum())})
    current = snap[snap["classification"] == "current"]
    quality = [
        ["Current members with no unit recorded", int((current["unit"] == "No unit recorded").sum()), r(current.loc[current["unit"] == "No unit recorded", "feature_gb"].sum())],
        ["Members in unmapped departments", int((snap["unit"] == "Unmapped departments").sum()), r(snap.loc[snap["unit"] == "Unmapped departments", "feature_gb"].sum())],
        ["Departed members, unit unknown", int(((snap["status"] == "departed") & (snap["unit"] == "Unit unknown")).sum()), r(snap.loc[(snap["status"] == "departed") & (snap["unit"] == "Unit unknown"), "feature_gb"].sum())],
        ["Members not looked up in Entra", int((snap["classification"] == "not looked up").sum()), r(snap.loc[snap["classification"] == "not looked up", "feature_gb"].sum())],
        ["Members with no delete status yet", int(snap["delete_status"].isna().sum()), r(snap.loc[snap["delete_status"].isna(), "feature_gb"].sum())],
        ["ArcGIS-login (non-SSO) accounts", int((snap["login_type"] == "arcgis").sum()), r(snap.loc[snap["login_type"] == "arcgis", "feature_gb"].sum())],
    ]

    def date_str(v):
        v = pd.to_datetime(v, errors="coerce")
        return "" if pd.isna(v) else v.strftime("%Y-%m-%d")

    # Unit views. Items are assigned to units as the overview does: the owner's unit, or, for a departed
    # owner with no unit, the unit of the one current owner of the groups the item is shared with.
    inventories = sorted(glob.glob(os.path.join(db.reports_dir(), "runs", "*", "groups", "groups.csv"))
                         + glob.glob(os.path.join(db.reports_dir(), "runs", "*-group-inventory", "groups.csv")), key=os.path.getmtime)
    group_owner, item_groups = {}, {}
    if args.groups:
        inventories = [os.path.join(args.groups, "groups.csv")]
    if inventories:
        folder = os.path.dirname(inventories[-1])
        group_owner = {r_["group_id"]: r_["owner"].strip().lower() for r_ in csv.DictReader(open(os.path.join(folder, "groups.csv"), encoding="utf-8"))}
        for r_ in csv.DictReader(open(os.path.join(folder, "group_items.csv"), encoding="utf-8")):
            item_groups.setdefault(r_["item_id"], set()).add(r_["group_id"])
    current_unit = {k: (u, s) for k, u, s, c in snap[["key", "unit", "sub_unit", "classification"]].itertuples(index=False)
                    if c == "current" and u != "No unit recorded"}
    items["item_unit"] = items["unit"].fillna("Unit unknown")
    items["item_sub"] = items["sub_unit"].fillna("")
    items["via_group"] = False
    for idx in items.index[(items["status"] == "departed") & (items["item_unit"] == "Unit unknown")]:
        found = {current_unit[o] for g in item_groups.get(items.at[idx, "item_id"], ()) for o in [group_owner.get(g)] if o in current_unit}
        if len(found) == 1:
            items.at[idx, "item_unit"], items.at[idx, "item_sub"] = next(iter(found))
            items.at[idx, "via_group"] = True

    def removal_group(row):  # same rules as tools/removal_summary.py
        p = row["projected_status"]
        if pd.isna(p):
            return "Not yet assessed (not in the Entra lookup)"
        if row["role"] not in STANDARD_ROLES and p in (1, 2):
            return "Review: admin or non-standard role"
        if p == 2 and row["items"] == 0:
            if row["login_type"] == "enterprise" and (pd.to_numeric(row["groups_owned"], errors="coerce") or 0) == 0:
                return "1. Empty account"
            return "Review: empty, ArcGIS login or owns groups"
        if p == 2:
            return "2. Status 2 with content"
        if p == 1:
            return "3. Status 1"
        return "Kept (status 0)"

    snap["rgroup"] = snap.apply(removal_group, axis=1)
    items = items.join(snap.set_index("key")["rgroup"], on="key")
    # College > department > degree. A student's sub_unit is a major code; the map places it under a department.
    by_unit_code, by_code, degree_names, unconfirmed = {}, {}, {}, {}
    if os.path.exists(MAJOR_MAP):
        for m_ in csv.DictReader(open(MAJOR_MAP, encoding="utf-8-sig")):
            code, dept = (m_.get("code") or "").strip(), (m_.get("department") or "").strip()
            if not code:
                continue
            degree_names[code] = (m_.get("degree_name") or "").strip()
            # keyed by the student's own unit, derived from the directory college exactly as the snapshot derives it
            # (a code can mean different programs in two colleges, e.g. ENST)
            unit_ = COLLEGE_UNIT.get((m_.get("college") or "").strip(), (m_.get("college") or "").strip())
            by_code.setdefault(code, []).append(unit_)
            if (m_.get("confidence") or "").strip().lower() in ("high", "medium") and dept and not dept.startswith("("):
                by_unit_code[(unit_, code)] = dept
            else:
                unconfirmed[code] = dept.strip("()") if dept.startswith("(") else "department not confirmed"

    college_level = set()
    if os.path.exists(COLLEGE_LEVEL):
        college_level = {(c_["unit"].strip(), c_["department_name"].strip()) for c_ in csv.DictReader(open(COLLEGE_LEVEL, encoding="utf-8-sig"))
                         if (c_.get("college_level") or "").strip().lower() == "yes"}
    all_codes = set(by_code)

    def place(unit, sub):
        """(department, child) for a unit and a sub_unit. A department's children are its degrees and its faculty & staff:
        a mapped major code -> (its department, the code); a college-wide degree -> (College-level, the code); a staff
        department name -> (that department, Faculty & staff), or (College-level, Faculty & staff) when the name means the
        college itself. Other codes (interdisciplinary, unconfirmed) and "major not given" stay their own entries."""
        if not sub:
            return "", ""
        dept = by_unit_code.get((unit, sub), "")
        if dept:
            return dept, sub
        if sub in all_codes:
            return (COLLEGE_LEVEL_DEPT, sub) if unconfirmed.get(sub) == "college-level" else (sub, "")
        if sub == "major not given" or unit in ("Unmapped departments", "Unit unknown", "No unit recorded"):
            return sub, (STAFF if sub != "major not given" else "")
        return (COLLEGE_LEVEL_DEPT, STAFF) if (unit, sub) in college_level else (sub, STAFF)

    snap_unit = snap["unit"].fillna("Unit unknown")
    snap_sub = snap["sub_unit"].fillna("")
    placed = [place(u, s) for u, s in zip(snap_unit, snap_sub)]
    snap["dept"], snap["degree"] = [d for d, _ in placed], [g for _, g in placed]
    placed = [place(u, s) for u, s in zip(items["item_unit"], items["item_sub"])]
    items["item_dept"], items["item_degree"] = [d for d, _ in placed], [g for _, g in placed]
    # placed through a group: under the group owner's department, as its own group (VIA_GROUP), not their degree or staff
    items.loc[items["via_group"] & (items["item_dept"] != ""), "item_degree"] = VIA_GROUP
    placed = [place(u, s) for u, s in zip(unit_usage["unit"].fillna("Unit unknown"), unit_usage["sub_unit"].fillna(""))]
    unit_usage["dept"], unit_usage["child"] = [d for d, _ in placed], [g for _, g in placed]
    snap_dept, snap_degree = snap["dept"], snap["degree"]
    unit_names = sorted(set(items["item_unit"]) | set(snap_unit))
    dept_pairs = sorted({(u, d) for u, d in zip(items["item_unit"], items["item_dept"]) if d} | {(u, d) for u, d in zip(snap_unit, snap_dept) if d})
    degree_sets = sorted({(u, d, g) for u, d, g in zip(items["item_unit"], items["item_dept"], items["item_degree"]) if g}
                         | {(u, d, g) for u, d, g in zip(snap_unit, snap_dept, snap_degree) if g})
    scope_index = [{"key": f"u{i}", "kind": "unit", "unit": u, "sub": ""} for i, u in enumerate(unit_names)] + \
                  [{"key": f"d{i}", "kind": "sub", "unit": u, "sub": s} for i, (u, s) in enumerate(dept_pairs)] + \
                  [{"key": f"g{i}", "kind": "degree", "unit": u, "sub": d, "degree": g} for i, (u, d, g) in enumerate(degree_sets)]

    def scope(entry, named, top_n, status=None):
        if entry["kind"] == "unit":
            it, mem = items[items["item_unit"] == entry["unit"]], snap[snap_unit == entry["unit"]]
            tr = unit_usage[unit_usage["unit"] == entry["unit"]]
        elif entry["kind"] == "sub":
            it = items[(items["item_unit"] == entry["unit"]) & (items["item_dept"] == entry["sub"])]
            mem = snap[(snap_unit == entry["unit"]) & (snap_dept == entry["sub"])]
            tr = unit_usage[(unit_usage["unit"] == entry["unit"]) & (unit_usage["dept"] == entry["sub"])]
        else:
            it = items[(items["item_unit"] == entry["unit"]) & (items["item_dept"] == entry["sub"]) & (items["item_degree"] == entry["degree"])]
            mem = snap[(snap_unit == entry["unit"]) & (snap_dept == entry["sub"]) & (snap_degree == entry["degree"])]
            tr = unit_usage[(unit_usage["unit"] == entry["unit"]) & (unit_usage["dept"] == entry["sub"]) & (unit_usage["child"] == entry["degree"])]
        if status:   # current or departed members only (the blue or orange part of a bar; Dan, 22 October 2026)
            is_cur = lambda s: (s == "current") if status == "current" else (s != "current")
            it, mem, tr = it[is_cur(it["status"])], mem[is_cur(mem["status"])], tr[is_cur(tr["status"])]
        cur_it, dep_it = it[it["status"] == "current"], it[it["status"] != "current"]
        out = {"members": len(mem), "current_members": int((mem["status"] == "current").sum()), "owners": int(it["key"].nunique()),
               "items": len(it), "feature_gb": r(it["feature_gb"].sum(), 3), "file_gb": r(it["file_gb"].sum(), 3),
               "current_gb": r(cur_it["feature_gb"].sum(), 3), "departed_gb": r(dep_it["feature_gb"].sum(), 3)}
        out["classes"] = [{"classification": k, "members": len(g)} for k, g in mem.groupby("classification")]
        out["current_accounts"] = current_counts(mem)
        out["removal"] = [{"group": k, "users": int(g["key"].nunique()), "items": int(len(it[it["rgroup"] == k])),
                           "feature_gb": r(it.loc[it["rgroup"] == k, "feature_gb"].sum(), 3)}
                          for k, g in mem.groupby("rgroup")]
        out["types"] = [{"type": k, "items": len(g), "feature_gb": r(g["feature_gb"].sum(), 3)}
                        for k, g in it.groupby("item_type")]
        out["types"] = sorted(out["types"], key=lambda x: -x["feature_gb"])[:8]
        out["viewed"] = [{"bucket": k, "items": len(g), "feature_gb": r(g["feature_gb"].sum(), 3)} for k, g in it.groupby("viewed_bucket")]
        out["trend"] = sorted([{"as_of": str(d), "current_gb": r(g.loc[g["status"] == "current", "feature_gb"].sum(), 3),
                                "departed_gb": r(g.loc[g["status"] == "departed", "feature_gb"].sum(), 3), "members": int(g["members"].sum())}
                               for d, g in tr.groupby("as_of")], key=lambda x: x["as_of"])
        if entry["kind"] in ("unit", "sub"):
            # a unit lists its departments; a department lists its degrees (when it has any)
            col_i, col_m = ("item_dept", snap_dept) if entry["kind"] == "unit" else ("item_degree", snap_degree)
            in_scope = (snap_unit == entry["unit"]) & ((snap_dept == entry["sub"]) if entry["kind"] == "sub" else True)
            names = set(it[col_i]) | set(col_m[in_scope])
            subs = []
            for s in names:
                if entry["kind"] == "sub" and not s:
                    continue
                g = it[it[col_i] == s]
                subs.append({"sub": s, "owners": int(g["key"].nunique()), "current_gb": r(g.loc[g["status"] == "current", "feature_gb"].sum(), 3),
                             "departed_gb": r(g.loc[g["status"] != "current", "feature_gb"].sum(), 3), "items": len(g),
                             "members": int((in_scope & (col_m == s)).sum()),
                             "degrees": int(snap_degree[in_scope & (snap_dept == s) & (snap_degree != STAFF)].replace("", pd.NA).nunique()) if entry["kind"] == "unit" else 0,
                             "staff": int((in_scope & (snap_dept == s) & (snap_degree == STAFF)).sum()) if entry["kind"] == "unit" else 0,
                             "unconfirmed": unconfirmed.get(s, "") if entry["kind"] == "unit" else ""})
            out["subs"] = sorted(subs, key=lambda x: -(x["current_gb"] + x["departed_gb"]))
        top_i = it.sort_values("feature_gb", ascending=False).head(top_n)
        out["top_items"] = [{"rank": i + 1, "title": row["title"] if named else f"{row['item_type']} #{i + 1}", "type": row["item_type"],
                             "id": row["item_id"] if named else "",
                             "sub_unit": row["item_sub"] if (named or row["status"] == "current") else "",
                             "owner": row["owner"] if named else f"Owner #{i + 1}", "owner_class": row["classification"] if isinstance(row["classification"], str) else "unknown",
                             "feature_gb": r(row["feature_gb"], 3), "last_viewed": date_str(row["last_viewed"]),
                             "projected": status_label.get(row["projected_status"], "not assessed"), "supervisor": supv(row, named)}
                            for i, (_, row) in enumerate(top_i.iterrows())]
        top_m = mem[mem["items"] > 0].sort_values("feature_gb", ascending=False).head(top_n)
        out["top_users"] = [{"rank": i + 1, "user": row["username"] if named else f"Person #{i + 1}", "classification": row["classification"],
                             "sub_unit": row["sub_unit"] if (named or row["status"] == "current") and isinstance(row["sub_unit"], str) else "",
                             "items": int(row["items"]), "feature_gb": r(row["feature_gb"], 3), "last_login": date_str(row["last_login"]),
                             "projected": status_label.get(row["projected_status"], "not assessed"), "supervisor": supv(row, named)}
                            for i, (_, row) in enumerate(top_m.iterrows())]
        return out

    # Review section (20 October 2026): concentration of current storage, and the empty-account list with cautions.
    largest_item = items.sort_values("feature_gb", ascending=False).groupby("key").first()
    cur_owners = snap[(snap["classification"] == "current") & (snap["feature_gb"] > 0)].sort_values("feature_gb", ascending=False).copy()
    cur_owners["cumulative_gb"] = cur_owners["feature_gb"].cumsum()
    removal_gb = {row_["group"][:2]: float(row_["feature_gb"]) for row_ in removal}
    gap_gb = total["feature_gb"] - sum(removal_gb.get(k, 0) for k in ("1.", "2.", "3.")) - FEATURE_LIMIT_GB
    empty = snap[snap["items"] == 0].copy()   # every account that owns nothing; group 1 is a subset
    empty["in_group_1"] = empty["rgroup"] == "1. Empty account"

    def cautions(row):
        found = []
        if row["login_type"] == "arcgis":
            found.append("ArcGIS login: not recreated at next sign-in")
        if (pd.to_numeric(row["groups_owned"], errors="coerce") or 0) > 0:
            found.append("owns groups: AGOL will refuse")
        if row["classification"] == "current":
            found.append("current member")
        if pd.notna(row["login_days"]) and row["login_days"] <= 90:
            found.append("signed in within 90 days")
        return found

    empty["cautions"] = empty.apply(cautions, axis=1)

    def review(named):
        concentration = [{"label": f"holding {t_}+ GB", "people": int((cur_owners["feature_gb"] >= t_).sum()),
                          "feature_gb": r(cur_owners.loc[cur_owners["feature_gb"] >= t_, "feature_gb"].sum())} for t_ in (20, 10, 5, 1)]
        concentration += [{"label": f"top {k_}", "people": k_, "feature_gb": r(cur_owners.head(k_)["feature_gb"].sum())} for k_ in (5, 10, 25, 50, 100)]
        owners_for_gap = int((cur_owners["cumulative_gb"] < gap_gb).sum()) + 1 if gap_gb > 0 else 0
        top = []
        for i, (_, row) in enumerate(cur_owners.head(150).iterrows()):
            li = largest_item.loc[row["key"]] if row["key"] in largest_item.index else None
            top.append({"rank": i + 1, "user": row["username"] if named else f"Person #{i + 1}", "unit": row["unit"],
                        "sub_unit": row["sub_unit"] if isinstance(row["sub_unit"], str) else "", "items": int(row["items"]),
                        "feature_gb": r(row["feature_gb"], 3), "cumulative_gb": r(row["cumulative_gb"], 3),
                        "largest_item": (li["title"] if named else li["item_type"]) if li is not None else "",
                        "largest_item_gb": r(li["feature_gb"], 3) if li is not None else 0,
                        "last_login": date_str(row["last_login"]),
                        "supervisor": (row["supervisor"] if isinstance(row["supervisor"], str) else "") if named else ("on record" if isinstance(row["supervisor"], str) else "")})
        caution_counts = collections.Counter(c_ for cs in empty["cautions"] for c_ in cs)
        out = {"current_owners": len(cur_owners), "current_gb": r(cur_owners["feature_gb"].sum()), "gap_gb": r(gap_gb),
               "owners_for_gap": owners_for_gap, "concentration": concentration, "top_current": top,
               "empty_total": len(empty), "empty_no_caution": int((empty["cautions"].str.len() == 0).sum()),
               "empty_group_1": int(empty["in_group_1"].sum()),
               "empty_cautions": [{"caution": k_, "accounts": v_} for k_, v_ in caution_counts.most_common()],
               "empty_by": {col: [{"value": k_, "accounts": int(v_)} for k_, v_ in empty[col].fillna("unknown").value_counts().items()]
                            for col in ("classification", "login_type", "login_bucket")}}
        out["empty_list"] = [{"user": row["username"], "classification": row["classification"], "login_type": row["login_type"],
                              "last_login": date_str(row["last_login"]), "login_bucket": row["login_bucket"],
                              "groups_owned": int(pd.to_numeric(row["groups_owned"], errors="coerce") or 0), "unit": row["unit"],
                              "supervisor": row["supervisor"] if isinstance(row["supervisor"], str) else "",
                              "in_group_1": bool(row["in_group_1"]), "projected": status_label.get(row["projected_status"], "not assessed"),
                              "cautions": "; ".join(row["cautions"])} for _, row in empty.iterrows()] if named else []
        return out

    def scopes(named):
        return {e["key"]: {**e, **scope(e, named, 15 if e["kind"] == "unit" else 10)} for e in scope_index}

    def status_scopes(named):
        """Each view for current members only and for departed members only, keyed "<key>~current" and
        "<key>~departed": for the site (DASH_Scope), not the index."""
        return {f'{e["key"]}~{s}': {**e, "status": s, **scope(e, named, 15 if e["kind"] == "unit" else 10, s)}
                for e in scope_index for s in ("current", "departed")}

    def edition(named):
        def owner_label(rank, row):
            return row["owner"] if named else f"Owner #{rank} ({row['unit'] if isinstance(row['unit'], str) else 'unit unknown'})"
        ti = [{"rank": i + 1, "title": (row["title"] if named else f"{row['item_type']} #{i + 1}"), "type": row["item_type"],
               "id": row["item_id"] if named else "",
               "owner": owner_label(i + 1, row), "owner_class": row["classification"] if isinstance(row["classification"], str) else "unknown",
               "unit": items.at[idx, "item_unit"], "sub_unit": items.at[idx, "item_sub"] if (named or row["status"] == "current") else "",
               "feature_gb": r(row["feature_gb"], 3), "file_gb": r(row["file_gb"], 3),
               "share": row["share"], "groups": int(row["groups_n"] or 0), "last_viewed": date_str(row["last_viewed"]),
               "created": date_str(row["created"]), "projected": status_label.get(row["projected_status"], "not assessed"),
               "supervisor": supv(row, named)}
              for i, (idx, row) in enumerate(top_items.iterrows())]
        tu = [{"rank": i + 1, "user": (row["username"] if named else f"Person #{i + 1}"), "classification": row["classification"],
               "unit": row["unit"], "sub_unit": row["sub_unit"] if named or row["status"] == "current" else "", "items": int(row["items"]),
               "feature_gb": r(row["feature_gb"], 3), "file_gb": r(row["file_gb"], 3), "last_login": date_str(row["last_login"]),
               "projected": status_label.get(row["projected_status"], "not assessed"),
               "supervisor": (row["supervisor"] if isinstance(row["supervisor"], str) else "") if named else ("on record" if isinstance(row["supervisor"], str) else "")}
              for i, (_, row) in enumerate(top_users.iterrows())]
        change_list = []
        if named and len(changes):
            change_list = changes.sort_values("feature_gb", ascending=False).head(300)[
                ["username", "changed_after", "changed_by", "prev_classification", "classification", "prev_unit", "unit",
                 "prev_projected_status", "projected_status", "items", "feature_gb"]].astype(str).replace({"None": "", "nan": "", "<NA>": ""}).to_dict("records")
            for c_ in change_list:
                s_ = latest_sup.get(norm(c_["username"].strip().lower()))
                c_["supervisor"] = s_ if isinstance(s_, str) else ""
        return {"edition": "full" if named else "published", "as_of": str(as_of), "generated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                "sources": f"{members_table}, {items_table}, snapshot {snap['run_id'].iloc[0]}",
                "limits": {"feature_gb": FEATURE_LIMIT_GB, "accounts": ACCOUNT_LIMIT}, "total": total, "current_accounts": current_accounts, "recycle_bin": recycle_bin(named), "trend": trend,
                "classes": classes, "removal": removal, "units": units, "unit_trend": unit_trend, "item_types": item_types,
                "viewed": viewed, "logins": logins, "roles": roles, "login_types": logintypes, "top_items": ti, "top_users": tu,
                "history": change_history(named), "quality": quality,
                "protected": [[k, p_.get("note", ""), p_.get("decided_on", ""), p_.get("decided_by", ""), k in set(snap["key"])] for k, p_ in protected.items()] if named else len(protected),
                "owner_links": [[l["account"], l["owner"], person_of.get(l["account"], ""), l.get("note", ""), l.get("decided_on", ""),
                                 l.get("decided_by", ""), l["account"] in set(snap["key"])] for l in owner_links] if named else len(owner_links),
                "runs": runs.astype(str).to_dict("records"), "scopes": scopes(named), "review": review(named), "degree_names": degree_names, "degree_notes": unconfirmed,
                "placement": {f"{u}\u0001{s}": list(place(u, s)) for u, s in set(zip(snap_unit, snap_sub)) | set(zip(items["item_unit"], items["item_sub"])) if s}, "people": people(named)}

    def write_site_tables(acct, acct_items, names, logins, person_of_multi, counts, group_list, views, scopes_):
        """What the website's API reads, written to the database each run (replacing the last run's) and copied to
        the site's read-only copy by tools/publish_serving_db.py: every account's page (/api/v1/account/<name>),
        every account with its removal group (/api/v1/group/<group>, full edition), and the views the page takes
        whole, as each edition shows them (DASH_ViewData: /api/v1/base, history, people, review), and each
        college, department and degree view (DASH_Scope: /api/v1/scope/<key>)."""
        accounts = pd.DataFrame([{"key": k, **v} for k, v in acct.items()])
        item_rows = pd.DataFrame([[k, n_, *x] for k, xs in acct_items.items() for n_, x in enumerate(xs)],
                                 columns=["key", "n", "item_id", "title", "item_type", "feature_gb", "file_gb", "share", "last_viewed", "created"])
        text = lambda df: {c_: "VARCHAR" for c_ in df.columns if df[c_].dtype == object}
        tables = {"DASH_Account": accounts, "DASH_AccountItems": item_rows,
                  "DASH_Names": pd.DataFrame(sorted(names.items()), columns=["key", "name"]),
                  "DASH_ArcgisLogins": pd.DataFrame(sorted(logins.items()), columns=["key", "email"]),
                  "DASH_PersonOf": pd.DataFrame([(k, p, int(counts[p])) for k, p in sorted(person_of_multi.items())], columns=["key", "person", "accounts"]),
                  "DASH_GroupList": pd.DataFrame(group_list, columns=GROUP_LIST_COLUMNS),
                  "DASH_ViewData": pd.DataFrame([(ed, v, json.dumps(d, separators=(",", ":"), default=str)) for ed, vs in views.items() for v, d in vs.items()],
                                                columns=["edition", "view", "data"]),
                  "DASH_Scope": pd.DataFrame([(ed, k, json.dumps(s, separators=(",", ":"), default=str)) for ed, ss in scopes_.items() for k, s in ss.items()],
                                             columns=["edition", "key", "data"]),
                  # every layer outside the recycle bin, with its dates and its owner's tier, for selecting layers for
                  # deletion by period (Proposed removals; Dan, 22 October 2026). Full edition only.
                  "DASH_Item": items.assign(tier=items["rgroup"])[["item_id", "title", "item_type", "key", "owner", "created", "modified",
                                                                    "last_viewed", "feature_gb", "file_gb", "share", "tier"]]}
        with db.connect() as c:
            c.execute("BEGIN")
            for name, df in tables.items():
                types = text(df)
                if name == "DASH_Item":   # real dates, for selecting by year
                    types.update({c_: "DATE" for c_ in ("created", "modified", "last_viewed")})
                db.write(c, name, df, mode="replace", types=types)
            c.execute("COMMIT")
        print("site tables: " + ", ".join(f"{k} {len(v)}" for k, v in tables.items()))

    site_views, site_scopes = {}, {}
    out = db.output_dir()
    os.makedirs(out, exist_ok=True)
    for named in (True, False):
        data = edition(named)
        by_status = status_scopes(named)
        if named:
            # only the names of people who appear on the page
            shown = set(re.findall(r"[a-z0-9._%+-]+@[a-z0-9.-]+|[a-z0-9._-]{3,}", json.dumps({k_: v_ for k_, v_ in data.items() if k_ != "history"}, default=str).lower()))   # the history lists everyone; not "shown"
            shown_accounts = {k for k in snap["key"] if k in shown or norm(k) in shown}
            data["accounts"] = {k: v for k, v in arcgis_email.items() if k in shown}
            in_multi = set(multi["person"])
            data["names"] = {k: v for k, v in person_names.items() if k in shown or k in in_multi}
            counts = multi.groupby("person").size().to_dict()
            data["person_of"] = {norm(k): p for k, p in person_of.items() if k in shown_accounts and p in counts}
            data["person_n"] = {p: int(n) for p, n in counts.items()}
            data["acct"], data["acct_items"] = account_pages(shown_accounts | set(snap.loc[snap["person"].isin(in_multi), "key"]))
            site = {"pages": account_pages(set(snap["key"])), "person_of": {norm(k): p for k, p in person_of.items() if p in counts},
                    "counts": counts}
            # Every account with its removal group, for the lists behind the Proposed removals table (full edition only)
            data["group_lists"] = [[a.username, a.classification, a.unit, a.sub_unit if isinstance(a.sub_unit, str) else "", int(a.items),
                                    r(a.feature_gb, 3), r(a.file_gb, 3), date_str(a.last_login), status_label.get(a.projected_status, "not assessed"),
                                    a.supervisor if isinstance(a.supervisor, str) else "", a.rgroup, a.login_type]
                                   for a in snap.itertuples(index=False)]
            site["group_list"] = data["group_lists"]
            for k in snap["key"]:
                if norm(k) in person_names:
                    data["names"].setdefault(norm(k), person_names[norm(k)])
                if norm(k) in arcgis_email:
                    data["accounts"].setdefault(norm(k), arcgis_email[norm(k)])
        if not named:
            # The published edition must name no one. Refuse to write its data if any account name, or any
            # NetID-length local part, appears as a word, except words that are part of a unit name.
            words = set(re.findall(r"[a-z0-9._@-]{3,}", (json.dumps(data, default=str) + json.dumps(by_status, default=str)).lower()))
            unit_words = set(re.findall(r"[a-z0-9._@-]{3,}", " ".join(map(str, list(snap["unit"]) + list(snap["sub_unit"]))).lower()))
            usernames = set(snap["username"].str.strip().str.lower())
            locals_ = {u.split("@")[0] for u in usernames if len(u.split("@")[0]) >= 5}
            leaked = sorted(((usernames | locals_) & words) - unit_words)
            if leaked:
                raise SystemExit(f"REFUSED: the published edition would name {len(leaked)} accounts (e.g. {leaked[:3]}). Not written.")
            print("published edition checked: no account names")
        json_path = os.path.join(out, f"dashboard_{data['edition']}.json")   # the same data, for checking
        open(json_path + ".tmp", "w", encoding="utf-8").write(json.dumps(data, separators=(",", ":"), default=str))
        os.replace(json_path + ".tmp", json_path)
        print(f"wrote {json_path} ({os.path.getsize(json_path) / 1024:.0f} KB, {data['edition']} edition)")
        base = {k: v for k, v in data.items() if k not in NOT_IN_BASE}
        base["scope_index"] = {k: {f: v for f, v in s.items() if f not in SCOPE_TABLES} for k, s in data["scopes"].items()}
        if named:   # which accounts have a page, for the links
            base["account_keys"] = sorted(snap["key"])
        site_views[data["edition"]] = {"base": base, **{k: data[k] for k in SITE_VIEWS}}
        site_scopes[data["edition"]] = {**data["scopes"], **by_status}
    write_site_tables(*site["pages"], person_names, arcgis_email, site["person_of"], site["counts"], site["group_list"], site_views, site_scopes)


if __name__ == "__main__":
    main()
