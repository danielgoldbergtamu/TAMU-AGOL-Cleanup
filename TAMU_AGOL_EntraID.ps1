# File Name: Identify_Historical_AGOL_Users.p1
# Author: Dalton Peterson
# Date: 03/26/2026
# Requires: Powershell 7.0+
# Description: This file takes an input report from ArcGIS Online (AGOL) and produces a csv of all members with a
#              fields that identify the user's current affiliation w/ TAMU and their manager (if exists)
# Expected Runtime: 2-3 Hours (depending on number of users and Graph API response times; generally around 150,000 users)


# collect csv
param(
    [string]$input_csv_path
)

# Ensure relative paths resolve from the script directory.
$scriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
Set-Location -Path $scriptDir


# Import library for accessing TAMU Identification System - uses OS authentication
Import-Module Microsoft.Graph.Users
Connect-MgGraph -Scopes "User.Read.All" -NoWelcome


# Cache all EntraID users to minimize Graph calls and create index tables
Write-Host "Caching EntraID users..."
$allEntraUsers = Get-MgUser -All -Property Id, UserPrincipalName, Mail, OtherMails, Department, DisplayName
Write-Host "Cached $($allEntraUsers.Count) EntraID users"


# Helper functions for user resolution and data retrieval
#######################################################################################################

# Function to create a formatted email based on a username (i.e. username@tamu.edu)
function Format-Email {
    param([string]$username)
    $username = $username -replace '_tamu$', ''
    if ($username -match '@') { $username = $username.Split('@')[0] }
    return "$username@tamu.edu"
}

# Function to resolve an EntraID user based on a list of formatted emails, using cached indexes for efficient lookup
function Resolve-EntraUserFromEmails {
    param([array]$emailsToTry)

    $emailsToTry = @($emailsToTry) | Where-Object { $_ -and $_.Trim() -ne "" } | Select-Object -Unique

    foreach ($emailToTry in $emailsToTry) {
        $lookupKey = $emailToTry.Trim().ToLowerInvariant()

        if ($ResolvedEmailCache.ContainsKey($lookupKey)) {
            $cachedUser = $ResolvedEmailCache[$lookupKey]
            if ($cachedUser) {
                return [PSCustomObject]@{
                    Found     = 1
                    User      = $cachedUser
                    EmailUsed = $emailToTry
                }
            }
            continue
        }

        if ($UserByPrincipal.ContainsKey($lookupKey)) {
            $resolvedUser = $UserByPrincipal[$lookupKey]
            $ResolvedEmailCache[$lookupKey] = $resolvedUser
            return [PSCustomObject]@{
                Found     = 1
                User      = $resolvedUser
                EmailUsed = $emailToTry
            }
        }

        $ResolvedEmailCache[$lookupKey] = $null
    }

    return [PSCustomObject]@{
        Found     = 0
        User      = $null
        EmailUsed = ""
    }
}

function Find-AlternateEmail {
    param([array]$emailsToTry)

    foreach ($emailToCheck in $emailsToTry) {
        if (-not $emailToCheck -or $emailToCheck.Trim() -eq "") { continue }
        $otherKey = $emailToCheck.Trim().ToLowerInvariant()

        if ($UserByOtherMail.ContainsKey($otherKey)) {
            $alternateUser = $UserByOtherMail[$otherKey]
            if ($alternateUser -and $alternateUser.UserPrincipalName) {
                return $alternateUser.UserPrincipalName
            }
        }
    }

    return ""
}

# Function to find a user's manager information (email and department) based on their EntraID user ID
function Find-ManagerInfo {
    param([string]$userId)

    if (-not $userId) {
        return [PSCustomObject]@{
            ManagerEmail      = ""
            ManagerDepartment = ""
        }
    }

    if ($ManagerByUserId.ContainsKey($userId)) {
        return $ManagerByUserId[$userId]
    }

    $result = [PSCustomObject]@{
        ManagerEmail      = ""
        ManagerDepartment = ""
    }

    try {
        $managerRef = Get-MgUserManager -UserId $userId -ErrorAction Stop
        if ($managerRef -and $managerRef.Id) {
            if ($UserById.ContainsKey($managerRef.Id)) {
                $managerUser = $UserById[$managerRef.Id]
            }
            else {
                $managerUser = Get-MgUser -UserId $managerRef.Id -Property Id, UserPrincipalName, Mail, Department, DisplayName -ErrorAction Stop
                if ($managerUser -and $managerUser.Id) {
                    $UserById[$managerUser.Id] = $managerUser

                    if ($managerUser.UserPrincipalName) {
                        $UserByPrincipal[$managerUser.UserPrincipalName.Trim().ToLowerInvariant()] = $managerUser
                    }

                    if ($managerUser.Mail) {
                        $UserByPrincipal[$managerUser.Mail.Trim().ToLowerInvariant()] = $managerUser
                    }
                }
            }

            $managerEmail = if ($managerUser.Mail) { $managerUser.Mail } else { "" }
            $managerDepartment = if ($managerUser.Department) { $managerUser.Department } else { "" }
            $result = [PSCustomObject]@{
                ManagerEmail      = $managerEmail
                ManagerDepartment = $managerDepartment
            }
        }
    }
    catch {
        $result = [PSCustomObject]@{
            ManagerEmail      = ""
            ManagerDepartment = ""
        }
    }

    $ManagerByUserId[$userId] = $result
    return $result
}

