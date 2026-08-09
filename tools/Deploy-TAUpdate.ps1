[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet("ControlCenter", "ContentFactory")]
    [string]$Profile,

    [Parameter(Mandatory)]
    [string[]]$Files,

    [string]$ServerHost = "155.212.216.35",

    [string]$ServerUser = "root",

    [switch]$DryRun,

    [switch]$PrepareOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$DeployProfiles = @{
    ControlCenter = @{
        LocalRoot  = "C:\Desktop\travel-advantage-ai-control-center"
        RemoteRoot = "/opt/travel_advantage_ai_control_center"
        BackupRoot = "/opt/backups/travel_advantage_ai_control_center"
        Service    = "travel-advantage-ai-control-center.service"
    }
    ContentFactory = @{
        LocalRoot  = "C:\Desktop\travel_content_factory"
        RemoteRoot = "/opt/travel_content_factory"
        BackupRoot = "/opt/backups/travel_content_factory"
        Service    = "travel-content-factory.service"
    }
}

function Normalize-RelativePath {
    param(
        [Parameter(Mandatory)]
        [string]$Path
    )

    $value = ($Path.Trim() -replace "\\", "/")

    if ([string]::IsNullOrWhiteSpace($value)) {
        throw "Invalid file path: empty value."
    }

    if (
        [System.IO.Path]::IsPathRooted($Path) -or
        $value.StartsWith("/") -or
        $value -match '(^|/)\.\.(/|$)' -or
        $value.EndsWith(".new") -or
        $value -match '\.bak-'
    ) {
        throw "Invalid relative file path: $Path"
    }

    # Runtime state never ships with the code. data/ holds the journal and the
    # working source registry, .env holds secrets: overwriting either from the
    # repository would destroy what the owner changed in production.
    # Checked before the leading dot is trimmed, otherwise ".env" would already
    # have turned into "env". ".env.example" stays deployable - it is a template.
    if (
        $value -eq ".env" -or
        $value -eq "./.env" -or
        $value -like "*data/*" -or
        $value -like "*.sqlite3" -or
        $value -like "*.db"
    ) {
        throw "Refusing to deploy runtime state: $Path. Runtime files live only on the server."
    }

    # Strip a leading "./" only. Trimming every leading dot would rename any
    # dotfile: ".env.example" used to arrive here as "env.example" and then
    # failed with a confusing "local file not found".
    while ($value.StartsWith("./")) {
        $value = $value.Substring(2)
    }

    $value = $value.TrimStart([char[]]"/")

    if ([string]::IsNullOrWhiteSpace($value)) {
        throw "Invalid file path after normalization: $Path"
    }

    return $value
}

function Resolve-Executable {
    param(
        [Parameter(Mandatory)]
        [string]$Name,

        [Parameter(Mandatory)]
        [string[]]$FallbackPaths
    )

    # PATH stays the primary source: if ssh/scp is there, it wins.
    $command = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1

    if ($command -and $command.Source) {
        return $command.Source
    }

    # Git for Windows ships ssh/scp in its own usr\bin and does not always add
    # it to PATH. Without this fallback the deploy would fail before it even
    # reaches the VPS.
    foreach ($candidate in $FallbackPaths) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return $candidate
        }
    }

    throw "$Name was not found in PATH and no known fallback location exists. Install Git for Windows or OpenSSH."
}

function Invoke-External {
    param(
        [Parameter(Mandatory)]
        [string]$Program,

        [Parameter(Mandatory)]
        [string[]]$Arguments,

        [Parameter(Mandatory)]
        [string]$FailureMessage
    )

    & $Program @Arguments

    if ($LASTEXITCODE -ne 0) {
        throw $FailureMessage
    }
}

$ssh = $null
$scp = $null

$config = $DeployProfiles[$Profile]
$relativeFiles = @()

foreach ($file in $Files) {
    $relative = Normalize-RelativePath -Path $file

    if ($relativeFiles -notcontains $relative) {
        $relativeFiles += $relative
    }
}

if ($relativeFiles.Count -eq 0) {
    throw "No files were provided."
}

Write-Host ""
Write-Host "=== Deploy profile ==="
Write-Host "Profile:  $Profile"
Write-Host "Service:  $($config.Service)"
Write-Host "Files:"

