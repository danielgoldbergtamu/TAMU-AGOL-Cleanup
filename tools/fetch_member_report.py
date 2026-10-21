"""Downloads the newest AGOL member report (scheduled 'OrganizationMembers_' report item) to a CSV.

Read-only. Signs in as agol_db.agol_gis() does: only as the read-only service account (AGOL_USERNAME /
AGOL_PASSWORD, from .env on a workstation), checked to have view privileges only.

    python tools/fetch_member_report.py --out <folder>/members.csv
"""
import argparse
import datetime
import os
import tempfile

import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agol_db import agol_gis  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, help="CSV path to write")
    args = parser.parse_args()
    gis = agol_gis()
    found = gis.content.search('title:"OrganizationMembers_"', max_items=1, sort_field="created", sort_order="desc")
    if not found:
        raise SystemExit("No OrganizationMembers_ report found in AGOL.")
    report = found[0]
    age = (datetime.datetime.now() - datetime.datetime.fromtimestamp(report.created / 1000)).days
    frame = pd.read_csv(report.download(save_path=tempfile.gettempdir()))
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    frame.to_csv(args.out, index=False)
    print(f"{report.title}: {len(frame)} members ({age} days old) -> {args.out}")
    if age > 7:
        print("WARNING: the newest member report is more than a week old. Check the scheduled report in AGOL.")


if __name__ == "__main__":
    main()
