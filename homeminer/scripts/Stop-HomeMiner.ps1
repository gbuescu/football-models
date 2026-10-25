<#
.SYNOPSIS
    Emergency stop: kill every process running from HomeMiner's tools folder.

.DESCRIPTION
    Works even if the HomeMiner interface or Python has hung. It stops only
    processes whose executable lives under tools\ in this project folder, so it
    cannot affect anything else on the PC.

.EXAMPLE
    .\scripts\Stop-HomeMiner.ps1
#>
[CmdletBinding()]
param()

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
$tools = Join-Path $root 'tools'
if (-not (Test-Path -LiteralPath $tools)) {
    Write-Host 'No tools folder: no HomeMiner-installed miner exists.'
    exit 0
}
$toolsFull = (Resolve-Path -LiteralPath $tools).Path.TrimEnd('\') + '\'

$stopped = 0
foreach ($proc in Get-Process) {
    $path = $null
    try { $path = $proc.Path } catch { $path = $null }
    if ($path -and $path.StartsWith($toolsFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        try {
            Stop-Process -Id $proc.Id -Force -ErrorAction Stop
            Write-Host "Stopped $($proc.ProcessName) (pid $($proc.Id))"
            $stopped++
        } catch {
            Write-Warning "Could not stop $($proc.ProcessName) (pid $($proc.Id)): $($_.Exception.Message)"
        }
    }
}
Write-Host "$stopped miner process(es) stopped."
