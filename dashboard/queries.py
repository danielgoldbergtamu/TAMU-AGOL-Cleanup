"""The dashboard's views, computed from the database: what the website's API answers (web/app.py).

Each view is a function of an open, read-only DuckDB connection (the website's copy, tools/publish_serving_db.py)
and the edition: named=True for the full edition, False for the published one, which never names a person or a
layer. The numbers follow the same rules as the page builder (build_full_dashboard.py), which still embeds every
view in the built pages for reading them offline; a view moved here is checked against the built page.

Only duckdb is needed, so the website does not load pandas for these.
"""
import re

FEATURE_LIMIT_GB, ACCOUNT_LIMIT = 500, 15000


def r(x, n=3):
    """GB to 3 decimals (1 MB), so the page can show 0.01 GB (10 MB)."""
    return round(float(x or 0), n)


def q(name):
    return '"' + str(name).replace('"', '""') + '"'


def newest(con, prefix):
    """The newest dated report table, e.g. OrganizationItems_2026_10_21."""
    names = [t for (t,) in con.execute("SELECT table_name FROM information_schema.tables").fetchall()
             if re.fullmatch(re.escape(prefix) + r"\d{4}_\d{2}_\d{2}", t)]
    return max(names) if names else None


def lit(value):
    return "'" + str(value).replace("'", "''") + "'"


def has(con, name):
    return con.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?", [name]).fetchone()[0] > 0


def rows(con, sql, params=()):
    cur = con.execute(sql, list(params))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def latest_snapshot(con):
    """(as_of, run_id) of the newest snapshot: the latest run of the newest report date."""
    return con.execute("SELECT as_of, MAX(run_id) FROM SNAP_MemberStatus "
                       "WHERE as_of = (SELECT MAX(as_of) FROM SNAP_MemberStatus) GROUP BY as_of").fetchone()


ITEMS_SQL = ('SELECT "Item ID" AS item_id, "Title" AS title, "Item Type" AS item_type, "Owner" AS owner, '
             'COALESCE("Feature Storage Size", 0) / 1024.0 AS feature_gb, COALESCE("File Storage Size", 0) / 1024.0 AS file_gb, '
             'lower(trim(CAST("In Recycle Bin" AS VARCHAR))) IN (\'yes\', \'true\', \'1\') AS in_bin FROM {items}')