# Function to find a user's group memberships based on their EntraID user ID
function Find-GroupMemberships {
    param([string]$userId)

    if (-not $userId) { return @() }

    if ($GroupsByUserId.ContainsKey($userId)) {
        return $GroupsByUserId[$userId]
    }

    $groupMemberships = @()
    try {
        $groupMemberships = Get-MgUserMemberOf -UserId $userId -Property Id -All -ErrorAction Stop | Select-Object -ExpandProperty Id
    }
    catch {
        $groupMemberships = @()
    }

    $GroupsByUserId[$userId] = @($groupMemberships)
    return @($groupMemberships)
}

# Primary lookup indexes
$UserById = @{}
$UserByPrincipal = @{}
$UserByOtherMail = @{}

$numUsersIndexed = 0


# Indexing look to create lookup tables for efficient user resolution during main processing loop
#########################################################################################################


foreach ($entraUser in $allEntraUsers) {
    if ($entraUser.Id) {
        $UserById[$entraUser.Id] = $entraUser
    }

    if ($entraUser.UserPrincipalName) {
        $upnKey = $entraUser.UserPrincipalName.Trim().ToLowerInvariant()
        if (-not $UserByPrincipal.ContainsKey($upnKey)) {
            $UserByPrincipal[$upnKey] = $entraUser
        }
    }

    if ($entraUser.Mail) {
        $mailKey = $entraUser.Mail.Trim().ToLowerInvariant()
        if (-not $UserByPrincipal.ContainsKey($mailKey)) {
            $UserByPrincipal[$mailKey] = $entraUser
        }
    }

    foreach ($otherMail in @($entraUser.OtherMails)) {
        if (-not $otherMail) { continue }
        $otherKey = $otherMail.Trim().ToLowerInvariant()
        if (-not $UserByOtherMail.ContainsKey($otherKey)) {
            $UserByOtherMail[$otherKey] = $entraUser
        }
    }
    $numUsersIndexed++
    Write-Progress -Activity "Indexing EntraID Users" -Status "Indexed $numUsersIndexed users..." -PercentComplete (($numUsersIndexed / $allEntraUsers.Count) * 100)
} 

# Runtime caches to avoid repeated Graph calls
$ResolvedEmailCache = @{}
$ManagerByUserId = @{}
$GroupsByUserId = @{}



# Main loop for handling emails and building EntraID Status table
#########################################################################################################

# Initialize tables before loop
$UserTable = @()
$ErrorUsers = @()


$UserTable = $input_csv | ForEach-Object {

    # Initialize variables for analysis
    $username = $_.Username
    $email = $_.Email
    $name = $_.Name

    $formattedEmail1 = Format-Email -username $username
    $formattedEmail2 = Format-Email -username $email
    $emailstotry = @($email, $formattedEmail1, $formattedEmail2) | Where-Object { $_ -and $_.Trim() -ne "" } | Select-Object -Unique

    $altEmail = Find-AlternateEmail -emailsToTry $emailstotry
    if ($altEmail -and $altEmail.Trim() -ne "" -and $emailstotry -notcontains $altEmail) {
        $emailstotry += $altEmail
    }

    $entraidLookup = Resolve-EntraUserFromEmails -emailsToTry $emailstotry

    if ($entraidLookup.Found -eq 1) {
        Write-Host "Resolved EntraID for $username using email $($entraidLookup.EmailUsed)"
        $resolvedUser = $entraidLookup.User
        $entraid_status = $entraidLookup.Found
        $userDepartment = if ($resolvedUser.Department) { $resolvedUser.Department } else { "" }
        $workingEmail = $entraidLookup.EmailUsed

        $managerInfo = Find-ManagerInfo -userId $resolvedUser.Id
        $managerEmail = $managerInfo.ManagerEmail
        $managerDepartment = $managerInfo.ManagerDepartment

        $groupMemberships = Find-GroupMemberships -userId $resolvedUser.Id
        $groupMemberships = $groupMemberships -join ", "
        $groupMemberships = $groupMemberships.Substring(0, [math]::Min(1000, $groupMemberships.Length))
    }
    else {
        Write-Host "Could not resolve EntraID for $username with tried emails: $($emailstotry -join ", ")"
        $entraid_status = 0
        $managerEmail = ""
        $managerDepartment = ""
        $userDepartment = ""
        $groupMemberships = ""
        $workingEmail = ""
    }

    try {
        $emailsTried = ($emailstotry -join ", ")
        $emailsTried = $emailsTried.Substring(0, [math]::Min(500, $emailsTried.Length))
        [PSCustomObject]@{
            Username          = $username
            Email             = $email
            Name              = $name
            EmailsTried       = $emailsTried
            EntraID_Status    = $entraid_status
            ManagerEmail      = $managerEmail
            ManagerDepartment = $managerDepartment
            UserDepartment    = $userDepartment
            WorkingEmail      = $workingEmail
            Groups            = $groupMemberships
            ErrorUser         = ""
        }
    }
    catch {
        [PSCustomObject]@{
            Username          = $username
            Email             = $email
            Name              = $name
            EmailsTried       = ""
            EntraID_Status    = 0
            ManagerEmail      = ""
            ManagerDepartment = ""
            UserDepartment    = ""
            WorkingEmail      = ""
            Groups            = ""
            ErrorUser         = $username
        }
    }
}


# Exporting results from above loop
###########################################################################################################


$ErrorUsers = $UserTable | Where-Object { $_.ErrorUser } | Select-Object -ExpandProperty ErrorUser
$UserTable = $UserTable | Select-Object -ExcludeProperty ErrorUser

# Export results
$UserTable | Export-Csv -Path ".\reports\AGOL_EntraID_Status.csv" -NoTypeInformation
$ErrorUsers | Out-File -FilePath ".\reports\error_EntraID_users.txt"

Write-Host "Processing complete:"
Write-Host "Users Processed: $($UserTable.Count)"