foreach ($relative in $relativeFiles) {
    Write-Host " - $relative"

    $localRelative = $relative -replace "/", "\"
    $localFile = Join-Path $config.LocalRoot $localRelative

    if (-not (Test-Path -LiteralPath $localFile -PathType Leaf)) {
        throw "Local file not found: $localFile"
    }
}

if ($DryRun) {
    Write-Host ""
    Write-Host "DRY RUN: local files are valid. VPS was not contacted."
    return
}

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$connection = "${ServerUser}@${ServerHost}"
$remoteScriptPath = "/tmp/deploy-ta-$Profile-$timestamp.sh"
$localScriptPath = Join-Path $env:TEMP "deploy-ta-$Profile-$timestamp.sh"

$fileLines = (@(
    $relativeFiles | ForEach-Object { "  '$_'" }
) -join "`n")

$remoteTemplate = @'
#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT='__REMOTE_ROOT__'
BACKUP_ROOT='__BACKUP_ROOT__'
SERVICE='__SERVICE__'

FILES=(
__FILES__
)

STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP_DIR="$BACKUP_ROOT/$STAMP"

# Files that already existed in production BEFORE this deploy.
# Rollback needs this list to tell an update from a fresh install: the first
# is restored from backup, the second is removed. A missing backup copy could
# not serve as that marker - a new file has none by definition, and skipping
# it silently would leave half of a failed deploy on the server.
MANIFEST="$BACKUP_DIR/.preexisting"
CHANGED=0

on_error() {
    STATUS=$?

    # Drop the trap: an error inside the rollback would otherwise re-enter it.
    trap - ERR

    if [ "$CHANGED" -eq 1 ] && [ -d "$BACKUP_DIR" ] && [ -f "$MANIFEST" ]; then
        echo "rollback=started"

        for RELATIVE in "${FILES[@]}"; do
            TARGET="$PROJECT/$RELATIVE"
            BACKUP_FILE="$BACKUP_DIR/$RELATIVE"

            if grep -Fxq -- "$RELATIVE" "$MANIFEST"; then
                if [ -f "$BACKUP_FILE" ]; then
                    cp -a "$BACKUP_FILE" "$TARGET"
                    echo "rollback_restored=$RELATIVE"
                fi
            else
                rm -f "$TARGET"
                echo "rollback_removed=$RELATIVE"
            fi
        done

        systemctl restart "$SERVICE" || true
        echo "rollback=finished"
    fi

    exit "$STATUS"
}

trap on_error ERR

for RELATIVE in "${FILES[@]}"; do
    TARGET="$PROJECT/$RELATIVE"
    STAGED="$TARGET.new"

    test -s "$STAGED"

    case "$RELATIVE" in
        *.py)
            python3 -m py_compile "$STAGED"
            ;;
    esac
done

install -d -m 700 "$BACKUP_DIR"
: > "$MANIFEST"
chmod 600 "$MANIFEST"

for RELATIVE in "${FILES[@]}"; do
    TARGET="$PROJECT/$RELATIVE"
    BACKUP_FILE="$BACKUP_DIR/$RELATIVE"

    if [ -e "$TARGET" ]; then
        # Updating an existing file. A directory or a symlink where a regular
        # file is expected is an error, not a reason to carry on.
        test -f "$TARGET"
        install -d -m 700 "$(dirname "$BACKUP_FILE")"
        cp -a "$TARGET" "$BACKUP_FILE"
        printf '%s\n' "$RELATIVE" >> "$MANIFEST"
        echo "backup=$RELATIVE"
    else
        # New file: there is nothing to back up, and that is not an error.
        echo "new=$RELATIVE"
    fi
done

# Raised BEFORE the first replacement: a partially moved set must still
# trigger the rollback.
CHANGED=1

for RELATIVE in "${FILES[@]}"; do
    TARGET="$PROJECT/$RELATIVE"
    STAGED="$TARGET.new"

    # A new file may live in a directory that does not exist on the server yet.
    mkdir -p "$(dirname "$TARGET")"
    mv "$STAGED" "$TARGET"
done

systemctl restart "$SERVICE"
sleep 3
systemctl is-active --quiet "$SERVICE"

CHANGED=0
trap - ERR

echo "service=$(systemctl is-active "$SERVICE")"
echo "backup_dir=$BACKUP_DIR"
echo "updated_files:"

for RELATIVE in "${FILES[@]}"; do
    ls -lh "$PROJECT/$RELATIVE"