def overview(con, named):
    """The overview: totals against the limits, current accounts, the storage trend, classifications, the
    removal groups, and the recycle bin (left out of every total)."""
    as_of, run_id = latest_snapshot(con)
    items_t, members_t = q(newest(con, "OrganizationItems_")), q(newest(con, "OrganizationMembers_"))
    # a view cannot take parameters: the run is written in as a literal (it comes from the database, quoted anyway)
    con.execute(f"CREATE OR REPLACE TEMP VIEW snap AS SELECT *, lower(trim(username)) AS key FROM SNAP_MemberStatus "
                f"WHERE as_of = DATE '{as_of}' AND run_id = {lit(run_id)}")
    con.execute(f"CREATE OR REPLACE TEMP VIEW items AS {ITEMS_SQL.format(items=items_t)}")

    t = con.execute("SELECT COUNT(*), COUNT(*) FILTER (items > 0) FROM snap").fetchone()
    i = con.execute("SELECT COUNT(*), SUM(feature_gb), SUM(file_gb) FROM items WHERE NOT in_bin").fetchone()
    total = {"accounts": t[0], "owners": t[1], "items": i[0], "feature_gb": r(i[1]), "file_gb": r(i[2])}

    # Current accounts (in Entra with a current affiliation), and how recently accounts signed in.
    c = rows(con, f"""
        WITH m AS (SELECT s.classification = 'current' AS cur, s.login_type, s.items, l.last_login >= CAST(? AS TIMESTAMP) - INTERVAL 90 DAY AS d90,
                          l.last_login >= CAST(? AS TIMESTAMP) - INTERVAL 365 DAY AS d365
                   FROM snap s LEFT JOIN (SELECT lower(trim(Username)) AS key, "Last Login Date" AS last_login FROM {members_t}) l USING (key))
        SELECT COUNT(*) AS members, COUNT(*) FILTER (cur) AS current, COUNT(*) FILTER (cur AND login_type = 'enterprise') AS netid,
               COUNT(*) FILTER (cur AND login_type = 'arcgis') AS arcgis, COUNT(*) FILTER (cur AND items > 0) AS owning,
               COUNT(*) FILTER (d90) AS d90, COUNT(*) FILTER (d90 AND cur) AS d90_current,
               COUNT(*) FILTER (d365) AS d365, COUNT(*) FILTER (d365 AND cur) AS d365_current,
               COUNT(*) FILTER (cur AND d365 IS NOT TRUE) AS current_idle_year FROM m""", [as_of, as_of])[0]

    trend = [{"as_of": str(x["as_of"]), "accounts": x["accounts"], "feature_gb": r(x["feature_gb"]), "file_gb": r(x["file_gb"]),
              "current_accounts": x["current_accounts"], "current_feature_gb": r(x["current_feature_gb"]),
              "departed_feature_gb": r(x["departed_feature_gb"])}
             for x in rows(con, """
        SELECT as_of, COUNT(*) AS accounts, SUM(feature_gb) AS feature_gb, SUM(file_gb) AS file_gb,
               COUNT(*) FILTER (status = 'current') AS current_accounts,
               COALESCE(SUM(feature_gb) FILTER (status = 'current'), 0) AS current_feature_gb,
               COALESCE(SUM(feature_gb) FILTER (status = 'departed'), 0) AS departed_feature_gb
        FROM SNAP_MemberStatus s WHERE run_id = (SELECT MAX(x.run_id) FROM SNAP_MemberStatus x WHERE x.as_of = s.as_of)
        GROUP BY as_of ORDER BY as_of""")]

    classes = [{**x, "feature_gb": r(x["feature_gb"]), "file_gb": r(x["file_gb"])} for x in rows(con, """
        SELECT classification, COUNT(*) AS accounts, COUNT(*) FILTER (items > 0) AS owners, CAST(SUM(items) AS BIGINT) AS items,
               SUM(feature_gb) AS feature_gb, SUM(file_gb) AS file_gb
        FROM snap WHERE classification IS NOT NULL GROUP BY classification ORDER BY classification""")]

    removal = [] if not has(con, "SNAP_RemovalSummary") else [
        {**x, "feature_gb": r(x["feature_gb"]), "file_gb": r(x["file_gb"])} for x in rows(con, """
        SELECT "group", users, items, feature_gb, file_gb FROM SNAP_RemovalSummary WHERE run_id = ? ORDER BY "group" """, [run_id])]

    b = con.execute("SELECT COUNT(*), SUM(feature_gb), SUM(file_gb), COUNT(DISTINCT lower(trim(owner))) FROM items WHERE in_bin").fetchone()
    binned = rows(con, """
        SELECT i.item_id, i.title, i.item_type, i.owner, COALESCE(s.unit, 'Unit unknown') AS unit, s.sub_unit, s.status, i.feature_gb, i.file_gb
        FROM items i LEFT JOIN snap s ON s.key = lower(trim(i.owner)) WHERE i.in_bin
        ORDER BY i.feature_gb DESC, i.item_id LIMIT 200""")
    recycle_bin = {"items": b[0], "feature_gb": r(b[1]), "file_gb": r(b[2]), "owners": b[3],
                   "list": [{"title": x["title"] if named else f'{x["item_type"]} #{n + 1}', "id": x["item_id"] if named else "",
                             "type": x["item_type"], "owner": x["owner"] if named else "", "unit": x["unit"],
                             # the published edition gives the department only of current members
                             "sub_unit": (x["sub_unit"] or "") if named or x["status"] == "current" else "",
                             "feature_gb": r(x["feature_gb"]), "file_gb": r(x["file_gb"])} for n, x in enumerate(binned)]}

    return {"edition": "full" if named else "published", "as_of": str(as_of), "run_id": run_id,
            "limits": {"feature_gb": FEATURE_LIMIT_GB, "accounts": ACCOUNT_LIMIT}, "total": total, "current_accounts": c,
            "trend": trend, "classes": classes, "removal": removal, "recycle_bin": recycle_bin}


VIEWS = {"overview": overview}
