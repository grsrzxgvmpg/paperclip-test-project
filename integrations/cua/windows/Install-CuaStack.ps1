<#
.SYNOPSIS
    Install a pinned, verified Cua Driver and (optionally) the Cua Perception extension on Windows.

.DESCRIPTION
    Hardened alternative to `irm https://cua.ai/driver/install.ps1 | iex`:

      * pins exact versions (Cua ships Driver releases almost daily; 0.28.3 was withdrawn),
      * turns telemetry off BEFORE the first cua-driver process runs, so no installation
        ID is created and no registration event is sent,
      * runs the official installer from the matching GitHub release tag instead of piping
        unreviewed code from a website,
      * verifies the Driver zip against the release's checksums.txt and then checks that the
        installed cua-driver.exe is byte-identical to the verified zip (the official
        installer does not check hashes),
      * verifies the Perception catalog and archive against SHA256SUMS, shows the signed
        `extension inspect` output (licenses included) and asks before installing,
      * optionally pins the daemon to `bounded` mode with a capability manifest.

    Run from an interactive desktop session (console or RDP), NOT over SSH: the autostart
    task and every window tool need Session 1+.

        powershell -NoProfile -ExecutionPolicy Bypass -File .\Install-CuaStack.ps1
        powershell -NoProfile -ExecutionPolicy Bypass -File .\Install-CuaStack.ps1 -PinBoundedMode

.NOTES
    Cua Perception bundles an AGPL-3.0-only model (OmniParser icon detector). Private use on
    your own machines is unrestricted; redistributing it, or letting other people use it
    over a network (for example through a hosted agent service), carries AGPL obligations.
#>
[CmdletBinding()]
param(
    [string]$DriverVersion = '0.30.3',
    [string]$PerceptionVersion = '0.2.1',
    [switch]$SkipDriver,
    [switch]$SkipPerception,
    [switch]$PinBoundedMode,
    [string]$ManifestPath = (Join-Path $env:USERPROFILE '.cua\cua-capabilities.yaml'),
    [string]$DownloadDir = (Join-Path $env:USERPROFILE '.cua\downloads'),
    [switch]$DisableUpdateCheck,
    [switch]$Yes
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
# Invoke-WebRequest renders a progress bar per chunk in Windows PowerShell 5.1, which makes
# the ~420 MB Perception download many times slower.
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$Repo = 'trycua/cua'
$Triple = 'x86_64-pc-windows-msvc'
$DriverHome = if ($env:CUA_DRIVER_RS_HOME) { $env:CUA_DRIVER_RS_HOME } else { Join-Path $env:USERPROFILE '.cua-driver' }
$DriverBinDir = Join-Path $env:LOCALAPPDATA 'Programs\Cua\cua-driver\bin'

function Write-Step([string]$Message) { Write-Host "==> $Message" -ForegroundColor Cyan }
function Write-Warn([string]$Message) { Write-Host "warning: $Message" -ForegroundColor Yellow }

function Confirm-Step([string]$Question) {
    if ($Yes) { return $true }
    $answer = Read-Host "$Question [y/N]"
    return $answer -match '^(y|yes)$'
}

function Save-Url([string]$Url, [string]$Path) {
    Write-Step "downloading $Url"
    Invoke-WebRequest -Uri $Url -OutFile $Path -UseBasicParsing
}

function Get-ExpectedHashes([string]$ChecksumFile) {
    # Accepts `sha256sum` output, including the Markdown-fenced checksums.txt Cua publishes.
    $map = @{}
    foreach ($line in Get-Content -LiteralPath $ChecksumFile) {
        if ($line -match '^\s*([0-9a-fA-F]{64})\s+\*?(\S+)\s*$') {
            $map[$Matches[2]] = $Matches[1].ToLowerInvariant()
        }
    }
    return $map
}

function Assert-FileHash([string]$Path, [hashtable]$Expected) {
    $name = Split-Path -Leaf $Path
    if (-not $Expected.ContainsKey($name)) { throw "no published checksum for $name" }
    $actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $Expected[$name]) {
        throw "SHA-256 mismatch for ${name}: expected $($Expected[$name]), got $actual. Delete it and retry; do not install."
    }
    Write-Step "verified SHA-256 of $name"
}

function Invoke-Cua([string[]]$CuaArgs) {
    & $script:CuaExe @CuaArgs
    if ($LASTEXITCODE -ne 0) { throw "cua-driver $($CuaArgs -join ' ') exited with $LASTEXITCODE" }
}

