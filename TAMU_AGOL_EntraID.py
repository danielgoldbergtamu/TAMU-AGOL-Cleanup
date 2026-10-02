# File Name: TAMU_AGOL_EntraID.py
# Description: Looks up every ArcGIS Online member in Microsoft Entra ID (through Microsoft Graph)
#              and writes their affiliation status, department, supervisor and group memberships.
#              Python replacement for TAMU_AGOL_EntraID.ps1, with the same input and output.
# Date: 2026-10-02
#
# Why it replaces the PowerShell version: that script downloaded every account in the tenant
# (about 150,000, two to three hours) on each run in order to match about 12,500 members. This one
# asks Graph only about the members, 20 lookups per $batch request.
#
# Output columns are identical to the PowerShell version (reports/AGOL_EntraID_Status.csv), so
# TAMU_AGOL_Catalog.py and TAMU_AGOL_DeleteStatus.py read it unchanged:
#   Username, Email, Name, EmailsTried, EntraID_Status, ManagerEmail, ManagerDepartment,
#   UserDepartment, WorkingEmail, Groups
#
# Two deliberate differences. Groups are written in full: the PowerShell version cut them at 1,000
# characters, which in the 30 July output cut 743 rows and could drop the affiliation group that
# marks someone as current. And an alias (otherMails) lookup is made only for members who did not
# match on userPrincipalName or mail. The PowerShell version also appended alias matches to
# EmailsTried for members who had already matched; that one leaves status, department, supervisor and
# groups unaffected.
#
# Every run also adds the supervisors it sees to the HIST_Supervisors table, because Entra stops
# returning a supervisor once someone leaves (see CLAUDE.md). Turn that off with --no-history.
#
# Configuration (.env beside this file):
#   ENTRA_TENANT          tenant id or primary domain
#   ENTRA_CLIENT_ID       a public-client app allowed delegated User.Read.All
#   ORG_EMAIL_DOMAIN      domain that NetID-style usernames belong to
#   AGOL_USERNAME_SUFFIX  suffix AGOL adds to SSO usernames, e.g. _tamu
#   SQL_CONNECTION_STRING SQLAlchemy URL of the catalog database (for HIST_Supervisors)
#
# Usage (from the cloned ArcGIS Pro environment):
#   python TAMU_AGOL_EntraID.py --input_csv_path reports/OrganizationMembers_2026_10_01.csv
#   python TAMU_AGOL_EntraID.py --input_csv_path ... --limit 25 --output_csv_path reports/entra_test.csv

import argparse
import base64
import csv
import hashlib
import http.server
import os
import re
import secrets
import sys
import threading
import time
import urllib.parse
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import requests
from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = "https://graph.microsoft.com/User.Read.All offline_access"   # what Connect-MgGraph -Scopes User.Read.All asks for
BATCH_SIZE = 20                       # Graph's limit per $batch request
USER_FIELDS = "id,userPrincipalName,mail,department,displayName"
OUTPUT_COLUMNS = ["Username", "Email", "Name", "EmailsTried", "EntraID_Status", "ManagerEmail",
                  "ManagerDepartment", "UserDepartment", "WorkingEmail", "Groups"]


# Sign-in: authorization code with PKCE and a localhost redirect, the same browser flow
# Connect-MgGraph uses. Tokens are kept in memory only.
###########################################################################################

