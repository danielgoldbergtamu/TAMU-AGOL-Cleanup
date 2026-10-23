"""The dashboard's views, computed from the database: what the website's API answers (web/app.py).

Each view is a function of an open, read-only DuckDB connection (the website's copy, tools/publish_serving_db.py)
and the edition: named=True for the full edition, False for the published one, which never names a person or a
layer. The numbers follow the same rules as the page builder (build_full_dashboard.py), which still embeds every
view in the built pages for reading them offline; a view moved here is checked against the built page.

Only duckdb is needed, so the website does not load pandas for these.
"""
import json
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


def norm(v):
    """An account or email as the name tables key it: lower case, without AGOL's _suffix (as the builder's norm)."""
    return re.sub(r"(@[^@\s]+?)_[a-z0-9]+$", r"\1", (v or "").strip().lower())


ITEM_COLUMNS = ["item_id", "title", "item_type", "feature_gb", "file_gb", "share", "last_viewed", "created"]


def account(con, named, key):
    """One account's page (full edition only): details, items (largest first, up to 250), the same person's other
    accounts, and the names, ArcGIS-login owners and people the page shows for them. None if there is no such account."""
    if not named or not has(con, "DASH_Account"):
        return None
    key = (key or "").strip().lower()
    found = rows(con, "SELECT * FROM DASH_Account WHERE key = ?", [key])
    if not found:
        return None
    a = found[0]
    items = [[x[c] for c in ITEM_COLUMNS] for x in rows(con, "SELECT * FROM DASH_AccountItems WHERE key = ? ORDER BY n", [key])]
    others = rows(con, "SELECT * FROM DASH_Account WHERE person = ? AND key <> ? ORDER BY feature_gb DESC, key", [a["person"], key]) if a["person"] else []
    return {"key": key, "account": {k: v for k, v in a.items() if k != "key"}, "items": items,
            "others": [[o["key"], {k: v for k, v in o.items() if k != "key"}] for o in others],
            **lookups(con, [a["user"], a["supervisor"], *(o["user"] for o in others)])}


def lookups(con, ids):
    """For the accounts and people a view shows (full edition): their names, the owners of ArcGIS logins, and which
    person each account belongs to, with how many accounts that person has. The page names people from these."""
    # The tables are small (some 16,000 names), and passing thousands of keys into a query is far slower than
    # reading a table whole (16 s against 0.02 s for the history's 14,700), so they are read whole and picked here.
    ids = {norm(x) for x in ids if x}
    pick = lambda table, col: {k: v for k, v in con.execute(f"SELECT key, {col} FROM {table}").fetchall() if k in ids}
    person_of = {k: (p, n) for k, p, n in con.execute("SELECT key, person, accounts FROM DASH_PersonOf").fetchall() if k in ids}
    # each person's unit and department or major, shown under their name everywhere (Dan, 22 October 2026); for an
    # id with several accounts, the one that has a department wins
    places = {}
    for k, unit, sub, login in con.execute("SELECT key, unit, sub_unit, last_login FROM DASH_Account").fetchall():
        k = norm(k)
        if k in ids and (k not in places or (sub and not places[k][1])):
            places[k] = [unit or "", sub or "", login or ""]   # and the last sign-in (Dan, 22 October 2026)
    return {"names": pick("DASH_Names", "name"), "accounts": pick("DASH_ArcgisLogins", "email"),
            "person_of": {k: p for k, (p, _) in person_of.items()}, "person_n": {p: n for p, n in person_of.values()},
            "places": places}


def lookups_in(con, text):
    """lookups() for every account or email that appears in a view's data (as the builder finds who a page shows)."""
    return lookups(con, re.findall(r"[a-z0-9._%+-]+@[a-z0-9._-]+|[a-z0-9._-]{3,}", text.lower()))


def stored_view(con, named, view):
    """A view the page takes whole, as the builder made it for this edition (DASH_ViewData)."""
    edition = "full" if named else "published"
    found = con.execute("SELECT data FROM DASH_ViewData WHERE edition = ? AND view = ?", [edition, view]).fetchone() if has(con, "DASH_ViewData") else None
    if not found:
        return None
    return {"edition": edition, view: json.loads(found[0]), **(lookups_in(con, found[0]) if named else {})}


