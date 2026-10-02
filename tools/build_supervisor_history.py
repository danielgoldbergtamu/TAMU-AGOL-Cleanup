"""Merge every supervisor lookup ever made into one supervisor history.

Why: a supervisor is the person told to collect a departed user's data before it is
removed, and the identity provider stops returning a supervisor once someone leaves.
Old lookups are often the only record, so this keeps every supervisor ever seen and
never lets a later blank overwrite an earlier answer.

Sources are SQL Server tables and CSV files. Column names are matched loosely
(Username / userName / userEmail / studentEmail, ManagerEmail, ManagerDepartment,
UserDepartment, updated_date), so lookups from different script versions merge.

Usage (ArcGIS Pro's Python has pyodbc):
    python tools/build_supervisor_history.py --out private/data \
        --sql "localhost\\MSSQLSERVER01|AGOLCleanup|HIST_EntraID_Status" \
        --csv reports/AGOL_EntraID_Status.csv --csv-date 2026-07-30

Outputs (both contain personal data; write them outside the public repository):
    supervisor_history.csv  one row per person per supervisor, first/last seen, sources
    supervisor_latest.csv   each person's most recent non-empty supervisor
"""
import argparse
import csv
import os
import re
from datetime import date, datetime

USER_COLUMNS = ("username", "useremail", "studentemail")
DATE_COLUMNS = ("updated_date",)
ORG_SUFFIX = re.compile(r"(@[^@\s]+?)_[a-z0-9]+$")  # netid@domain.edu_org -> netid@domain.edu  # leak-check: allow


def normalize_user(value):
    value = (value or "").strip().lower()
    return ORG_SUFFIX.sub(r"\1", value) if "@" in value else value


def clean(value):
    value = "" if value is None else str(value).strip()
    return "" if value.lower() in ("", "null", "none", "nan") else value


def to_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    value = clean(value)
    if not value:
        return None
    try:
        return datetime.fromisoformat(value[:19]).date()
    except ValueError:
        return None


def pick(columns, wanted):
    lower = {c.lower(): c for c in columns}
    for name in wanted:
        if name in lower:
            return lower[name]
    return None


def rows_from_sql(spec):
    import pyodbc  # only needed for SQL sources

    server, database, table = spec.split("|")
    conn = pyodbc.connect(
        f"DRIVER={{ODBC Driver 17 for SQL Server}};SERVER={server};DATABASE={database};"
        "Trusted_Connection=yes;Encrypt=yes;TrustServerCertificate=yes;",
        readonly=True,
    )
    cursor = conn.cursor()
    cursor.execute(f"SELECT * FROM [dbo].[{table}]")
    columns = [d[0] for d in cursor.description]
    for record in cursor:
        yield dict(zip(columns, record))
    conn.close()


def rows_from_csv(path):
    with open(path, encoding="utf-8-sig", errors="replace", newline="") as fh:
        yield from csv.DictReader(fh)


def collect(rows, source, fallback_date):
    """Yield (user, manager, manager_dept, user_dept, seen) for rows that name a supervisor."""
    columns = None
    for row in rows:
        if columns is None:
            columns = list(row)
            user_col = pick(columns, USER_COLUMNS)
            mgr_col = pick(columns, ("manageremail",))
            mdept_col = pick(columns, ("managerdepartment",))
            udept_col = pick(columns, ("userdepartment",))
            date_col = pick(columns, DATE_COLUMNS)
            if not user_col or not mgr_col:
                print(f"  skip {source}: no user or ManagerEmail column")
                return
        manager = clean(row.get(mgr_col)).lower()
        user = normalize_user(row.get(user_col))
        if not manager or not user:
            continue
        seen = to_date(row.get(date_col)) if date_col else None
        yield (
            user,
            manager,
            clean(row.get(mdept_col)) if mdept_col else "",
            clean(row.get(udept_col)) if udept_col else "",
            seen or fallback_date,
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", required=True, help="output folder (outside the public repo)")
    parser.add_argument("--sql", action="append", default=[], help='"server|database|table"')
    parser.add_argument("--csv", action="append", default=[], help="CSV lookup file")
    parser.add_argument("--csv-date", action="append", default=[],
                        help="date for the matching --csv when its rows carry none (YYYY-MM-DD)")
    args = parser.parse_args()

    history = {}  # (user, manager) -> record
    sources = [(f"sql:{s.split('|', 1)[1]}", rows_from_sql(s), None) for s in args.sql]
    for i, path in enumerate(args.csv):
        fallback = to_date(args.csv_date[i]) if i < len(args.csv_date) else None
        sources.append((f"csv:{os.path.basename(path)}", rows_from_csv(path), fallback))

    for label, rows, fallback in sources:
        count = 0
        for user, manager, mdept, udept, seen in collect(rows, label, fallback):
            count += 1
            rec = history.setdefault((user, manager), {
                "user": user, "manager_email": manager, "manager_department": "",
                "user_department": "", "first_seen": None, "last_seen": None,
                "sightings": 0, "sources": set(),
            })
            rec["sightings"] += 1
            rec["sources"].add(label)
            if seen and (rec["first_seen"] is None or seen < rec["first_seen"]):
                rec["first_seen"] = seen
            if seen is None or rec["last_seen"] is None or seen >= rec["last_seen"]:
                rec["manager_department"] = mdept or rec["manager_department"]
                rec["user_department"] = udept or rec["user_department"]
                if seen and (rec["last_seen"] is None or seen > rec["last_seen"]):
                    rec["last_seen"] = seen
        print(f"  {label}: {count} rows with a supervisor")

    os.makedirs(args.out, exist_ok=True)
    fields = ["user", "manager_email", "manager_department", "user_department",
              "first_seen", "last_seen", "sightings", "sources"]
    ordered = sorted(history.values(), key=lambda r: (r["user"], r["last_seen"] or date.min))
    with open(os.path.join(args.out, "supervisor_history.csv"), "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for rec in ordered:
            writer.writerow({**rec, "sources": ";".join(sorted(rec["sources"]))})

    latest = {}
    for rec in ordered:  # sorted by last_seen, so the last one per user wins
        latest[rec["user"]] = rec
    with open(os.path.join(args.out, "supervisor_latest.csv"), "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for rec in latest.values():
            writer.writerow({**rec, "sources": ";".join(sorted(rec["sources"]))})

    print(f"people with a supervisor ever recorded: {len(latest)}")
    print(f"person-supervisor pairs: {len(history)}")
    print(f"people seen with more than one supervisor: "
          f"{sum(1 for u in latest if sum(1 for k in history if k[0] == u) > 1)}")


if __name__ == "__main__":
    main()
