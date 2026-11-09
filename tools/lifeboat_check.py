"""Read-only check that ArcGIS Online items are in the Lifeboat backup, complete and usable as they were.

Why: nothing with content is deleted from ArcGIS Online before its Lifeboat copy is shown to be correct
(docs/backup-check-plan.md). Backup My Org keeps the original item ids, so items are matched by id.

For each ArcGIS Online item created in the chosen years, it checks the Lifeboat copy:
  found        an item with the same id is in the Lifeboat
  type         same item type
  owner        same owner, and that account exists in the Lifeboat
  sharing      same sharing level (private, organization, public)
  groups       every group it is shared with exists in the Lifeboat (by id, else title and owner), the copy is
               shared with it, and none of the group's members is missing in the Lifeboat
  structure    feature services: same layers and tables, fields (name and type), geometry type, spatial reference
  counts       feature services: same number of features and of attachments in every layer and table
  files        file items: same size
  dependencies every item id its definition refers to is in the Lifeboat, and no URL in the copy points at
               ArcGIS Online
  usable       feature services: the first features (by object id) read from the copy equal the original's
  unchanged    the original was not modified after the copy was made
Each item also gets warnings for the review page: public, delete protection, content status, sync, and use by
field or survey apps (kinds of use a dependency map cannot see).

Results go to their own database (default data/lifeboat-check.duckdb, never committed):
  LIFEBOAT_Items     the Lifeboat's item list, one row per item per run
  LIFEBOAT_Copy      what was read from each Lifeboat copy (layers, counts, sample, sharing, references), read
                     once: the backup is not run again, so a copy is read again only if its modified time changes
  LIFEBOAT_Group     each Lifeboat group's members, read once
  AGOL_Group         each ArcGIS Online group an item is shared with: title, owner, access, members; one row per
                     group per run (the review page shows who can still use an item, from these)
  LIFEBOAT_Check     one row per item per check, written as each item is checked (a stopped run loses nothing);
                     the newest counts
  LIFEBOAT_Runs      one row per run, with the time each step took
An item is checked again only when its ArcGIS Online or Lifeboat modified time, or CHECK_VERSION, changed; otherwise
its last result stands. --recheck checks every item again.

Signs in to ArcGIS Online as the read-only service account and to the Lifeboat as its read-only account
(LIFEBOAT_URL, LIFEBOAT_USERNAME, LIFEBOAT_PASSWORD in .env), and refuses unless both can only view.
Reads only; changes nothing in either portal.

Usage (ArcGIS Pro clone Python):
    python tools/lifeboat_check.py --from-year 2010 --to-year 2013
"""
import argparse
import collections
import datetime
import json
import os
import re
import sys
import time
import warnings

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import agol_db as db  # noqa: E402

warnings.filterwarnings("ignore", category=DeprecationWarning)
CHECK_VERSION = 7   # raise when a test changes, so every item is checked again
TESTS = ["found", "type", "owner", "sharing", "groups", "structure", "counts", "files", "dependencies", "usable", "unchanged"]
FILE_TYPES = {"PDF", "Shapefile", "File Geodatabase", "CSV", "Microsoft Excel", "Microsoft Word", "Microsoft Powerpoint",
              "Image", "KML", "GeoJson", "Service Definition", "Mobile Map Package", "Map Package", "Layer Package",
              "Tile Package", "Vector Tile Package", "Scene Package", "Code Attachment", "Report Template",
              "Project Package", "Geoprocessing Package", "Locator Package", "Desktop Application", "ZIP", "Visio Document"}
FIELD_USE = ("Survey123", "Collector", "FieldMaps", "Field Maps", "QuickCapture", "Workforce", "Form")
AGOL_HOST = re.compile(r"https?://[^/\s\"']*arcgis\.com/[^\s\"']*", re.I)
ITEM_ID = re.compile(r"\b[0-9a-f]{32}\b")
SAMPLE = 5   # features compared per layer
SKIP_FIELDS = {"globalid", "created_date", "last_edited_date", "creationdate", "editdate", "created_user", "last_edited_user",
               "creator", "editor", "shape__area", "shape__length"}
# fields the Lifeboat adds that ArcGIS Online does not show: a copy may have these as well, never fewer fields
SYSTEM_FIELDS = {"globalid", "shape__area", "shape__length"}


