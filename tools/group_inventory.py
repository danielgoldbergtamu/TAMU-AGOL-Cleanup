"""Read-only inventory of the organization's groups and the items shared to each.

Why: content of people who have left can often be traced to a unit through the groups it is shared with,
whose owners are usually still here. The item report gives only a count of groups per item, not which ones.

Writes, into the output folder:
  groups.csv       one row per group: id, title, owner, access, created, modified, tags
  group_items.csv  one row per (group, item) pair
  progress.log     a line per 100 groups, flushed as it goes, readable while the run is going

Results are appended as each group is read, so a run that stops part-way keeps everything it got;
re-running with --resume skips groups already in groups.csv. Reads only; changes nothing in ArcGIS Online.

Usage (ArcGIS Pro clone Python, Pro signed in as an administrator):
    python tools/group_inventory.py --out reports/runs/<run id>
"""
import argparse
import csv
import datetime
import os
import time

from arcgis.gis import GIS

GROUP_FIELDS = ["group_id", "title", "owner", "access", "created", "modified", "tags", "item_count"]


def stamp(ms):
    return datetime.datetime.fromtimestamp(ms / 1000).isoformat(timespec="seconds") if ms else ""


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", required=True, help="output folder")
    parser.add_argument("--resume", action="store_true", help="skip groups already written to groups.csv")
    args = parser.parse_args()
    os.makedirs(args.out, exist_ok=True)
    groups_path = os.path.join(args.out, "groups.csv")
    items_path = os.path.join(args.out, "group_items.csv")
    log = open(os.path.join(args.out, "progress.log"), "a", encoding="utf-8", buffering=1)

    def say(message):
        line = f"{datetime.datetime.now():%H:%M:%S} {message}"
        print(line, flush=True)
        log.write(line + "\n")

    done = set()
    if args.resume and os.path.exists(groups_path):
        with open(groups_path, encoding="utf-8") as fh:
            done = {r["group_id"] for r in csv.DictReader(fh)}

    gis = GIS("home")
    groups = gis.groups.search(f"orgid:{gis.properties.id}", max_groups=20000)
    say(f"{len(groups)} groups in the organization; {len(done)} already read")

    new_files = not (args.resume and os.path.exists(groups_path))
    with open(groups_path, "a" if not new_files else "w", newline="", encoding="utf-8") as gfh, \
         open(items_path, "a" if not new_files else "w", newline="", encoding="utf-8") as ifh:
        gw = csv.DictWriter(gfh, fieldnames=GROUP_FIELDS)
        iw = csv.writer(ifh)
        if new_files:
            gw.writeheader()
            iw.writerow(["group_id", "item_id"])
        started, read, pairs, failed = time.time(), 0, 0, 0
        for n, group in enumerate(groups, 1):
            if group.id in done:
                continue
            try:
                items = group.content(max_items=10000)
            except Exception as error:  # a group we cannot read is recorded and skipped
                failed += 1
                say(f"could not read group {group.id}: {type(error).__name__}")
                items = None
            gw.writerow({"group_id": group.id, "title": group.title, "owner": group.owner, "access": group.access,
                         "created": stamp(getattr(group, "created", None)), "modified": stamp(getattr(group, "modified", None)),
                         "tags": "; ".join(group.tags or []), "item_count": "" if items is None else len(items)})
            for item in items or []:
                iw.writerow([group.id, item.id])
            pairs += len(items or [])
            read += 1
            gfh.flush()
            ifh.flush()
            if read % 100 == 0 or n == len(groups):
                rate = read / max(time.time() - started, 1)
                left = (len(groups) - len(done) - read) / rate / 60 if rate else 0
                say(f"{n}/{len(groups)} groups, {pairs:,} group-item pairs, {failed} unreadable, about {left:.0f} min left")
    say(f"done: {read} groups read this run, {pairs:,} group-item pairs, {failed} unreadable")


if __name__ == "__main__":
    main()