def history(con, named):
    """Each member's state on every snapshot date (stored only when it changed), for the Changes page."""
    return stored_view(con, named, "history")


def people(con, named):
    """People with more than one account (the Multi-Account Holders page)."""
    return stored_view(con, named, "people")


def runs(con):
    """The most recent runs, read when asked (the list built with the dashboards is taken while that run is still
    going, so it would show it as running): the same fields and text form as the builder's."""
    text = lambda v: "" if v is None else str(v)
    return [{"run_id": r, "kind": k, "status": s, "started_at": text(a), "finished_at": text(f)} for r, k, s, a, f in con.execute(
        "SELECT run_id, kind, status, started_at, finished_at FROM RUNS ORDER BY started_at DESC LIMIT 25").fetchall()]


def base(con, named):
    """What every page needs as it opens: the small sections (totals, units, top items and owners, runs, ...) and
    the index of college, department and degree views (without their tables)."""
    found = stored_view(con, named, "base")
    if found and has(con, "RUNS"):
        found["base"]["runs"] = runs(con)
    return found


def review(con, named):
    """The Review page: current members' storage, the accounts that own nothing, and the list."""
    return stored_view(con, named, "review")


def scope(con, named, key):
    """One college, department or degree view, with its tables (largest layers and owners, trend, ...)."""
    edition = "full" if named else "published"
    found = con.execute("SELECT data FROM DASH_Scope WHERE edition = ? AND key = ?", [edition, key]).fetchone() if has(con, "DASH_Scope") else None
    if not found:
        return None
    return {"edition": edition, "scope": json.loads(found[0]), **(lookups_in(con, found[0]) if named else {})}


# which accounts each row of the removal table lists (the same rules as the page's groupMatcher)
GROUP_RULES = [("1.", lambda g, items: g.startswith("1.")), ("2.", lambda g, items: g.startswith("2.")),
               ("3.|empty", lambda g, items: g.startswith("3.") and items == 0), ("3.", lambda g, items: g.startswith("3.") and items > 0),
               ("kept", lambda g, items: g.startswith("Kept")), ("review: admin", lambda g, items: g.startswith("Review: admin")),
               ("review: empty", lambda g, items: g.startswith("Review: empty")), ("not yet", lambda g, items: g.startswith("Not yet"))]


def group(con, named, key):
    """The accounts of one removal group (full edition only), largest feature storage first."""
    if not named or not has(con, "DASH_GroupList"):
        return None
    label = key
    low = (label or "").lower()
    rule = next((f for prefix, f in GROUP_RULES if low.startswith(prefix.split("|")[0]) and ("|" not in prefix or prefix.split("|")[1] in low)), None)
    if rule is None:
        return None
    found = [x for x in rows(con, "SELECT * FROM DASH_GroupList ORDER BY feature_gb DESC, items DESC, user") if rule(x["rgroup"] or "", x["items"])]
    return {"edition": "full", "group": label, "rows": found, **lookups(con, [x["user"] for x in found] + [x["supervisor"] for x in found])}


def search(con, named, q, limit=25):
    """People matching q (part of a name, NetID, account name or email), best matches first: an exact match, then
    names or accounts starting with it, then containing it (Dan, 22 October 2026). Full edition only."""
    q = (q or "").strip().lower()
    if not named or len(q) < 2 or not has(con, "DASH_Account"):
        return None
    names = dict(con.execute("SELECT key, name FROM DASH_Names").fetchall())
    found = []
    for k, user, email, person, login, cls, proj, unit, sub, last, items, fgb in con.execute(
            "SELECT key, user, email, person, login, classification, projected, unit, sub_unit, last_login, items, feature_gb FROM DASH_Account").fetchall():
        name = names.get(norm(k)) or names.get(norm(email)) or ""
        fields = [x.lower() for x in (name, user or "", email or "", person or "") if x]
        if not any(q in f_ for f_ in fields):
            continue
        rank = 0 if any(f_ == q for f_ in fields) else 1 if any(f_.startswith(q) or any(w.startswith(q) for w in f_.split()) for f_ in fields) else 2
        found.append((rank, -(fgb or 0), {"key": k, "user": user, "name": name, "email": email or "", "login": login, "classification": cls,
                                          "projected": proj, "unit": unit, "sub_unit": sub or "", "last_login": last or "", "items": items,
                                          "feature_gb": r(fgb)}))
    found.sort(key=lambda x: (x[0], x[1]))
    return {"edition": "full", "q": q, "count": len(found), "people": [x[2] for x in found[:limit]]}