class GraphAuth:
    def __init__(self, tenant, client_id):
        self.authority = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0"
        self.client_id = client_id
        self.access_token = None
        self.refresh_token = None
        self.expires_at = 0
        self.lock = threading.Lock()

    def sign_in(self):
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(16)
        received = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                received.update(urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query))
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"Signed in. You can close this tab and return to the lookup.")

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("localhost", 0), Handler)
        redirect_uri = f"http://localhost:{server.server_port}"
        url = f"{self.authority}/authorize?" + urllib.parse.urlencode({
            "client_id": self.client_id, "response_type": "code", "redirect_uri": redirect_uri,
            "scope": SCOPES, "state": state, "code_challenge": challenge,
            "code_challenge_method": "S256", "prompt": "select_account"})
        print("Opening a browser for Microsoft sign-in. If nothing opens, visit:\n" + url)
        webbrowser.open(url)
        server.timeout = 300
        server.handle_request()
        server.server_close()

        if received.get("state", [""])[0] != state or "code" not in received:
            raise RuntimeError(f"Sign-in failed: {received.get('error_description', received.get('error', ['no code returned']))[0]}")
        self._token({"grant_type": "authorization_code", "code": received["code"][0],
                     "redirect_uri": redirect_uri, "code_verifier": verifier})

    def _token(self, form):
        form = {**form, "client_id": self.client_id, "scope": SCOPES}
        response = requests.post(f"{self.authority}/token", data=form, timeout=60)
        if response.status_code != 200:
            raise RuntimeError(f"Token request failed: {response.json().get('error_description', response.text)[:300]}")
        body = response.json()
        self.access_token = body["access_token"]
        self.refresh_token = body.get("refresh_token", self.refresh_token)
        self.expires_at = time.time() + int(body.get("expires_in", 3600))

    def header(self):
        with self.lock:
            if time.time() > self.expires_at - 300:
                self._token({"grant_type": "refresh_token", "refresh_token": self.refresh_token})
            return {"Authorization": f"Bearer {self.access_token}"}


# Graph calls, 20 at a time, retrying throttled requests
###########################################################################################

def run_batch(auth, requests_by_id, attempts=6):
    """POST up to 20 sub-requests to $batch. Returns {id: (status, body)}; retries 429/5xx."""
    pending = dict(requests_by_id)
    results = {}
    for attempt in range(attempts):
        if not pending:
            break
        payload = {"requests": [{"id": rid, "method": "GET", "url": spec["url"], "headers": spec.get("headers", {})}
                                for rid, spec in pending.items()]}
        response = requests.post(f"{GRAPH}/$batch", json=payload, headers=auth.header(), timeout=120)
        if response.status_code in (429, 503, 504):
            time.sleep(int(response.headers.get("Retry-After", 2 ** attempt)))
            continue
        response.raise_for_status()
        wait = 0
        for sub in response.json()["responses"]:
            if sub["status"] in (429, 503, 504) and attempt < attempts - 1:
                wait = max(wait, int(sub.get("headers", {}).get("Retry-After", 2 ** attempt)))
                continue
            results[sub["id"]] = (sub["status"], sub.get("body") or {})
            pending.pop(sub["id"], None)
        if pending:
            time.sleep(wait or 2 ** attempt)
    for rid in pending:
        results[rid] = (429, {})
    return results


def run_all(auth, specs, workers, label):
    """Run {id: spec} through $batch in chunks of 20, several chunks at a time."""
    ids = list(specs)
    chunks = [ids[i:i + BATCH_SIZE] for i in range(0, len(ids), BATCH_SIZE)]
    results, done = {}, 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for chunk_result in pool.map(lambda c: run_batch(auth, {i: specs[i] for i in c}), chunks):
            results.update(chunk_result)
            done += 1
            if done % 25 == 0 or done == len(chunks):
                print(f"  {label}: {done}/{len(chunks)} batches")
    return results


def get_all_pages(auth, first_url, body):
    """Follow @odata.nextLink outside $batch, for the rare member of more than 999 groups."""
    values = list(body.get("value", []))
    link = body.get("@odata.nextLink")
    while link:
        response = requests.get(link, headers=auth.header(), timeout=120)
        response.raise_for_status()
        page = response.json()
        values += page.get("value", [])
        link = page.get("@odata.nextLink")
    return values


# Matching members to accounts, exactly as the PowerShell version did
###########################################################################################

def format_email(value, domain, suffix):
    """netid@example.edu from 'netid@example.edu_org' or 'netid' (Format-Email in the .ps1)."""
    value = (value or "").strip()
    if suffix and value.endswith(suffix):
        value = value[: -len(suffix)]
    if "@" in value:
        value = value.split("@")[0]
    return f"{value}@{domain}" if value else ""


def emails_to_try(row, domain, suffix):
    seen, out = set(), []
    for value in (row.get("Email"), format_email(row.get("Username"), domain, suffix),
                  format_email(row.get("Email"), domain, suffix)):
        value = (value or "").strip()
        if value and value.lower() not in seen:
            seen.add(value.lower())
            out.append(value)
    return out


def odata_list(values):
    return ",".join("'" + v.replace("'", "''") + "'" for v in values)


def users_url(filter_text):
    return f"/users?$select={USER_FIELDS}&$filter=" + urllib.parse.quote(filter_text, safe="(),'=$:/")