class Timer:
    """Seconds spent in each step, for the run's summary (8 November 2026, Dan asked what takes the time)."""
    def __init__(self):
        self.s = collections.Counter()

    def __call__(self, step, fn, *a, **k):
        t = time.perf_counter()
        try:
            return fn(*a, **k)
        finally:
            self.s[step] += time.perf_counter() - t


T = Timer()


def lifeboat_gis():
    """Signs in to the Lifeboat as its read-only account, and refuses unless it can only view."""
    from arcgis.gis import GIS
    url, user, password = (os.environ.get(k) for k in ("LIFEBOAT_URL", "LIFEBOAT_USERNAME", "LIFEBOAT_PASSWORD"))
    if not (url and user and password):
        raise SystemExit("REFUSED: LIFEBOAT_URL, LIFEBOAT_USERNAME and LIFEBOAT_PASSWORD are not set (.env beside "
                         "agol_db.py). Nothing was read.")
    gis = GIS(url, user, password)
    me = db.check_read_only(gis)
    print(f"connected to the Lifeboat as {me.username} (read-only: {len(me.privileges)} view privileges, checked)")
    return gis


def ms_date(ms):
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).replace(tzinfo=None) if ms else None


def err(e):
    return f"error: {e}"[:200]


def sharing_of(item):
    """('private' | 'org' | 'public', [group ids])."""
    level = {"shared": "private"}.get(item.access or "private", item.access or "private")
    # one request (content/items/<id>/groups): the library's sharing manager took about 7 s an item (8 November 2026)
    r = item._gis._con.get(f"{item._gis._portal.resturl}content/items/{item.id}/groups")
    groups = sorted({g["id"] for k in ("admin", "member", "other") for g in r.get(k, [])})
    return level, groups


def members_of(group):
    m = group.get_members()
    return sorted({u.lower() for u in [m.get("owner")] + m.get("admins", []) + m.get("users", []) if u})


def layer_facts(layer):
    """What one layer or table is: structure, counts and its first features."""
    p = layer.properties
    sr = (p.get("extent") or {}).get("spatialReference", {})
    oid = p.get("objectIdField") or "objectid"
    out = {"name": p.get("name"), "geometry": p.get("geometryType"), "sr": sr.get("latestWkid") or sr.get("wkid"),
           "fields": sorted([f["name"].lower(), f["type"]] for f in p.get("fields", [])),
           "sync": "Sync" in str(p.get("capabilities", ""))}
    try:
        out["features"] = T("feature counts", layer.query, where="1=1", return_count_only=True)
    except Exception as e:
        out["features"] = err(e)
    if p.get("hasAttachments"):
        try:   # one request for the attachment list of the whole layer (returnCountOnly is not honoured)
            r = T("attachment counts", layer._con.post, layer.url + "/queryAttachments",
                  {"definitionExpression": "1=1", "returnUrl": "false", "f": "json"})
            out["attachments"] = sum(len(g.get("attachmentInfos", [])) for g in r.get("attachmentGroups", []))
        except Exception as e:
            out["attachments"] = err(e)
    if isinstance(out["features"], int) and out["features"]:
        try:
            fs = T("feature samples", layer.query, where="1=1", out_fields="*", return_geometry=True,
                   order_by_fields=oid, result_record_count=SAMPLE)
            out["sample"] = [[{k.lower(): v for k, v in f.attributes.items() if k.lower() not in SKIP_FIELDS},
                              re.sub(r"(\d+\.\d{6})\d+", r"\1", json.dumps(f.geometry, sort_keys=True))[:2000] if f.geometry else ""]
                             for f in fs.features[:SAMPLE]]
        except Exception as e:
            out["sample"] = err(e)
    return out


