<#
.SYNOPSIS
    Stores the TAMU directory (MQS) client id and shared secret in this repository's .env file.

.DESCRIPTION
    Prompts for MQS_CLIENT_ID and MQS_SHARED_SECRET (the secret is not shown as you type), then writes
    them into .env, replacing earlier values and leaving every other line as it was. .env is ignored by
    git and must never be committed. The values come from https://mqs.tamu.edu/rest/clients/.

    Windows PowerShell 5.1. Run from anywhere:
        powershell -ExecutionPolicy Bypass -File tools\set-mqs-credentials.ps1
#>
param(
    [string]$EnvPath = (Join-Path (Split-Path -Parent $PSScriptRoot) '.env')
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path $EnvPath)) {
    Write-Host "No .env at $EnvPath. Copy .env.example to .env first, then run this again."
    exit 1
}

$clientId = (Read-Host 'MQS client identifier (from mqs.tamu.edu/rest/clients/)').Trim()
$secure   = Read-Host 'MQS shared secret (not shown)' -AsSecureString
if (-not $clientId -or $secure.Length -eq 0) {
    Write-Host 'Nothing entered. .env was not changed.'
    exit 1
}

# The secret exists as plain text only long enough to be written into .env.
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
    $secret = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr).Trim()
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
}

$values = [ordered]@{ 'MQS_CLIENT_ID' = $clientId; 'MQS_SHARED_SECRET' = $secret }
$lines  = New-Object System.Collections.Generic.List[string]
$seen   = @{}
foreach ($line in [IO.File]::ReadAllLines($EnvPath)) {
    $key = ($line -split '=', 2)[0].Trim()
    if ($values.Contains($key)) {
        if (-not $seen.ContainsKey($key)) { $lines.Add("$key = $($values[$key])"); $seen[$key] = $true }
    } else {
        $lines.Add($line)
    }
}
foreach ($key in $values.Keys) {
    if (-not $seen.ContainsKey($key)) { $lines.Add("$key = $($values[$key])") }
}

# UTF-8 without a byte-order mark: a BOM would hide the first setting in the file from python-dotenv.
[IO.File]::WriteAllLines($EnvPath, $lines, (New-Object System.Text.UTF8Encoding($false)))
$secret = $null

Write-Host "Saved to $EnvPath :"
Write-Host "  MQS_CLIENT_ID      set ($($clientId.Length) characters)"
Write-Host "  MQS_SHARED_SECRET  set ($($secure.Length) characters, not shown)"
