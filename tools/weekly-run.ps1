<#
.SYNOPSIS
    The weekly read-only AGOL cleanup run: lookups, catalog, delete-status DRY RUN, removal tables, snapshot.

.DESCRIPTION
    1. Downloads the newest member report.
    2. Entra lookup (TAMU_AGOL_EntraID.py) in this window: it opens a browser for Microsoft sign-in, and
       waits up to 15 minutes. At the same time, the directory lookup (TAMU_AGOL_MQS.py, if configured) and
       the group inventory run in the background.
    3. Catalog with those three results, then a delete-status DRY RUN, then tools/removal_summary.py.
    4. If the private repository is present, the dashboard data, the snapshot (SNAP_* tables) and the page.

    It never runs a real delete-status update, and nothing in it deletes or changes anything in ArcGIS
    Online. If the Entra sign-in is not completed, it stops before the database is touched.
    Everything goes into reports/runs/<date-time>-weekly/, with a log you can read while it runs (run.log).

    Needs: ArcGIS Pro signed in as an administrator; the Pro clone Python (or -Python); .env configured.
    Until an unattended Entra sign-in exists, someone has to be logged on to complete the sign-in.

    powershell -ExecutionPolicy Bypass -File tools\weekly-run.ps1
#>
param(
    [string]$Repo = (Split-Path -Parent $PSScriptRoot),
    [string]$Python = "$env:LOCALAPPDATA\ESRI\conda\envs\arcgispro-py3-clone-1\python.exe"
)
# Continue, not Stop: Python warnings on stderr must not end the run in PowerShell 5.1; each step's exit code is checked instead.
$ErrorActionPreference = 'Continue'
$run = Join-Path $Repo ("reports\runs\{0}-weekly" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Force $run | Out-Null
$log = Join-Path $run 'run.log'
function Say($m) { $line = "{0} {1}" -f (Get-Date -Format HH:mm:ss), $m; Add-Content -Path $log -Value $line; Write-Host $line }
function Step($name, [string[]]$arguments, $outfile) {
    & $Python -u @arguments *>> (Join-Path $run $outfile)
    if ($LASTEXITCODE -ne 0) { Say "STOPPED: $name exited with $LASTEXITCODE (see $outfile). Nothing after it ran."; exit 1 }
}
Set-Location $Repo
$host.UI.RawUI.WindowTitle = 'AGOL weekly run: a Microsoft sign-in tab will open'
Say "Weekly AGOL run started. Folder: $run"
Say "A Microsoft sign-in tab will open in your browser in a moment. Please complete it within 15 minutes."

Step 'member report download' @('tools\fetch_member_report.py', '--out', "$run\members.csv") 'fetch.log'

$mqs = $null
if ((Select-String -Path (Join-Path $Repo '.env') -Pattern '^\s*MQS_CLIENT_ID\s*=\s*\S' -Quiet)) {
    $mqs = Start-Process $Python -ArgumentList @('-u', 'TAMU_AGOL_MQS.py', '--input_csv_path', "`"$run\members.csv`"", '--output_csv_path', "`"$run\MQS_Directory_Status.csv`"") `
        -WorkingDirectory $Repo -PassThru -WindowStyle Minimized -RedirectStandardError "$run\mqs_stderr.txt"
    Say "directory lookup started in the background (progress: MQS_Directory_Status.progress.log)"
}
$groups = Start-Process $Python -ArgumentList @('-u', 'tools\group_inventory.py', '--out', "`"$run\groups`"") `
    -WorkingDirectory $Repo -PassThru -WindowStyle Minimized -RedirectStandardError "$run\groups_stderr.txt"
Say "group inventory started in the background (progress: groups\progress.log)"

Say "Entra lookup: waiting for your sign-in"
Step 'Entra lookup' @('TAMU_AGOL_EntraID.py', '--input_csv_path', "$run\members.csv", '--output_csv_path', "$run\AGOL_EntraID_Status.csv") 'entra.log'
Say "Entra lookup done. Waiting for the background lookups."
foreach ($p in @($mqs, $groups) | Where-Object { $_ }) { $p.WaitForExit() }
if (-not (Test-Path "$run\groups\groups.csv")) { Say "STOPPED: group inventory produced no groups.csv"; exit 1 }

$catalog = @('TAMU_AGOL_Catalog.py', '--entraid-csv', "$run\AGOL_EntraID_Status.csv")
if (Test-Path "$run\MQS_Directory_Status.csv") { $catalog += @('--mqs-csv', "$run\MQS_Directory_Status.csv") }
Say "catalog: loading this week's reports and lookups"
Step 'catalog' $catalog 'catalog.log'
Say "delete-status DRY RUN (writes nothing to DeleteStatus)"
Step 'delete-status dry run' @('TAMU_AGOL_DeleteStatus.py', '--dry-run') 'deletestatus_dryrun.log'
$dry = Get-ChildItem (Join-Path $Repo 'reports\runs') -Directory -Filter '*-deletestatus-dryrun' | Sort-Object Name | Select-Object -Last 1
Step 'removal tables' @('tools\removal_summary.py', '--changes', "$($dry.FullName)\DeleteStatus_changes.csv", '--out', "$run\removal") 'removal.log'

$builder = Join-Path $Repo 'private\dashboard\build_dashboard_data.py'
if (Test-Path $builder) {
    Say "dashboard data and snapshot"
    Step 'dashboard data and snapshot' @($builder, '--groups', "$run\groups", '--out', (Join-Path $Repo 'private\data'), '--changes', "$($dry.FullName)\DeleteStatus_changes.csv") 'dashboard.log'
    $json = Get-ChildItem (Join-Path $Repo 'private\data') -Filter 'dashboard_data_*.json' | Sort-Object Name | Select-Object -Last 1
    Step 'dashboard page' @((Join-Path $Repo 'private\dashboard\build_dashboard.py'), $json.FullName) 'dashboard.log'
}
Say "Weekly run finished. Removal summary: $run\removal\removal_summary.txt"
Get-Content "$run\removal\removal_summary.txt" | Select-Object -Last 4 | ForEach-Object { Say $_ }
Say "WEEKLY RUN EXITED WITH CODE 0"
