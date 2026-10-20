"""One-time fill of HIST_PersonNames: first and last names of supervisors and members, from Entra ID.

TAMU_AGOL_EntraID.py records names on every run from 20 October 2026. This fills the gap before that:
every supervisor in HIST_Supervisors, and every member the latest lookup found (AGOL_EntraID_Status), whose
email has no name on record yet. People no longer in Entra cannot be named this way and are left out.

Reads Entra through Microsoft Graph (the same browser sign-in as the main lookup) and writes only to
HIST_PersonNames. Nothing in ArcGIS Online is read or changed.

Usage (from the repo folder, in the cloned ArcGIS Pro environment):
    python tools/name_lookup.py [--limit N]
"""
import argparse
import os
import sys
import time
from datetime import date

from dotenv import load_dotenv
from sqlalchemy import text

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from TAMU_AGOL_EntraID import GraphAuth, member_key, odata_list, record_names, run_all, sql_engine, users_url  # noqa: E402

PER_REQUEST = 7   # Graph allows at most 15 values in a whole filter, and each email is asked twice (UPN and mail)


def main():
    load_dotenv(os.path.join(REPO, ".env"), override=True)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=0, help="only the first N emails (for testing)")
    parser.add_argument("--workers", type=int, default=4, help="batches in flight at once")
    args = parser.parse_args()
    suffix = os.getenv("AGOL_USERNAME_SUFFIX", "")

    engine = sql_engine()
    if engine is None:
        sys.exit("SQL_CONNECTION_STRING is not set in .env")
    with engine.connect() as c:
        named = set()
        if c.execute(text("SELECT OBJECT_ID('dbo.HIST_PersonNames')")).scalar() is not None:
            named = {r[0] for r in c.execute(text("SELECT DISTINCT Email FROM dbo.HIST_PersonNames"))}
        # key (what the name is recorded under) -> email to look up
        wanted = {r[0].strip().lower(): r[0].strip().lower()
                  for r in c.execute(text("SELECT DISTINCT ManagerEmail FROM dbo.HIST_Supervisors WHERE ISNULL(ManagerEmail, '') <> ''"))}
        supervisors = len(wanted)
        for username, working in c.execute(text("SELECT Username, WorkingEmail FROM dbo.AGOL_EntraID_Status "
                                                "WHERE EntraID_Status = 1 AND ISNULL(WorkingEmail, '') <> ''")):
            wanted.setdefault(member_key(username, suffix), working.strip().lower())
    todo = sorted((k, e) for k, e in wanted.items() if k not in named)
    if args.limit:
        todo = todo[: args.limit]
    print(f"{supervisors} supervisors and {len(wanted) - supervisors} more members on record; {len(todo)} without a name to look up.")
    if not todo:
        return

    auth = GraphAuth(os.getenv("ENTRA_TENANT"), os.getenv("ENTRA_CLIENT_ID"))
    auth.sign_in()
    started = time.time()
    chunks = [todo[i:i + PER_REQUEST] for i in range(0, len(todo), PER_REQUEST)]
    specs = {str(i): {"url": users_url(f"userPrincipalName in ({odata_list([e for _, e in ch])}) or mail in ({odata_list([e for _, e in ch])})")}
             for i, ch in enumerate(chunks)}
    people, failed = [], 0
    for rid, (status, body) in run_all(auth, specs, args.workers, "names").items():
        if status != 200:
            if not failed:
                print(f"first failed request: HTTP {status}: {str(body)[:300]}")
            failed += len(chunks[int(rid)])
            continue
        by_email = {}
        for user in body.get("value", []):
            for field in ("userPrincipalName", "mail"):
                if user.get(field):
                    by_email[user[field].lower()] = user
        for key, email in chunks[int(rid)]:
            user = by_email.get(email)
            if user:
                people.append((key, user.get("givenName"), user.get("surname"), user.get("displayName")))
    print(f"Done in {(time.time() - started) / 60:.1f} min: {len(people)} of {len(todo)} named, "
          f"{len(todo) - len(people) - failed} not in Entra, {failed} in failed requests.")
    record_names(people, date.today(), "tools/name_lookup.py")


if __name__ == "__main__":
    main()