def item_facts(item):
    """Everything the tests compare, read from one item (either portal)."""
    f = {"type": item.type, "owner": item.owner, "size": item.size, "created": item.created, "modified": item.modified,
         "keywords": item.typeKeywords or [], "protected": bool(getattr(item, "protected", False)),
         "content_status": getattr(item, "content_status", "") or ""}
    f["access"], f["groups"] = T("sharing", sharing_of, item)
    data = None
    if item.type not in FILE_TYPES:   # a file item's data is the file itself, downloaded (took most of the time)
        try:
            data = T("definitions", item.get_data, try_json=True)
        except Exception:
            data = None
    text = (json.dumps(data) if isinstance(data, (dict, list)) else (data if isinstance(data, str) else "")) + " " + (item.url or "")
    f["refs"] = sorted(set(ITEM_ID.findall(text)) - {item.id})
    f["agol_urls"] = sorted(set(m.group(0) for m in AGOL_HOST.finditer(text)))[:20]
    if item.type in ("Feature Service", "Feature Layer"):
        try:
            parts = T("layer lists", lambda: [("layer", l) for l in (item.layers or [])] + [("table", t) for t in (item.tables or [])])
            f["layers"] = [[k, T("layer structure", lambda x=x: x.properties) and layer_facts(x)] for k, x in parts]
        except Exception as e:
            f["layers"] = err(e)
    return f


NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")


def same_sample(sa, sl):
    """The first features of a layer, original and copy, are the same: same attributes (leaving out fields the
    system writes) and the same geometry within the data store's precision (the Lifeboat stores coordinates a
    fraction of a millimetre, or a millionth of a degree, apart from ArcGIS Online)."""
    if not (isinstance(sa, list) and isinstance(sl, list)) or len(sa) != len(sl):
        return False
    for (aa, ag), (la, lg) in zip(sa, sl):
        drop = lambda d: {k: v for k, v in d.items() if k not in SKIP_FIELDS}
        if drop(aa) != drop(la):
            return False
        an, ln = [float(x) for x in NUMBER.findall(ag)], [float(x) for x in NUMBER.findall(lg)]
        if len(an) != len(ln) or any(abs(x - y) > max(2e-6, 2e-9 * abs(x)) for x, y in zip(an, ln)):
            return False
    return True


