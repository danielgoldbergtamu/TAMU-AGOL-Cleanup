"""The daily job's first step: signs in as the service account and stops the run unless it can only view.

The job never changes or deletes anything in ArcGIS Online, and its account's role allows only viewing. This makes
the second of those a checked fact on every run: if the account has any privilege beyond the view-only ones
(agol_db.READ_ONLY_PRIVILEGES), this exits with an error, the run stops before any other step, and it shows as
failed. Each later step checks again when it signs in (agol_db.agol_gis).

    python tools/check_read_only.py
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(REPO, ".env"), override=False)
import agol_db  # noqa: E402


def main():
    if not (os.environ.get("AGOL_USERNAME") and os.environ.get("AGOL_PASSWORD")):
        raise SystemExit("REFUSED: AGOL_USERNAME and AGOL_PASSWORD are not set. The daily job signs in only as the "
                         "read-only service account, never with another sign-in.")
    gis = agol_db.agol_gis()   # refuses unless the account can only view
    print("privileges: " + ", ".join(sorted(gis.users.me.privileges)))
    print("read-only check passed")


if __name__ == "__main__":
    main()
