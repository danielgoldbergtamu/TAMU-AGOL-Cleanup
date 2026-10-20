"""What could be removed once the backup is complete: counts for decision, lists for review.

Reads the catalog database (current member and item report tables, DeleteStatus, HIST_Supervisors) and
the changes file of a delete-status DRY RUN, applies those changes to get each member's projected status,
and sorts members into the removal groups of the process:

  1. Empty accounts      owns no items; projected status 2. No backup needed (nothing to back up, and an
                         SSO account is recreated at its next login).
  2. Status 2, content   flagged 30+ days, or left with nothing kept; removable once the backup is verified.
  3. Status 1, content   newly flagged; reaches status 2 after 30 days, then as group 2.
  Administrators and other non-standard roles found in any group are listed for review, never counted as
  removable.

Writes into --out:
  removal_summary.txt / .csv   counts only (users, items, feature GB, file GB per group)
  review_*.csv                 the people and items behind each group, for review. These name people:
                               keep them in reports/ (git-ignored), never in a repository.

This only reads. It changes nothing in the database or in ArcGIS Online, and a list here is not
permission to remove anything (see CLAUDE.md, "Deleting").

Usage (from the repo folder):
    python tools/removal_summary.py --changes <dry-run folder>/DeleteStatus_changes.csv --out <folder>
"""
import argparse
import csv
import os
import sys
import urllib.parse
from collections import defaultdict

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STANDARD_ROLES = {"Publisher with Notebooks", "User", "Viewer", "Data Editor", "Publisher"}


def engine_from_env():
    load_dotenv(os.path.join(REPO, ".env"), override=True)
    url = (os.getenv("SQL_CONNECTION_STRING") or "").strip().strip('"').strip("'")
    if not url:
        sys.exit("SQL_CONNECTION_STRING is not set in .env")
    return create_engine(url if "://" in url else "mssql+pyodbc:///?odbc_connect=" + urllib.parse.quote_plus(url))


