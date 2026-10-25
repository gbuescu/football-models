<#
.SYNOPSIS
    Open the HomeMiner interface in your browser.

.DESCRIPTION
    Starts HomeMiner's local server on 127.0.0.1 (never on the network) and opens
    the interface. Nothing is benchmarked or mined until you press a button.
    Closing this window stops any miner HomeMiner started.

.EXAMPLE
    .\scripts\Start-HomeMiner.ps1
#>
[CmdletBinding()]
param(
    [int]$Port = 0,
    [switch]$NoBrowser
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$venvPy = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPy)) {
    Write-Host 'Run .\scripts\Setup-HomeMiner.ps1 first.'
    exit 1
}

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Warning 'This window is elevated. HomeMiner will open, but it will refuse to launch miners. Use a normal PowerShell window.'
}

$uiArgs = @('-m', 'homeminer', 'ui', '--port', $Port)
if ($NoBrowser) { $uiArgs += '--no-browser' }
Push-Location $root
try {
    & $venvPy @uiArgs
} finally {
    Pop-Location
}