function Set-UserEnv([string]$Name, [string]$Value) {
    [Environment]::SetEnvironmentVariable($Name, $Value, 'User')
    Set-Item -Path "Env:$Name" -Value $Value
    Write-Step "set user environment variable $Name=$Value"
}

# ---------------------------------------------------------------- preflight
if ($env:OS -ne 'Windows_NT') { throw 'This script is for Windows. Use the Linux/macOS installer elsewhere.' }
$arch = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
if ($arch -ne 'AMD64') {
    Write-Warn "architecture ${arch}: Cua Perception ships only for Windows x64; skipping Perception."
    $SkipPerception = $true
}
$sessionId = (Get-Process -Id $PID).SessionId
if ($sessionId -eq 0) {
    throw 'Running in Session 0 (SSH or a service). Run this from the console or an RDP session so the autostart task and window tools work.'
}
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Warn 'this shell is elevated. Cua Driver does not need administrator rights; an elevated daemon can also drive elevated windows. Prefer a normal shell.'
}

# Privacy defaults must exist before any cua-driver process starts.
Set-UserEnv 'CUA_DRIVER_RS_TELEMETRY_ENABLED' 'false'
if ($DisableUpdateCheck) { Set-UserEnv 'CUA_DRIVER_RS_UPDATE_CHECK' 'false' }
New-Item -ItemType Directory -Force -Path $DownloadDir | Out-Null

# ---------------------------------------------------------------- Cua Driver
if (-not $SkipDriver) {
    $tag = "cua-driver-rs-v$DriverVersion"
    $driverDir = Join-Path $DownloadDir $tag
    New-Item -ItemType Directory -Force -Path $driverDir | Out-Null

    $zipName = "cua-driver-rs-$DriverVersion-windows-x86_64.zip"
    $zipPath = Join-Path $driverDir $zipName
    $sumsPath = Join-Path $driverDir 'checksums.txt'
    Save-Url "https://github.com/$Repo/releases/download/$tag/checksums.txt" $sumsPath
    Save-Url "https://github.com/$Repo/releases/download/$tag/$zipName" $zipPath
    Assert-FileHash $zipPath (Get-ExpectedHashes $sumsPath)

    # Official installer and its helper module from the same release tag. With the helper
    # beside it, the installer does not fetch code from cua.ai.
    $installerDir = Join-Path $driverDir 'installer'
    New-Item -ItemType Directory -Force -Path $installerDir | Out-Null
    foreach ($file in @('install.ps1', '_install-common.psm1')) {
        Save-Url "https://raw.githubusercontent.com/$Repo/$tag/libs/cua-driver/scripts/$file" (Join-Path $installerDir $file)
    }
    Write-Step "running the official installer pinned to $DriverVersion"
    # Child process of the same PowerShell host: the installer must not inherit this
    # script's StrictMode/ErrorActionPreference, and its `exit` must not end this script.
    $hostExe = (Get-Process -Id $PID).Path
    $env:CUA_DRIVER_RS_VERSION = $DriverVersion
    try {
        & $hostExe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $installerDir 'install.ps1')
        if ($LASTEXITCODE -ne 0) { throw "official installer exited with $LASTEXITCODE" }
    } finally {
        Remove-Item Env:CUA_DRIVER_RS_VERSION -ErrorAction SilentlyContinue
    }

    # The installer re-downloads the zip without hashing it; prove the installed binary is
    # the verified one.
    $installedExe = Join-Path $DriverHome "packages\releases\$DriverVersion-$Triple\cua-driver.exe"
    if (-not (Test-Path -LiteralPath $installedExe)) { throw "expected installed binary at $installedExe" }
    $expandDir = Join-Path $driverDir 'verified'
    if (Test-Path -LiteralPath $expandDir) { Remove-Item -LiteralPath $expandDir -Recurse -Force }
    Expand-Archive -LiteralPath $zipPath -DestinationPath $expandDir
    $verifiedExe = Get-ChildItem -LiteralPath $expandDir -Recurse -Filter 'cua-driver.exe' | Select-Object -First 1
    $a = (Get-FileHash -LiteralPath $installedExe -Algorithm SHA256).Hash
    $b = (Get-FileHash -LiteralPath $verifiedExe.FullName -Algorithm SHA256).Hash
    if ($a -ne $b) { throw "installed cua-driver.exe ($a) differs from the checksum-verified release ($b). Uninstall and investigate." }
    Write-Step 'installed cua-driver.exe matches the checksum-verified release'
}

