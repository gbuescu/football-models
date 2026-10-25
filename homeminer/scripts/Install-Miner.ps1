<#
.SYNOPSIS
    Download and verify an official miner release. Asks for your approval first.

.DESCRIPTION
    Downloads the configured release (XMRig 6.26.0 or xmrig-cuda 6.22.1 by default)
    from the project's official GitHub release page, refuses it unless its SHA-256
    matches the release's SHA256SUMS, checks the GPG signature if Gpg4win is
    installed and the publisher's key is in your keyring, and extracts it into
    tools\. A manifest of file hashes is written and re-checked before every launch.

    It does not add Windows Defender exclusions, services, scheduled tasks, startup
    entries or registry keys. Defender may quarantine the files; that is your call
    to make (see docs\SECURITY.md).

.EXAMPLE
    .\scripts\Install-Miner.ps1 -Miner xmrig
#>
[CmdletBinding()]
param(
    [ValidateSet('xmrig', 'xmrig-cuda')]
    [string]$Miner = 'xmrig'
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$venvPy = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPy)) {
    Write-Host 'Run .\scripts\Setup-HomeMiner.ps1 first.'
    exit 1
}

# Show exactly what will be downloaded, without downloading anything yet.
& $venvPy -m homeminer install-miner $Miner
Write-Host ''
Write-Host 'This installs third-party cryptocurrency mining software on this PC.'
$answer = Read-Host "Type APPROVE (in capitals) to download and verify $Miner, or anything else to cancel"
if ($answer -cne 'APPROVE') {
    Write-Host 'Cancelled. Nothing was downloaded.'
    exit 1
}
& $venvPy -m homeminer install-miner $Miner --approve
exit $LASTEXITCODE
