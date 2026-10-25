<#
.SYNOPSIS
    Create HomeMiner's Python environment. Installs no mining software.

.DESCRIPTION
    1. Checks that Python 3.11 or newer is installed (it does not install Python).
    2. Creates .venv in the project folder.
    3. Installs HomeMiner and its one runtime dependency (psutil) into .venv;
       with -Dev also pytest.
    4. Copies config\homeminer.sample.toml to config\homeminer.toml if missing.

    Supports -WhatIf to show what would happen without doing it.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\Setup-HomeMiner.ps1 -Dev
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [switch]$Dev
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

$pyExe = $null
$pyArgs = @()
if (Get-Command py -ErrorAction SilentlyContinue) {
    $pyExe = 'py'; $pyArgs = @('-3')
} else {
    $candidate = Get-Command python -ErrorAction SilentlyContinue
    if ($candidate -and $candidate.Source -notlike '*WindowsApps*') { $pyExe = $candidate.Source }
}
if (-not $pyExe) {
    Write-Host 'Python 3.11 or newer was not found.'
    Write-Host 'Install it yourself from https://www.python.org/downloads/windows/ (tick "Add python.exe to PATH"),'
    Write-Host 'or run:  winget install --id Python.Python.3.12 -e'
    Write-Host 'Then run this script again.'
    exit 1
}

$version = (& $pyExe @pyArgs -c "import sys; print('%d.%d' % sys.version_info[:2])").Trim()
$parts = $version.Split('.')
if ([int]$parts[0] -lt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -lt 11)) {
    Write-Host "Python $version found, but HomeMiner needs 3.11 or newer."
    exit 1
}
Write-Host "Using Python $version"

$venv = Join-Path $root '.venv'
if (-not (Test-Path -LiteralPath $venv)) {
    if ($PSCmdlet.ShouldProcess($venv, 'Create Python virtual environment')) {
        & $pyExe @pyArgs -m venv $venv
    }
}
$venvPy = Join-Path $venv 'Scripts\python.exe'

$target = if ($Dev) { "$root[dev]" } else { $root }
if ($PSCmdlet.ShouldProcess($venv, "pip install HomeMiner (psutil$(if ($Dev) { ', pytest' }))")) {
    & $venvPy -m pip install --disable-pip-version-check --upgrade pip
    & $venvPy -m pip install --disable-pip-version-check -e $target
}

$config = Join-Path $root 'config\homeminer.toml'
if (-not (Test-Path -LiteralPath $config)) {
    if ($PSCmdlet.ShouldProcess($config, 'Copy the sample configuration')) {
        Copy-Item -LiteralPath (Join-Path $root 'config\homeminer.sample.toml') -Destination $config
    }
}

Write-Host ''
Write-Host 'Done. No mining software was installed and nothing is running.'
Write-Host 'Next:'
Write-Host '  .\scripts\Get-HomeMinerDiagnostics.ps1      read-only hardware report'
Write-Host '  .\scripts\Start-HomeMiner.ps1               open the interface'
Write-Host '  .\scripts\Install-Miner.ps1 -Miner xmrig    only after you decide to approve it'
