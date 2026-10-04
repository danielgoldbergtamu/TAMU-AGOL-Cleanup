# File Name: TAMU_AGOL_MQS.py
# Description: Looks up each ArcGIS Online member in Texas A&M's directory (NetID Attribute API,
#              mqs.tamu.edu/rest) for department, affiliation, student classification and major, and
#              supervisor, so usage can be reported by department.
# Date: 2026-10-03
#
# Why on every run: the directory fills student fields (classification, major) only while a student is
# enrolled in a current or future semester, and an employee's department and supervisor only while they
# are employed. Once someone leaves, the association is gone. Like the Entra supervisor, it has to be
# captured while it exists and kept in history (see CLAUDE.md).
#
# API (docs.security.tamu.edu, Identity Security > Attribute Services):
#   - requests must come from the campus network;
#   - every request is signed: Authorization: TAM <client id>:<Base64(HMAC-SHA256(secret, path\ndate\nid))>
#     with the same date in the Date header;
#   - one person per request: GET /rest/directory/netid/<netid>/json/ (trailing slash required);
#   - a new client sees public, unsuppressed entries only. Suppressed people and extra attributes need an
#     approved request to identity@tamu.edu; UIN-based fields need UIN access.  # leak-check: allow (public role mailbox)
#
# Configuration (.env beside this file; never commit it):
#   MQS_CLIENT_ID, MQS_SHARED_SECRET   from mqs.tamu.edu/rest/clients/
#   ORG_EMAIL_DOMAIN, AGOL_USERNAME_SUFFIX   as for TAMU_AGOL_EntraID.py
#
# Usage (from the repo folder, on campus):
#   python TAMU_AGOL_MQS.py --input_csv_path <member report CSV> --limit 25 --output_csv_path <file>
#   python TAMU_AGOL_MQS.py --input_csv_path <member report CSV>

import argparse
import base64
import csv
import hashlib
import hmac
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from email.utils import formatdate

import requests
from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE = "https://mqs.tamu.edu"

# Attributes worth keeping, in output order. Lists (multi-valued) are joined with "; ".
ATTRIBUTES = [
    "tamuEduPersonNetID", "eduPersonPrimaryAffiliation", "eduPersonAffiliation", "tamuEduPersonAffiliation",
    "tamuEduPersonDepartmentName", "department", "title",
    "tamuEduPersonClassification", "tamuEduPersonClassificationName",
    "tamuEduPersonPrimaryMajor", "tamuEduPersonPrimaryMajorName", "tamuEduPersonMajor",
    "enrolledPrimaryMajor1College", "enrolledPrimaryMajor1CollegeName",
    "tamuEduPersonSupervisorUIN", "tamuStatus", "tamuDegreeAwarded",
]
OUTPUT_COLUMNS = ["Username", "NetIDTried", "MQS_Status", "HTTPStatus"] + ATTRIBUTES + ["OtherAttributes", "updated_date"]


def sign(path, client_id, secret):
    """Headers for one signed request, exactly as the API's authentication page describes."""
    when = formatdate(usegmt=True)                      # e.g. Tue, 04 May 2010 20:46:36 GMT
    auth_string = f"{path}\n{when}\n{client_id}"
    digest = hmac.new(secret.encode("latin-1"), auth_string.encode("latin-1"), hashlib.sha256).digest()
    return {"Date": when, "Authorization": f"TAM {client_id}:{base64.b64encode(digest).decode()}"}


def netid_for(row, domain, suffix):
    """NetID from an SSO username (netid@example.edu_example), or from a TAMU-family email on an ArcGIS-login
    account. Returns '' when there is no trustworthy NetID: a non-TAMU email is never turned into one,
    because the local part may be a different person's NetID."""
    username = (row.get("Username") or "").strip().lower()
    if suffix and username.endswith(suffix.lower()) and "@" in username:
        return username.split("@")[0]
    email = (row.get("Email") or "").strip().lower()
    local, _, mail_domain = email.rpartition("@")
    if local and (mail_domain == domain or mail_domain.endswith("." + domain)):
        return local
    return ""


def flatten(value):
    if isinstance(value, list):
        return "; ".join(str(v) for v in value)
    return "" if value is None else str(value)


def lookup(netid, client_id, secret, attempts=5):
    """(http status, entry dict or None). Retries throttling and server errors with back-off."""
    path = f"/rest/directory/netid/{requests.utils.quote(netid, safe='')}/json/"
    for attempt in range(attempts):
        try:
            response = requests.get(BASE + path, headers=sign(path, client_id, secret), timeout=30)
        except requests.RequestException:
            time.sleep(2 ** attempt)
            continue
        if response.status_code in (429, 500, 502, 503, 504):
            time.sleep(int(response.headers.get("Retry-After", 2 ** attempt)))
            continue
        if response.status_code != 200:
            return response.status_code, None
        body = response.json()
        # The API may return the entry itself or wrap it; accept either, and an empty result as "not found".
        if isinstance(body, list):
            body = body[0] if body else None
        if isinstance(body, dict) and len(body) == 1 and isinstance(next(iter(body.values())), (dict, list)):
            inner = next(iter(body.values()))
            body = inner[0] if isinstance(inner, list) and inner else inner if isinstance(inner, dict) else None
        return 200, body or None
    return 429, None


