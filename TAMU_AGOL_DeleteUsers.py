# File Name: TAMU_AGOL_DeleteUsers.py
# Decription: Lists the accounts at DeleteStatus 2, into this run's folder (UsersToDelete.csv). It removes nothing:
#             removing accounts and content is a separate, manual step, only after the maintainer has reviewed a
#             specific set and approved it in writing, and only after a verified backup (CLAUDE.md, "Deleting").
# Author: Dalton Peterson
# Date: 2026-04-02 (DuckDB since 2026-10-20)

import os

import pandas as pd
from dotenv import load_dotenv

from agol_db import connect, q
from TAMU_AGOL_Runs import Run

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, ".env"), override=True)
DELETE_STATUS_TABLE_NAME = os.getenv("DELETE_STATUS_TABLE_NAME") or "DeleteStatus"


def main():
    with connect() as con, Run(con, 'deleteusers') as run:
        print('Querying database for users tagged for deletion...')
        users = [r[0] for r in con.execute(f"SELECT Username FROM {q(DELETE_STATUS_TABLE_NAME)} WHERE DeleteStatus = 2 ORDER BY Username").fetchall()]
        users_to_delete_csv = run.path('UsersToDelete.csv')   # this run's folder: never overwrites an earlier list
        pd.DataFrame(users, columns=['Username']).to_csv(users_to_delete_csv, index=False)
        run.note(users_to_delete=len(users))
        print(f'Found {len(users)} accounts at status 2. Saved the list to {users_to_delete_csv} (nothing is removed).')


if __name__ == "__main__":
    main()