$script:CuaExe = Join-Path $DriverBinDir 'cua-driver.exe'
if (-not (Test-Path -LiteralPath $script:CuaExe)) {
    $cmd = Get-Command cua-driver.exe -ErrorAction SilentlyContinue
    if (-not $cmd) { throw 'cua-driver.exe not found; run without -SkipDriver first.' }
    $script:CuaExe = $cmd.Source
}
$reported = (& $script:CuaExe --version) -join ' '
Write-Step "using $script:CuaExe ($reported)"
if (-not $SkipDriver -and $reported -notmatch [regex]::Escape($DriverVersion)) {
    throw "expected cua-driver $DriverVersion, got '$reported'"
}
Invoke-Cua @('telemetry', 'disable')
& $script:CuaExe autostart status
if ($LASTEXITCODE -ne 0) { Write-Warn 'autostart task not confirmed; run "cua-driver autostart enable" from this session.' }
& $script:CuaExe autostart kick | Out-Null
& $script:CuaExe doctor

# ---------------------------------------------------------------- Cua Perception
if (-not $SkipPerception) {
    $ptag = "cua-perception-v$PerceptionVersion"
    $pdir = Join-Path $DownloadDir $ptag
    New-Item -ItemType Directory -Force -Path $pdir | Out-Null
    $base = "cua-perception-$PerceptionVersion-$Triple"
    $catalog = Join-Path $pdir "$base.catalog.json"
    $archive = Join-Path $pdir "$base.tar.gz"   # must sit beside the catalog
    $psums = Join-Path $pdir 'SHA256SUMS'
    Save-Url "https://github.com/$Repo/releases/download/$ptag/SHA256SUMS" $psums
    Save-Url "https://github.com/$Repo/releases/download/$ptag/$base.catalog.json" $catalog
    if (-not (Test-Path -LiteralPath $archive)) {
        Save-Url "https://github.com/$Repo/releases/download/$ptag/$base.tar.gz" $archive
    }
    $expected = Get-ExpectedHashes $psums
    Assert-FileHash $catalog $expected
    Assert-FileHash $archive $expected

    Write-Step 'signed catalog inspection (no state is changed):'
    Invoke-Cua @('extension', 'inspect', 'cua-perception', '--catalog', $catalog)
    Write-Host ''
    Write-Host 'Review the Trust line (must be publisher-verified) and the licenses above.' -ForegroundColor Yellow
    Write-Host 'The OmniParser detector is AGPL-3.0-only: fine for private use; do not redistribute or expose it to other users over a network without meeting the AGPL.' -ForegroundColor Yellow
    if (Confirm-Step "Install cua-perception $PerceptionVersion?") {
        Invoke-Cua @('extension', 'install', 'cua-perception', '--catalog', $catalog)
        Invoke-Cua @('extension', 'status', 'cua-perception', '--self-test')
    } else {
        Write-Warn 'skipped Perception install; parse_visual_regions will return not_installed.'
    }
}

# ---------------------------------------------------------------- bounded mode
if ($PinBoundedMode) {
    if (-not (Test-Path -LiteralPath $ManifestPath)) {
        $template = Join-Path $PSScriptRoot 'cua-capabilities.yaml'
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $ManifestPath) | Out-Null
        Copy-Item -LiteralPath $template -Destination $ManifestPath
        Write-Warn "copied the template manifest to $ManifestPath. Edit its app and folder entries, then rerun with -SkipDriver -SkipPerception -PinBoundedMode."
        return
    }
    Set-UserEnv 'CUA_DRIVER_PERMISSION_MODE' 'bounded'
    Set-UserEnv 'CUA_DRIVER_CAPABILITY_MANIFEST_FILE' $ManifestPath
    Set-UserEnv 'CUA_DRIVER_CAPABILITY_MANIFEST_APPROVED' '1'
    & $script:CuaExe stop | Out-Null
    Invoke-Cua @('autostart', 'kick')
    Start-Sleep -Seconds 3
    # A bounded daemon with a missing or invalid manifest refuses to bind rather than
    # silently falling back to standard mode.
    Invoke-Cua @('status')
}

Write-Host ''
Write-Step 'done. Next:'
Write-Host '  * Claude Code (cloud Claude):  claude mcp add --transport stdio cua-driver -- cua-driver mcp'
Write-Host '  * OpenClaw:                    cua-driver mcp-config --client openclaw   (run the printed command)'
Write-Host '  * Local model via llama-server: integrations\cua\windows\start-claude-local-cua.cmd'
Write-Host '  * Smoke test:                  python integrations\cua\tools\perception_smoke.py --window-title "<title>"'
Write-Host '  Open a NEW terminal first so the updated PATH and environment variables apply.'