done
'@

$remoteScript = $remoteTemplate
$remoteScript = $remoteScript.Replace("__REMOTE_ROOT__", $config.RemoteRoot)
$remoteScript = $remoteScript.Replace("__BACKUP_ROOT__", $config.BackupRoot)
$remoteScript = $remoteScript.Replace("__SERVICE__", $config.Service)
$remoteScript = $remoteScript.Replace("__FILES__", $fileLines)
$remoteScript = $remoteScript -replace "`r`n", "`n"

$ascii = [System.Text.Encoding]::ASCII
[System.IO.File]::WriteAllText($localScriptPath, $remoteScript, $ascii)

if ($PrepareOnly) {
    Write-Host ""
    Write-Host "PREPARE ONLY: Bash script was created. VPS was not contacted."
    Write-Host ""
    Write-Host "=== Generated Bash script ==="
    Get-Content -LiteralPath $localScriptPath

    Remove-Item -LiteralPath $localScriptPath -Force -ErrorAction SilentlyContinue
    return
}

$gitBin = Join-Path $env:ProgramFiles "Git\usr\bin"

$ssh = Resolve-Executable -Name "ssh.exe" -FallbackPaths @(
    (Join-Path $gitBin "ssh.exe"),
    "C:\Program Files\Git\usr\bin\ssh.exe"
)
$scp = Resolve-Executable -Name "scp.exe" -FallbackPaths @(
    (Join-Path $gitBin "scp.exe"),
    "C:\Program Files\Git\usr\bin\scp.exe"
)

Write-Host ""
Write-Host "ssh: $ssh"
Write-Host "scp: $scp"

# Directories that may not exist on the server yet: scp does not create them
# and would fail on the first file of a new package.
$remoteDirectories = @()

foreach ($relative in $relativeFiles) {
    $parent = ([System.IO.Path]::GetDirectoryName($relative) -replace "\\", "/")

    if (-not [string]::IsNullOrWhiteSpace($parent)) {
        $remoteDirectory = "$($config.RemoteRoot)/$parent"

        if ($remoteDirectories -notcontains $remoteDirectory) {
            $remoteDirectories += $remoteDirectory
        }
    }
}

$remoteScriptUploaded = $false

try {
    if ($remoteDirectories.Count -gt 0) {
        Write-Host ""
        Write-Host "=== Ensure remote directories ==="

        # mkdir -p overwrites nothing: existing directories and the files
        # inside them are left untouched.
        $quotedDirectories = ($remoteDirectories | ForEach-Object { "'$_'" }) -join " "

        foreach ($remoteDirectory in $remoteDirectories) {
            Write-Host " - $remoteDirectory"
        }

        Invoke-External `
            -Program $ssh `
            -Arguments @(
                $connection,
                "mkdir -p $quotedDirectories"
            ) `
            -FailureMessage "Could not create remote directories. Nothing was uploaded."
    }

    Write-Host ""
    Write-Host "=== Upload staged files ==="

    foreach ($relative in $relativeFiles) {
        $localRelative = $relative -replace "/", "\"
        $localFile = Join-Path $config.LocalRoot $localRelative
        $remoteNewFile = "$($config.RemoteRoot)/$relative.new"

        Invoke-External `
            -Program $scp `
            -Arguments @(
                $localFile,
                "${connection}:$remoteNewFile"
            ) `
            -FailureMessage "Upload failed for $relative.new. Production file was not replaced."
    }

    Invoke-External `
        -Program $scp `
        -Arguments @(
            $localScriptPath,
            "${connection}:$remoteScriptPath"
        ) `
        -FailureMessage "Upload failed for the temporary deployment script."

    $remoteScriptUploaded = $true

    Write-Host ""
    Write-Host "=== Server validation and deploy ==="

    Invoke-External `
        -Program $ssh `
        -Arguments @(
            $connection,
            "bash -n '$remoteScriptPath' && bash '$remoteScriptPath'"
        ) `
        -FailureMessage "Deployment failed. If production replacement had begun, the script attempted rollback."
}
finally {
    if (Test-Path -LiteralPath $localScriptPath) {
        Remove-Item -LiteralPath $localScriptPath -Force -ErrorAction SilentlyContinue
    }

    if ($remoteScriptUploaded) {
        & $ssh $connection "rm -f '$remoteScriptPath'" 2>$null
    }
}