SELECTION_DATES = {"created": "created", "modified": "modified", "viewed": "last_viewed"}
SELECTION_LIMIT = 50000


def selection(con, named, date="created", year_from=None, year_to=None, tiers=(), never=False):
    """Layers for proposed deletion by period (Proposed removals; Dan, 22 October 2026). date: which date puts a
    layer in the period (created, modified or viewed). Answers, for the period: each tier's layers, people and GB,
    and, for the tiers asked for, the people with their layers in it (largest first). never: with date=viewed,
    also layers never viewed. Full edition only; the site never deletes anything (the CSV made from this is a
    proposal for review)."""
    if not named or not has(con, "DASH_Item"):
        return None
    col = SELECTION_DATES.get(date)
    if not col:
        return None
    lo, hi = con.execute(f"SELECT MIN(year({col})), MAX(year({col})) FROM DASH_Item").fetchone()
    year_from, year_to = int(year_from or lo), int(year_to or hi)
    period = f"((year(i.{col}) BETWEEN {year_from} AND {year_to})" + (f" OR i.{col} IS NULL)" if never and col == "last_viewed" else ")")
    summary = [{"tier": tr or "", "layers": n_, "people": p_, "feature_gb": r(f_), "file_gb": r(fi_)} for tr, n_, p_, f_, fi_ in con.execute(
        f"SELECT tier, COUNT(*), COUNT(DISTINCT key), SUM(feature_gb), SUM(file_gb) FROM DASH_Item i WHERE {period} GROUP BY tier ORDER BY tier").fetchall()]
    out = {"edition": "full", "date": date, "from": year_from, "to": year_to, "never": bool(never and col == "last_viewed"),
           "years": [lo, hi], "tiers": summary, "people": [], "too_many": False, "limit": SELECTION_LIMIT}
    tiers = [x for x in tiers if x]
    if not tiers:
        return out
    chosen = sum(s["layers"] for s in summary if s["tier"] in tiers)
    if chosen > SELECTION_LIMIT:
        out["too_many"] = True
        return out
    rows_ = con.execute(f"""
        SELECT i.key, i.item_id, i.title, i.item_type, i.created, i.modified, i.last_viewed, i.feature_gb, i.file_gb, i.share, i.tier,
               a.user, a.email, a.login, a.classification, a.projected, a.unit, a.sub_unit, a.supervisor, a.protected
        FROM DASH_Item i JOIN DASH_Account a ON a.key = i.key
        WHERE {period} AND i.tier IN (SELECT UNNEST(?)) ORDER BY i.key, i.feature_gb DESC""", [tiers]).fetchall()
    people = {}
    text = lambda v: "" if v is None else str(v)
    for (k, iid, title, typ, cr, mo, vw, fg, fi, share, tier, user, email, login, cls, proj, unit, sub, supv, prot) in rows_:
        p_ = people.setdefault(k, {"key": k, "user": user, "email": email or "", "login": login, "classification": cls, "projected": proj,
                                   "protected": prot or "",
                                   "tier": tier, "unit": unit, "sub_unit": sub or "", "supervisor": supv or "", "feature_gb": 0.0, "file_gb": 0.0, "items": []})
        p_["items"].append([iid, title, typ, text(cr), text(mo), text(vw), r(fg), r(fi), share or ""])
        p_["feature_gb"] += fg or 0
        p_["file_gb"] += fi or 0
    out["people"] = sorted(({**p_, "feature_gb": r(p_["feature_gb"]), "file_gb": r(p_["file_gb"])} for p_ in people.values()),
                           key=lambda p_: -(p_["feature_gb"] + p_["file_gb"]))
    out.update(lookups(con, [p_["user"] for p_ in out["people"]] + [p_["supervisor"] for p_ in out["people"]]))
    return out


VIEWS = {"base": base, "overview": overview, "history": history, "people": people, "review": review}
KEYED_VIEWS = {"account": account, "group": group, "scope": scope}   # /api/v1/<view>/<key>