def main():
    load_dotenv(os.path.join(SCRIPT_DIR, ".env"), override=True)
    parser = argparse.ArgumentParser(description="Look up AGOL members in the TAMU directory (MQS).")
    parser.add_argument("--input_csv_path", required=True, help="AGOL member report (Username, Email)")
    parser.add_argument("--output_csv_path", default=os.path.join(SCRIPT_DIR, "reports", f"MQS_Directory_Status_{time.strftime('%Y%m%d-%H%M%S')}.csv"),
                        help="defaults to a new timestamped file, so a run never overwrites an earlier one")
    parser.add_argument("--limit", type=int, default=0, help="only the first N members (for testing)")
    parser.add_argument("--workers", type=int, default=4, help="requests in flight at once")
    args = parser.parse_args()

    client_id, secret = os.getenv("MQS_CLIENT_ID"), os.getenv("MQS_SHARED_SECRET")
    domain, suffix = (os.getenv("ORG_EMAIL_DOMAIN") or "").lower(), os.getenv("AGOL_USERNAME_SUFFIX", "")
    missing = [n for n, v in (("MQS_CLIENT_ID", client_id), ("MQS_SHARED_SECRET", secret), ("ORG_EMAIL_DOMAIN", domain)) if not v]
    if missing:
        sys.exit(f"Missing in .env: {', '.join(missing)}")

    with open(args.input_csv_path, encoding="utf-8-sig", newline="") as fh:
        members = [r for r in csv.DictReader(fh) if (r.get("Username") or "").strip()]
    if args.limit:
        members = members[: args.limit]
    netids = [netid_for(m, domain, suffix) for m in members]

    # Progress goes to a log file beside the output, flushed line by line, so it can be read while the
    # run is going (a PowerShell transcript only shows a program's output once the program ends), and
    # each result is written to the CSV as soon as it arrives, so a run that stops part-way keeps it.
    os.makedirs(os.path.dirname(os.path.abspath(args.output_csv_path)), exist_ok=True)
    log = open(os.path.splitext(args.output_csv_path)[0] + ".progress.log", "a", encoding="utf-8", buffering=1)

    def say(message):
        line = f"{time.strftime('%H:%M:%S')} {message}"
        print(line, flush=True)
        log.write(line + "\n")

    say(f"{len(members)} members; {sum(1 for n in netids if n)} with a NetID to look up.")
    run_date = date.today().isoformat()
    members_by_netid = {}
    for member, netid in zip(members, netids):
        members_by_netid.setdefault(netid, []).append(member)
    counts = {}

    def base_row(member, netid):
        row = {c: "" for c in OUTPUT_COLUMNS}
        row.update({"Username": member.get("Username", ""), "NetIDTried": netid, "updated_date": run_date})
        return row

    started = time.time()
    to_query = sorted(n for n in members_by_netid if n)
    with open(args.output_csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=OUTPUT_COLUMNS, quoting=csv.QUOTE_ALL)
        writer.writeheader()

        def write(row):
            writer.writerow(row)
            counts[row["MQS_Status"]] = counts.get(row["MQS_Status"], 0) + 1

        for member in members_by_netid.get("", []):
            row = base_row(member, "")
            row["MQS_Status"] = "no NetID"
            write(row)
        fh.flush()

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(lookup, n, client_id, secret): n for n in to_query}
            for i, future in enumerate(as_completed(futures), 1):
                netid = futures[future]
                status, entry = future.result()
                for member in members_by_netid[netid]:
                    row = base_row(member, netid)
                    row["HTTPStatus"] = status
                    if status == 200 and entry:
                        row["MQS_Status"] = "found"
                        for a in ATTRIBUTES:
                            row[a] = flatten(entry.get(a))
                        others = sorted(k for k in entry if k not in ATTRIBUTES)
                        row["OtherAttributes"] = ", ".join(others)   # names only: shows what this client can see
                    else:
                        row["MQS_Status"] = "not found" if status in (200, 404) else f"error {status}"
                    write(row)
                fh.flush()
                if i % 250 == 0 or i == len(to_query):
                    rate = i / max(time.time() - started, 1)
                    say(f"{i}/{len(to_query)} looked up; {counts}; about {(len(to_query) - i) / rate / 60:.0f} min left")

    say(f"Done in {(time.time() - started) / 60:.1f} min: {counts}. Wrote {args.output_csv_path}")


if __name__ == "__main__":
    main()