def pick_match(candidates, users):
    """First candidate email that equals a user's userPrincipalName or mail wins."""
    for email in candidates:
        key = email.lower()
        for user in users:
            if key in ((user.get("userPrincipalName") or "").lower(), (user.get("mail") or "").lower()):
                return user, email
    return None, ""


# Supervisor history (CLAUDE.md: capture supervisors every run, never overwrite with a blank)
###########################################################################################

def record_supervisors(rows, run_date):
    from sqlalchemy import create_engine, text

    url = (os.getenv("SQL_CONNECTION_STRING") or "").strip().strip('"').strip("'")
    if not url:
        print("SQL_CONNECTION_STRING not set; supervisor history NOT updated.")
        return
    if "://" in url:
        engine = create_engine(url)
    else:  # a raw ODBC connection string, as the other scripts also accept
        engine = create_engine("mssql+pyodbc:///?odbc_connect=" + urllib.parse.quote_plus(url))
    merge = text("""
        MERGE dbo.HIST_Supervisors AS t
        USING (SELECT :user AS UserEmail, :mgr AS ManagerEmail) AS s
           ON t.UserEmail = s.UserEmail AND t.ManagerEmail = s.ManagerEmail
        WHEN MATCHED THEN UPDATE SET
             LastSeen = CASE WHEN t.LastSeen IS NULL OR t.LastSeen < :seen THEN :seen ELSE t.LastSeen END,
             FirstSeen = CASE WHEN t.FirstSeen IS NULL OR t.FirstSeen > :seen THEN :seen ELSE t.FirstSeen END,
             ManagerDepartment = COALESCE(NULLIF(:mdept, ''), t.ManagerDepartment),
             UserDepartment = COALESCE(NULLIF(:udept, ''), t.UserDepartment),
             Sightings = t.Sightings + 1,
             Sources = CASE WHEN t.Sources LIKE '%TAMU_AGOL_EntraID.py%' THEN t.Sources ELSE t.Sources + ';TAMU_AGOL_EntraID.py' END
        WHEN NOT MATCHED THEN INSERT
             (UserEmail, ManagerEmail, ManagerDepartment, UserDepartment, FirstSeen, LastSeen, Sightings, Sources)
             VALUES (:user, :mgr, NULLIF(:mdept, ''), NULLIF(:udept, ''), :seen, :seen, 1, 'TAMU_AGOL_EntraID.py');""")
    suffix = os.getenv("AGOL_USERNAME_SUFFIX", "")
    count = 0
    with engine.begin() as conn:
        for row in rows:
            if not row["ManagerEmail"]:
                continue
            user = row["Username"].strip().lower()
            if suffix and user.endswith(suffix.lower()):
                user = user[: -len(suffix)]
            conn.execute(merge, {"user": user, "mgr": row["ManagerEmail"].strip().lower(),
                                 "mdept": row["ManagerDepartment"], "udept": row["UserDepartment"],
                                 "seen": run_date})
            count += 1
    print(f"Supervisor history: {count} supervisor sightings recorded in HIST_Supervisors.")


# Main
###########################################################################################

