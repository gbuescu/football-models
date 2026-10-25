<#
.SYNOPSIS
    Run HomeMiner's automated tests. Needs Setup-HomeMiner.ps1 -Dev first.

.DESCRIPTION
    Tests use synthetic fixtures and a fake miner script; they never download or
    run real mining software and never contact the network. Benchmark-process
    tests that rely on a POSIX test double are skipped on Windows.
#>
[CmdletBinding()]
param()

$root = Split-Path -Parent $PSScriptRoot
$venvPy = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPy)) {
    Write-Host 'Run .\scripts\Setup-HomeMiner.ps1 -Dev first.'
    exit 1
}
Push-Location $root
try {
    & $venvPy -m pytest
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