def compare(a, l, a_groups, lb_group, lb_by_key, lb_items, lb_user, ours):
    """The tests, from the facts of the ArcGIS Online item a and its Lifeboat copy l (or None)."""
    t, d, w = {k: None for k in TESTS}, {}, []
    if a["access"] == "public":
        w.append("public: may be used outside ArcGIS Online")
    if a["protected"]:
        w.append("delete protection is on")
    if a["content_status"]:
        w.append(f"content status: {a['content_status']}")
    if any(m in k for k in a["keywords"] for m in FIELD_USE):
        w.append("used by a field or survey app")
    if isinstance(a.get("layers"), list) and any(x[1].get("sync") for x in a["layers"]):
        w.append("sync is on: mobile devices may hold copies")
    t["found"] = l is not None
    if l is None:
        return t, d, w
    t["type"] = a["type"] == l["type"]
    exists = lb_user(l["owner"])
    t["owner"] = a["owner"].lower() == l["owner"].lower() and exists
    if not t["owner"]:
        d["owner"] = f"ArcGIS Online {a['owner']}, Lifeboat {l['owner']}" + ("" if exists else " (no such Lifeboat user)")
    t["sharing"] = a["access"] == l["access"]
    if not t["sharing"]:
        d["sharing"] = f"ArcGIS Online {a['access']}, Lifeboat {l['access']}"
    bad = []
    for gid in a["groups"]:
        ag = a_groups.get(gid)
        lid = gid if lb_group(gid) is not None else (lb_by_key.get((ag["title"].lower(), ag["owner"].lower())) if ag else None)
        lg = lb_group(lid) if lid else None
        name = ag["title"] if ag else gid
        if lg is None:
            bad.append(f"group {name}: not in the Lifeboat")
            continue
        if lid not in l["groups"]:
            bad.append(f"group {name}: the copy is not shared with it")
        if ag and isinstance(ag.get("members"), list) and isinstance(lg, list):
            missing = set(ag["members"]) - set(lg)
            if missing:
                bad.append(f"group {name}: {len(missing)} of {len(ag['members'])} members missing in the Lifeboat")
    t["groups"] = not bad
    if bad:
        d["groups"] = bad
    if "layers" in a:
        al, ll = a.get("layers"), l.get("layers")
        if not (isinstance(al, list) and isinstance(ll, list)):
            t["structure"] = t["counts"] = t["usable"] = False
            d["layers"] = {"agol": al if isinstance(al, str) else "", "lifeboat": ll if isinstance(ll, str) else "missing"}
        else:
            shape = lambda xs: [(k, x["name"], x["geometry"], x["sr"]) for k, x in xs]
            fields_ok = all({tuple(f) for f in ax["fields"]} <= {tuple(f) for f in lx["fields"]}
                            and {f[0] for f in lx["fields"]} - {f[0] for f in ax["fields"]} <= SYSTEM_FIELDS
                            for (_, ax), (_, lx) in zip(al, ll))
            t["structure"] = shape(al) == shape(ll) and fields_ok
            counts, usable, rows = len(al) == len(ll), True, []
            for (k, ax), (_, lx) in zip(al, ll):
                row = {"layer": ax["name"], "features": [ax.get("features"), lx.get("features")]}
                if ax.get("features") != lx.get("features") or isinstance(ax.get("features"), str):
                    counts = False
                if "attachments" in ax:
                    row["attachments"] = [ax["attachments"], lx.get("attachments")]
                    if ax["attachments"] != lx.get("attachments") or isinstance(ax["attachments"], str):
                        counts = False
                if "sample" in ax and not same_sample(ax["sample"], lx.get("sample")):
                    usable = False
                    row["sample"] = "differs" if isinstance(ax.get("sample"), list) and isinstance(lx.get("sample"), list) else "could not be read"
                rows.append(row)
            t["counts"], t["usable"] = counts, usable and bool(ll)
            d["layers"] = rows
            if not t["structure"]:
                d["structure"] = {"agol": [(k, x["name"], x["geometry"], len(x["fields"]), x["sr"]) for k, x in al],
                                  "lifeboat": [(k, x["name"], x["geometry"], len(x["fields"]), x["sr"]) for k, x in ll]}
    elif a["type"] in FILE_TYPES:
        t["files"] = a["size"] == l["size"]
        if not t["files"]:
            d["files"] = f"ArcGIS Online {a['size']} bytes, Lifeboat {l['size']} bytes"
    # only references to this organization's items and services count: Esri's basemaps, symbols and Living Atlas
    # layers stay where they are (8 November 2026: most "links back" were static.arcgis.com)
    missing = [i for i in a["refs"] if ours["item"](i) and i not in lb_items]
    own_urls = [u for u in l["agol_urls"] if ours["url"](u)]
    t["dependencies"] = not missing and not own_urls
    if missing:
        d["dependencies_missing"] = missing
    if own_urls:
        d["points_at_agol"] = own_urls
    d["depends_on"] = [i for i in a["refs"] if ours["item"](i)]   # this organization's items only
    t["unchanged"] = (a["modified"] or 0) <= (l["created"] or 0)
    if not t["unchanged"]:
        d["unchanged"] = f"modified in ArcGIS Online {ms_date(a['modified'])}, after the copy was made {ms_date(l['created'])}"
    return t, d, w


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--from-year", type=int, required=True)
    ap.add_argument("--to-year", type=int, required=True)
    ap.add_argument("--db", default=os.path.join(db.SCRIPT_DIR, "data", "lifeboat-check.duckdb"))
    ap.add_argument("--recheck", action="store_true", help="check every item again, even unchanged ones")
    args = ap.parse_args()
    run_id = datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + "-lifeboat"
    started = time.perf_counter()
    agol, lb_gis = T("sign-in", db.agol_gis), T("sign-in", lifeboat_gis)
    con = db.connect(path=args.db)
    now = datetime.datetime.now()

    print("reading the Lifeboat's item and group lists ...")
    # as plain records: an Item object reads its size with a request of its own (8 November 2026: saving the list
    # that way took one request per item); the Item is fetched only for the copies that are checked
    lb_all = T("Lifeboat lists", lambda: lb_gis.content.advanced_search(query=f"orgid:{lb_gis.properties.id}", max_items=-1, as_dict=True)["results"])
    lb_items = {i["id"]: i for i in lb_all}
    lb_group_list = T("Lifeboat lists", lb_gis.groups.search, query="*", max_groups=100000)
    lb_by_key = {(g.title.lower(), g.owner.lower()): g.id for g in lb_group_list}
    lb_group_obj = {g.id: g for g in lb_group_list}
    db.write(con, "LIFEBOAT_Items", pd.DataFrame([{"run_id": run_id, "at": now, "item_id": i["id"], "title": i.get("title"), "type": i.get("type"),
                                                   "owner": i.get("owner"), "created": ms_date(i.get("created")), "modified": ms_date(i.get("modified")),
                                                   "access": i.get("access")} for i in lb_all]), mode="append")
    print(f"  {len(lb_items)} items, {len(lb_group_list)} groups")

    # what was read from the Lifeboat before: copies and group members, kept (the backup is not run again)
    con.execute("CREATE TABLE IF NOT EXISTS LIFEBOAT_Copy (item_id VARCHAR, modified BIGINT, read_at TIMESTAMP, facts VARCHAR)")
    con.execute("CREATE TABLE IF NOT EXISTS LIFEBOAT_Group (group_id VARCHAR, read_at TIMESTAMP, members VARCHAR)")
    copies = {(i, m): json.loads(f) for i, m, f in con.execute("SELECT item_id, modified, facts FROM LIFEBOAT_Copy").fetchall()}
    groups_read = {g: json.loads(m) for g, m in con.execute("SELECT group_id, members FROM LIFEBOAT_Group").fetchall()}

    def lb_copy(rec):
        key = (rec["id"], rec.get("modified") or 0)
        if key not in copies:
            item = T("item details", lb_gis.content.get, rec["id"])
            copies[key] = item_facts(item)
            con.execute("INSERT INTO LIFEBOAT_Copy VALUES (?, ?, ?, ?)", [rec["id"], rec.get("modified") or 0, datetime.datetime.now(), json.dumps(copies[key], default=str)])
        return copies[key]

    def lb_group(gid):
        """A Lifeboat group's members (None: no such group), read once."""
        if gid not in lb_group_obj:
            return None
        if gid not in groups_read:
            try:
                groups_read[gid] = T("group members", members_of, lb_group_obj[gid])
            except Exception as e:
                groups_read[gid] = err(e)
            con.execute("INSERT INTO LIFEBOAT_Group VALUES (?, ?, ?)", [gid, datetime.datetime.now(), json.dumps(groups_read[gid])])
        return groups_read[gid]

    users = {}

    def lb_user(name):
        if name.lower() not in users:
            users[name.lower()] = T("owners", lb_gis.users.get, name) is not None
        return users[name.lower()]

    start = int(datetime.datetime(args.from_year, 1, 1, tzinfo=datetime.timezone.utc).timestamp() * 1000)
    end = int(datetime.datetime(args.to_year + 1, 1, 1, tzinfo=datetime.timezone.utc).timestamp() * 1000) - 1
    print(f"reading ArcGIS Online items created {args.from_year} to {args.to_year} ...")
    agol_items = T("ArcGIS Online list", lambda: agol.content.advanced_search(
        query=f"orgid:{agol.properties.id} AND created:[{start:019d} TO {end:019d}]", max_items=-1)["results"])
    print(f"  {len(agol_items)} items")

    org_id, url_key = agol.properties.id, (agol.properties.get("urlKey") or "").lower()
    own_items = {}

    def own_item(iid):
        """Is iid an item of this organization (not Esri's, and not some other 32-character code)?"""
        if iid not in own_items:
            try:
                r = T("references", agol._con.get, f"{agol._portal.resturl}content/items/{iid}")
                own_items[iid] = r.get("orgId") == org_id
            except Exception:
                own_items[iid] = False
        return own_items[iid]

    ours = {"item": own_item, "url": lambda u: org_id in u or (url_key and f"//{url_key}.maps.arcgis.com" in u.lower())}

    if db.exists(con, "LIFEBOAT_Check"):   # the item's type was first stored under "type", the name of a test
        con.execute("ALTER TABLE LIFEBOAT_Check ADD COLUMN IF NOT EXISTS item_type VARCHAR")   # and its owner under "owner"
        con.execute("ALTER TABLE LIFEBOAT_Check ADD COLUMN IF NOT EXISTS item_owner VARCHAR")
        con.execute("ALTER TABLE LIFEBOAT_Check ADD COLUMN IF NOT EXISTS access VARCHAR")      # version 6: sharing, for
        con.execute("ALTER TABLE LIFEBOAT_Check ADD COLUMN IF NOT EXISTS group_ids VARCHAR")   # the review page ("groups" is a test)
    previous = {}
    if db.exists(con, "LIFEBOAT_Check") and not args.recheck:
        for r in con.execute("SELECT * FROM LIFEBOAT_Check QUALIFY ROW_NUMBER() OVER (PARTITION BY item_id ORDER BY checked_at DESC) = 1").df().to_dict("records"):
            previous[r["item_id"]] = r
    a_groups, checked, reused, passed = {}, 0, 0, 0
    types = {**{k: "BOOLEAN" for k in TESTS + ["passed"]}, "agol_modified": "BIGINT", "lifeboat_modified": "BIGINT",
             "check_version": "INTEGER", "backup_year": "INTEGER", "seconds": "DOUBLE"}
    for n, a in enumerate(agol_items, 1):
        lb = lb_items.get(a.id)
        basis = {"agol_modified": a.modified or 0, "lifeboat_modified": (lb.get("modified") or 0) if lb else 0, "check_version": CHECK_VERSION}
        p = previous.get(a.id)
        if p and all(p[k] == v for k, v in basis.items()):
            reused += 1
            passed += bool(p["passed"])
            continue
        t0 = time.perf_counter()
        af = {"access": None, "groups": []}
        try:
            af = item_facts(a)
            for gid in af["groups"]:
                if gid not in a_groups:
                    try:
                        g = T("group members", agol.groups.get, gid)
                        a_groups[gid] = {"title": g.title, "owner": g.owner, "access": g.access,
                                         "members": T("group members", members_of, g)} if g else None
                    except Exception:
                        a_groups[gid] = None
            lf = lb_copy(lb) if lb else None
            tests, detail, warns = compare(af, lf, a_groups, lb_group, lb_by_key, lb_items, lb_user, ours)
        except Exception as e:
            tests, detail, warns = {k: None for k in TESTS}, {"error": str(e)[:500]}, []
        applicable = [v for v in tests.values() if v is not None]
        ok = bool(applicable) and all(applicable) and "error" not in detail
        row = {"run_id": run_id, "checked_at": datetime.datetime.now(), "item_id": a.id, "title": a.title, "item_type": a.type,
               "item_owner": a.owner, "created": ms_date(a.created), "modified": ms_date(a.modified), "backup_year": ms_date(a.created).year,
               "lifeboat_item_id": lb["id"] if lb else "", "lifeboat_created": ms_date(lb.get("created")) if lb else None,
               "access": af["access"], "group_ids": json.dumps(af["groups"]), **basis, **tests, "passed": ok, "failed_tests": ", ".join(k for k, v in tests.items() if v is False),
               "warnings": "; ".join(warns), "detail": json.dumps(detail, default=str), "seconds": round(time.perf_counter() - t0, 2)}
        frame = pd.DataFrame([row])
        for k in TESTS + ["passed"]:
            frame[k] = frame[k].astype("object")
        db.write(con, "LIFEBOAT_Check", frame, mode="append", types=types)   # saved as it goes
        checked += 1
        passed += ok
        if n % 10 == 0:
            print(f"  {n} of {len(agol_items)} ({checked} checked, {reused} unchanged)")

    if a_groups:
        db.write(con, "AGOL_Group", pd.DataFrame([{"run_id": run_id, "read_at": now, "group_id": gid, "found": g is not None,
                                                   "title": (g or {}).get("title"), "owner": (g or {}).get("owner"), "access": (g or {}).get("access"),
                                                   "members": json.dumps((g or {}).get("members"))} for gid, g in a_groups.items()]), mode="append")
    total = time.perf_counter() - started
    timing = {k: round(v, 1) for k, v in T.s.most_common()}
    db.write(con, "LIFEBOAT_Runs", pd.DataFrame([{"run_id": run_id, "at": now, "from_year": args.from_year, "to_year": args.to_year,
                                                  "items": len(agol_items), "passed": passed, "checked": checked, "carried_forward": reused,
                                                  "check_version": CHECK_VERSION, "seconds": round(total, 1), "timing": json.dumps(timing)}]), mode="append")
    latest = con.execute("SELECT * FROM LIFEBOAT_Check QUALIFY ROW_NUMBER() OVER (PARTITION BY item_id ORDER BY checked_at DESC) = 1").df()
    latest = latest[latest["backup_year"].between(args.from_year, args.to_year)]
    con.close()
    print(f"run {run_id}: {len(agol_items)} items, {passed} passed; {checked} checked, {reused} unchanged, in {total / 60:.1f} min")
    print("time by step (s):", timing)
    for k in TESTS:
        n_ = int((latest[k] == False).sum())  # noqa: E712
        if n_:
            print(f"  failing {k}: {n_}")


if __name__ == "__main__":
    main()