def main():
    load_dotenv(os.path.join(SCRIPT_DIR, ".env"), override=True)
    parser = argparse.ArgumentParser(description="Look up AGOL members in Entra ID.")
    parser.add_argument("--input_csv_path", required=True, help="AGOL member report (Username, Email, Name)")
    parser.add_argument("--output_csv_path", default=os.path.join(SCRIPT_DIR, "reports", "AGOL_EntraID_Status.csv"))
    parser.add_argument("--limit", type=int, default=0, help="only the first N members (for testing)")
    parser.add_argument("--workers", type=int, default=4, help="batches in flight at once")
    parser.add_argument("--no-history", action="store_true", help="do not update HIST_Supervisors")
    args = parser.parse_args()

    tenant, client_id = os.getenv("ENTRA_TENANT"), os.getenv("ENTRA_CLIENT_ID")
    domain, suffix = os.getenv("ORG_EMAIL_DOMAIN"), os.getenv("AGOL_USERNAME_SUFFIX", "")
    missing = [n for n, v in (("ENTRA_TENANT", tenant), ("ENTRA_CLIENT_ID", client_id), ("ORG_EMAIL_DOMAIN", domain)) if not v]
    if missing:
        sys.exit(f"Missing in .env: {', '.join(missing)}")

    with open(args.input_csv_path, encoding="utf-8-sig", newline="") as fh:
        members = [r for r in csv.DictReader(fh) if (r.get("Username") or "").strip()]
    if args.limit:
        members = members[: args.limit]
    print(f"{len(members)} members to look up.")

    auth = GraphAuth(tenant, client_id)
    auth.sign_in()
    started = time.time()

    # 1. Match on userPrincipalName or mail.
    tried = {i: emails_to_try(m, domain, suffix) for i, m in enumerate(members)}
    specs = {str(i): {"url": users_url(f"userPrincipalName in ({odata_list(e)}) or mail in ({odata_list(e)})")}
             for i, e in tried.items() if e}
    found = {}
    for rid, (status, body) in run_all(auth, specs, args.workers, "primary match").items():
        if status == 200:
            user, used = pick_match(tried[int(rid)], body.get("value", []))
            if user:
                found[int(rid)] = (user, used)

    # 2. Members still unmatched: look for their addresses among account aliases (otherMails).
    alias_specs = {}
    for i, emails in tried.items():
        if i not in found and emails:
            clause = " or ".join(f"otherMails/any(o:o eq {odata_list([e])})" for e in emails)
            alias_specs[str(i)] = {"url": users_url(clause) + "&$count=true", "headers": {"ConsistencyLevel": "eventual"}}
    for rid, (status, body) in run_all(auth, alias_specs, args.workers, "alias match").items():
        users = body.get("value", []) if status == 200 else []
        if users and users[0].get("userPrincipalName"):
            upn = users[0]["userPrincipalName"]
            tried[int(rid)].append(upn)
            found[int(rid)] = (users[0], upn)

    # 3. Supervisor and groups for every matched account.
    ids = {user["id"] for user, _ in found.values()}
    manager_specs = {f"m{uid}": {"url": f"/users/{uid}/manager?$select=mail,department"} for uid in ids}
    group_specs = {f"g{uid}": {"url": f"/users/{uid}/memberOf?$select=id&$top=999"} for uid in ids}
    extra = run_all(auth, {**manager_specs, **group_specs}, args.workers, "supervisors and groups")

    run_date = date.today()
    rows, errors = [], []
    for i, member in enumerate(members):
        emails = tried[i]
        row = {"Username": member.get("Username", ""), "Email": member.get("Email", ""), "Name": member.get("Name", ""),
               "EmailsTried": ", ".join(emails)[:500], "EntraID_Status": 0, "ManagerEmail": "",
               "ManagerDepartment": "", "UserDepartment": "", "WorkingEmail": "", "Groups": ""}
        if i in found:
            user, used = found[i]
            m_status, m_body = extra.get(f"m{user['id']}", (404, {}))
            g_status, g_body = extra.get(f"g{user['id']}", (404, {}))
            if m_status in (429,) or g_status in (429,):
                errors.append(row["Username"])
            groups = get_all_pages(auth, None, g_body) if g_status == 200 else []
            row.update({
                "EntraID_Status": 1,
                "UserDepartment": user.get("department") or "",
                "WorkingEmail": used,
                "ManagerEmail": (m_body.get("mail") or "") if m_status == 200 else "",
                "ManagerDepartment": (m_body.get("department") or "") if m_status == 200 else "",
                "Groups": ", ".join(g["id"] for g in groups),   # never cut: a dropped affiliation group reads as "departed"
            })
        rows.append(row)

    os.makedirs(os.path.dirname(os.path.abspath(args.output_csv_path)), exist_ok=True)
    with open(args.output_csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=OUTPUT_COLUMNS, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)
    error_path = os.path.join(os.path.dirname(os.path.abspath(args.output_csv_path)), "error_EntraID_users.txt")
    with open(error_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(errors))

    matched = sum(1 for r in rows if r["EntraID_Status"] == 1)
    with_mgr = sum(1 for r in rows if r["ManagerEmail"])
    print(f"Done in {(time.time() - started) / 60:.1f} min: {len(rows)} members, {matched} found in Entra, "
          f"{with_mgr} with a supervisor, {len(errors)} throttled. Wrote {args.output_csv_path}")

    if not args.no_history:
        record_supervisors(rows, run_date)


if __name__ == "__main__":
    main()
