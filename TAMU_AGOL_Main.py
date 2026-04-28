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

from TAMU_AGOL_Catalog import main as catalog_main
from TAMU_AGOL_DeleteStatus import main as delete_status_main
from TAMU_AGOL_DeleteUsers import main as delete_users_main
from TAMU_AGOL_UserQuotas import main as user_quotas_main

def main():
    print("Starting ArcGIS Online cleanup process...")
    
    print("\nStep 1: Accessing AGOL Catalog and Gathering User Data...")
    catalog_main()
    
    print("\nStep 2: Checking Account Deletion Status and Emailing Users...")
    delete_status_main()
    
    print("\nStep 3: Deleting Accounts Marked for Deletion...")
    delete_users_main()
    
    print("\nStep 4: Calculating User Storage Quotas and Emailing Over-Quota Users...")
    user_quotas_main()
    
    print("\nArcGIS Online cleanup process completed.")

if __name__ == "__main__":
    main()