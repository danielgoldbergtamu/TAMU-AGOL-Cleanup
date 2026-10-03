# File Name: TAMU_AGOL_Main.py
# Description: This script runs all of the necessary functions for Texas A&M's ArcGIS Online cleanup project
#               by calling the scripts in the following order:
#               1. TAMU_AGOL_Catalog.py 
#               2. TAMU_AGOL_EntraID.ps1 (called by TAMU_AGOL_Catalog.py)
#               3. TAMU_AGOL_DeleteStatus.py
#               4. TAMU_AGOL_DeleteUsers.py
#               5. TAMU_AGOL_UserQuotas.py
# Author: Dalton Peterson
# Date: 2026-04-21

import os

from TAMU_AGOL_Catalog import main as catalog_main, engine
from TAMU_AGOL_Runs import Run
from TAMU_AGOL_DeleteStatus import main as delete_status_main
from TAMU_AGOL_DeleteUsers import main as delete_users_main
from TAMU_AGOL_UserQuotas import main as user_quotas_main

def main():
    # One pipeline run; each step below records its own run under it and writes into a sub-folder of
    # reports/runs/<pipeline run id>/ (TAMU_AGOL_Runs reads AGOL_RUN_ID).
    print("Starting ArcGIS Online cleanup process...")
    with Run(engine, 'pipeline') as pipeline:
        os.environ['AGOL_RUN_ID'] = pipeline.run_id
        try:
            run_steps()
        finally:
            os.environ.pop('AGOL_RUN_ID', None)


def run_steps():
    
    print("\nStep 1: Accessing AGOL Catalog and Gathering User Data...")
    catalog_main()
    
    print("\nStep 2: Recalculating delete status (no email is sent)...")
    delete_status_main()

    print("\nStep 3: Listing accounts marked for deletion (nothing is deleted)...")
    delete_users_main()

    print("\nStep 4: Listing users over the storage quota (no email is sent)...")
    user_quotas_main()
    
    print("\nArcGIS Online cleanup process completed.")

if __name__ == "__main__":
    main()