def newest(connection, pattern):
    return connection.execute(text(f"SELECT TOP 1 name FROM sys.tables WHERE name LIKE '{pattern}' ORDER BY name DESC")).scalar()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--changes", required=True, help="DeleteStatus_changes.csv from a delete-status dry run")
    parser.add_argument("--out", required=True, help="output folder (under reports/)")
    args = parser.parse_args()
    os.makedirs(args.out, exist_ok=True)

    engine = engine_from_env()
    with engine.connect() as c:
        members_table, items_table = newest(c, "OrganizationMembers[_]20%"), newest(c, "OrganizationItems[_]20%")
        members = pd.read_sql(text(f"SELECT [Username], [Role], [Login Type], [Last Login Date], [Date Created] FROM dbo.[{members_table}]"), c)
        items = pd.read_sql(text(f"SELECT [Item ID], [Title], [Item Type], [Owner], [Share Level], [# of Groups shared with], "
                                 f"[Date Last Viewed], [Feature Storage Size], [File Storage Size], [In Recycle Bin] FROM dbo.[{items_table}]"), c)
        status = pd.read_sql(text("SELECT Username, DeleteStatus, Override FROM dbo.DeleteStatus"), c)
        has_supervisors = c.execute(text("SELECT OBJECT_ID('dbo.HIST_Supervisors')")).scalar() is not None
        supervisors = pd.read_sql(text("SELECT UserEmail, ManagerEmail, LastSeen FROM dbo.HIST_Supervisors"), c) if has_supervisors else None

    key = lambda s: s.astype(str).str.strip().str.lower()
    projected = dict(zip(key(status["Username"]), status["DeleteStatus"]))
    overrides = set(key(status.loc[status["Override"].fillna(False).astype(bool), "Username"]))
    with open(args.changes, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            projected[row["Username"].strip().lower()] = int(row["To"])

    items = items[~items["In Recycle Bin"].astype(str).str.strip().str.lower().isin(["yes", "true", "1"])].copy()
    items["owner_key"] = key(items["Owner"])
    items["feature_gb"] = pd.to_numeric(items["Feature Storage Size"], errors="coerce").fillna(0) / 1024
    items["file_gb"] = pd.to_numeric(items["File Storage Size"], errors="coerce").fillna(0) / 1024
    per_owner = items.groupby("owner_key").agg(items=("Item ID", "count"), feature_gb=("feature_gb", "sum"), file_gb=("file_gb", "sum"))

    members["key"] = key(members["Username"])
    members["projected_status"] = members["key"].map(projected)
    members = members.join(per_owner, on="key")
    members[["items", "feature_gb", "file_gb"]] = members[["items", "feature_gb", "file_gb"]].fillna(0)
    if supervisors is not None:
        supervisors["user"] = key(supervisors["UserEmail"])
        latest = supervisors.sort_values("LastSeen").groupby("user").last()["ManagerEmail"]
        members["last_supervisor"] = members["key"].str.replace(r"(@[^@\s]+?)_[a-z0-9]+$", r"\1", regex=True).map(latest)

    def group(row):
        if row["key"] in overrides:
            return "Override set (kept)"
        if row["Role"] not in STANDARD_ROLES and row["projected_status"] in (1, 2):
            return "Review: administrator or non-standard role"
        if row["projected_status"] == 2 and row["items"] == 0:
            return "1. Empty accounts (no backup needed)"
        if row["projected_status"] == 2:
            return "2. Status 2 with content (after the backup is verified)"
        if row["projected_status"] == 1:
            return "3. Status 1 with content (after 30 days and the backup)" if row["items"] else "3. Status 1, empty (after 30 days)"
        if pd.isna(row["projected_status"]):
            return "Not yet assessed (no DeleteStatus row)"
        return "Kept (status 0)"

    members["group"] = members.apply(group, axis=1)
    summary = members.groupby("group").agg(users=("key", "count"), items=("items", "sum"), feature_gb=("feature_gb", "sum"), file_gb=("file_gb", "sum")).reset_index()
    total = {"users": len(members), "items": int(members["items"].sum()), "feature_gb": members["feature_gb"].sum(), "file_gb": members["file_gb"].sum()}
    summary.to_csv(os.path.join(args.out, "removal_summary.csv"), index=False)

    removable_now = summary[summary["group"].str.startswith(("1.", "2."))]
    after_30 = summary[summary["group"].str.startswith("3.")]
    with open(os.path.join(args.out, "removal_summary.txt"), "w", encoding="utf-8") as out:
        out.write(f"Removal summary from {members_table} / {items_table} and the dry run {args.changes}\n")
        out.write("Proposals for review only. Nothing here removes anything.\n\n")
        out.write(f"{'group':58} {'users':>7} {'items':>8} {'feature GB':>11} {'file GB':>9}\n")
        for _, r in summary.sort_values("group").iterrows():
            out.write(f"{r['group']:58} {r['users']:>7,} {int(r['items']):>8,} {r['feature_gb']:>11,.1f} {r['file_gb']:>9,.1f}\n")
        out.write(f"{'Whole organization':58} {total['users']:>7,} {total['items']:>8,} {total['feature_gb']:>11,.1f} {total['file_gb']:>9,.1f}\n\n")
        fg_now, fg_30 = removable_now["feature_gb"].sum(), after_30["feature_gb"].sum()
        out.write(f"Once the backup is verified (groups 1-2): {int(removable_now['users'].sum()):,} users, "
                  f"{int(removable_now['items'].sum()):,} items, {fg_now:,.1f} GB feature -> {total['feature_gb'] - fg_now:,.1f} GB remains\n")
        out.write(f"After status 1 reaches 30 days as well (group 3): {int(after_30['users'].sum()):,} more users, "
                  f"{int(after_30['items'].sum()):,} items, {fg_30:,.1f} GB -> {total['feature_gb'] - fg_now - fg_30:,.1f} GB remains\n")
        out.write(f"Accounts remaining after groups 1-3: {total['users'] - int(removable_now['users'].sum()) - int(after_30['users'].sum()):,}\n")

    columns = ["Username", "Role", "Login Type", "Last Login Date", "projected_status", "items", "feature_gb", "file_gb"] + (["last_supervisor"] if supervisors is not None else [])
    for name, prefix in (("review_empty_accounts.csv", "1."), ("review_status2_owners.csv", "2."),
                         ("review_status1_owners.csv", "3."), ("review_admins_and_roles.csv", "Review")):
        members[members["group"].str.startswith(prefix)].sort_values("feature_gb", ascending=False)[columns].to_csv(os.path.join(args.out, name), index=False)
    status2 = set(members.loc[members["group"].str.startswith("2."), "key"])
    items[items["owner_key"].isin(status2)].drop(columns=["owner_key"]).to_csv(os.path.join(args.out, "review_status2_items.csv"), index=False)
    print(open(os.path.join(args.out, "removal_summary.txt"), encoding="utf-8").read())


if __name__ == "__main__":
    main()
