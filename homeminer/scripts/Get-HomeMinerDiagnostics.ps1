<#
.SYNOPSIS
    Read-only HomeMiner hardware and software report. Changes nothing.

.DESCRIPTION
    Reports the CPU, memory, NVIDIA GPU (via nvidia-smi), Windows version,
    prerequisites and known limitations for HomeMiner. It only reads: it does not
    install software, change drivers, edit the registry, change power settings or
    touch Windows Defender.

    Serial numbers, UUIDs, MAC addresses, the computer name and your user name are
    not collected. Works in Windows PowerShell 5.1 and PowerShell 7, and does not
    need Python.

.PARAMETER Json
    Print JSON instead of text.

.PARAMETER OutFile
    Also write the report to this file.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\Get-HomeMinerDiagnostics.ps1
#>
[CmdletBinding()]
param(
    [switch]$Json,
    [string]$OutFile
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot

function Get-Cim([string]$Class, [string[]]$Properties) {
    try {
        return @(Get-CimInstance -ClassName $Class -ErrorAction Stop | Select-Object -Property $Properties)
    } catch {
        return @()
    }
}

function Get-RegistryValues([string]$Path) {
    try { return Get-ItemProperty -Path $Path -ErrorAction Stop } catch { return $null }
}

function Get-Prop($Object, [string]$Name) {
    if ($null -eq $Object) { return $null }
    $p = $Object.PSObject.Properties[$Name]
    if ($p) { return $p.Value }
    return $null
}

function Hide-Identity([string]$Text) {
    if (-not $Text) { return $Text }
    foreach ($id in @($env:USERNAME, $env:COMPUTERNAME)) {
        if ($id -and $id.Length -ge 3) { $Text = $Text -replace [regex]::Escape($id), '<redacted>' }
    }
    return $Text
}

# --- Operating system ---------------------------------------------------------
$nt = Get-RegistryValues 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
$osCim = Get-Cim 'Win32_OperatingSystem' @('Caption', 'BuildNumber', 'OSArchitecture', 'TotalVisibleMemorySize', 'FreePhysicalMemory') | Select-Object -First 1
$build = 0
[void][int]::TryParse([string](Get-Prop $nt 'CurrentBuild'), [ref]$build)
$family = if ($build -ge 22000) { 'Windows 11' } elseif ($build -ge 10240) { 'Windows 10' } else { 'Windows (older or unknown)' }

# --- CPU ----------------------------------------------------------------------
$cpu = Get-Cim 'Win32_Processor' @('Name', 'NumberOfCores', 'NumberOfLogicalProcessors', 'L2CacheSize', 'L3CacheSize', 'MaxClockSpeed') | Select-Object -First 1
$cpuKey = Get-RegistryValues 'HKLM:\HARDWARE\DESCRIPTION\System\CentralProcessor\0'
$microcode = $null
$rev = Get-Prop $cpuKey 'Update Revision'
if ($rev -is [byte[]] -and $rev.Length -ge 8) {
    $value = [BitConverter]::ToUInt32($rev, 4)
    if ($value -eq 0) { $value = [BitConverter]::ToUInt32($rev, 0) }
    if ($value -ne 0) { $microcode = '0x{0:X}' -f $value }
}
$cpuName = [string](Get-Prop $cpu 'Name')
$raptorDesktop = $cpuName -match '\bi[3579]-1[34]\d{3}(K|KF|F|KS|T)?\b'

# --- Memory -------------------------------------------------------------------
$dimms = Get-Cim 'Win32_PhysicalMemory' @('Capacity', 'ConfiguredClockSpeed', 'SMBIOSMemoryType')
$memTypes = @{ 24 = 'DDR3'; 26 = 'DDR4'; 34 = 'DDR5' }
$memType = ($dimms | ForEach-Object { $t = [int](Get-Prop $_ 'SMBIOSMemoryType'); if ($memTypes.ContainsKey($t)) { $memTypes[$t] } else { 'unknown' } } | Sort-Object -Unique) -join '/'
$totalGb = if ($osCim) { [math]::Round([double](Get-Prop $osCim 'TotalVisibleMemorySize') / 1MB, 1) } else { $null }
$freeGb = if ($osCim) { [math]::Round([double](Get-Prop $osCim 'FreePhysicalMemory') / 1MB, 1) } else { $null }

# --- Board --------------------------------------------------------------------
$board = Get-Cim 'Win32_BaseBoard' @('Manufacturer', 'Product') | Select-Object -First 1
$bios = Get-Cim 'Win32_BIOS' @('SMBIOSBIOSVersion') | Select-Object -First 1

# --- NVIDIA -------------------------------------------------------------------
$smi = $null
$cmd = Get-Command nvidia-smi -ErrorAction SilentlyContinue
if ($cmd) { $smi = $cmd.Source }
if (-not $smi) {
    foreach ($candidate in @("$env:SystemRoot\System32\nvidia-smi.exe", "$env:ProgramFiles\NVIDIA Corporation\NVSMI\nvidia-smi.exe")) {
        if (Test-Path -LiteralPath $candidate) { $smi = $candidate; break }
    }
}
$gpus = @()
$driverCuda = $null
if ($smi) {
    $fields = 'name,memory.total,memory.used,driver_version,temperature.gpu,utilization.gpu,power.draw,power.limit'
    $rows = @(& $smi "--query-gpu=$fields" '--format=csv,noheader,nounits' 2>$null)
    $ccRows = @(& $smi '--query-gpu=compute_cap' '--format=csv,noheader' 2>$null)
    if ($LASTEXITCODE -ne 0) { $ccRows = @() }
    $header = (@(& $smi 2>$null) -join "`n")
    if ($header -match 'CUDA Version:\s*([\d.]+)') { $driverCuda = $Matches[1] }
    for ($i = 0; $i -lt $rows.Count; $i++) {
        $p = $rows[$i] -split ',\s*'
        if ($p.Count -lt 8) { continue }
        $clean = { param($v) if ($v -match '^\[?(N/A|Not Supported)\]?$') { $null } else { $v } }
        $cc = if ($i -lt $ccRows.Count -and $ccRows[$i] -match '^\d+\.\d+$') { $ccRows[$i].Trim() } else { $null }
        $ccSource = 'nvidia-smi'
        if (-not $cc -and $p[0] -match 'GTX 9[5-8]0') { $cc = '5.2'; $ccSource = 'lookup' }
        $gpus += [ordered]@{
            index = $i; name = $p[0]; vram_mb = (& $clean $p[1]); vram_used_mb = (& $clean $p[2])
            driver = $p[3]; temperature_c = (& $clean $p[4]); utilization_pct = (& $clean $p[5])
            power_draw_w = (& $clean $p[6]); power_limit_w = (& $clean $p[7])
            compute_capability = $cc; compute_capability_source = $ccSource
        }
    }
}
$videoNames = @(Get-Cim 'Win32_VideoController' @('Name') | ForEach-Object { Get-Prop $_ 'Name' })

# --- Prerequisites ------------------------------------------------------------
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
$elevated = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
$lockPages = [bool](whoami /priv 2>$null | Select-String -SimpleMatch 'SeLockMemoryPrivilege')
$scheme = (powercfg /getactivescheme 2>$null) -join ' '
$powerPlan = if ($scheme -match '\(([^)]+)\)\s*$') { $Matches[1] } else { $null }
$defender = $null
try { $defender = (Get-MpComputerStatus -ErrorAction Stop).RealTimeProtectionEnabled } catch { $defender = $null }

$python = 'not found'
$pyLauncher = Get-Command py -ErrorAction SilentlyContinue
$pyCmd = Get-Command python -ErrorAction SilentlyContinue
if ($pyLauncher) {
    $python = (& py -3 --version 2>&1) -join ' '
} elseif ($pyCmd -and $pyCmd.Source -notlike '*WindowsApps*') {
    $python = (& python --version 2>&1) -join ' '
} elseif ($pyCmd) {
    $python = 'only the Microsoft Store alias (no real Python install)'
}
$gpg = [bool](Get-Command gpg -ErrorAction SilentlyContinue)
if (-not $gpg) { $gpg = (Test-Path "${env:ProgramFiles(x86)}\GnuPG\bin\gpg.exe") -or (Test-Path "$env:ProgramFiles\GnuPG\bin\gpg.exe") }

$miners = [ordered]@{}
foreach ($dir in @('xmrig-6.26.0', 'xmrig-cuda-6.22.1')) {
    $manifest = Join-Path (Join-Path $root 'tools') (Join-Path $dir 'homeminer-manifest.json')
    $miners[$dir] = if (Test-Path -LiteralPath $manifest) { 'installed by HomeMiner (hashes are re-checked before each launch)' } else { 'not installed (installing needs your approval)' }
}

# --- Limitations --------------------------------------------------------------
$limits = New-Object System.Collections.Generic.List[string]
if ($elevated) { $limits.Add('This window is elevated (Administrator). HomeMiner refuses to launch miners from an elevated session.') }
if ($raptorDesktop) { $limits.Add("13th/14th Gen Intel desktop CPU: Intel recommends the latest BIOS (microcode 0x12F) with Intel Default Settings because of 'Vmin shift' instability. Loaded microcode: $microcode.") }
if ($null -ne $freeGb -and $freeGb -lt 3) { $limits.Add("Only $freeGb GB RAM free. RandomX needs about 2.3 GB.") }
if (-not $lockPages) { $limits.Add("'Lock pages in memory' is not granted: huge pages unavailable, RandomX runs slower. HomeMiner never grants it.") }
if (-not $smi) { $limits.Add('nvidia-smi not found: GPU temperature, power and utilisation cannot be read.') }
foreach ($g in $gpus) {
    if ($g.compute_capability -and $g.compute_capability -like '5.*') {
        $limits.Add("$($g.name) is Maxwell: NVIDIA's R580 driver branch is its last, and CUDA 13 no longer targets it. Use CUDA 12.x (or older) miner builds.")
        $limits.Add("$($g.name) is not supported by NiceHash QuickMiner/Excavator (GTX 1000 series and newer).")
    }
    if ($g.vram_mb -and [double]$g.vram_mb -le 4160) { $limits.Add("$($g.name): $($g.vram_mb) MB VRAM. DAG algorithms (KawPow, Etchash, Octopus) no longer fit.") }
    if ($g.name -match 'GTX 970') { $limits.Add('GTX 970: only the first 3.5 GB of VRAM run at full speed.') }
    if (-not $g.power_draw_w) { $limits.Add("$($g.name) does not report power draw: GPU power will be estimated, and labelled so.") }
}
if ($defender) { $limits.Add('Windows Defender real-time protection is on and will likely quarantine miners. HomeMiner will not disable it or add exclusions.') }
if (-not $gpg) { $limits.Add('gpg not found: downloads are checked against SHA256SUMS but the GPG signature cannot be checked (install Gpg4win to add that).') }
$limits.Add('Windows has no built-in CPU temperature/power reading. HomeMiner uses LibreHardwareMonitor if you run it, otherwise estimates and says so.')

# --- Output -------------------------------------------------------------------
$report = [ordered]@{
    generated_at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    read_only = $true
    os = [ordered]@{ name = $family; edition = (Get-Prop $nt 'EditionID'); display_version = (Get-Prop $nt 'DisplayVersion'); build = "$build.$(Get-Prop $nt 'UBR')"; architecture = (Get-Prop $osCim 'OSArchitecture') }
    cpu = [ordered]@{ name = $cpuName; cores = (Get-Prop $cpu 'NumberOfCores'); logical_processors = (Get-Prop $cpu 'NumberOfLogicalProcessors'); l3_cache_mb = [math]::Round([double](Get-Prop $cpu 'L3CacheSize') / 1024, 1); base_clock_mhz = (Get-Prop $cpu 'MaxClockSpeed'); microcode_revision = $microcode }
    memory = [ordered]@{ total_gb = $totalGb; available_gb = $freeGb; type = $memType; modules = $dimms.Count }
    board = [ordered]@{ manufacturer = (Get-Prop $board 'Manufacturer'); product = (Get-Prop $board 'Product'); bios_version = (Get-Prop $bios 'SMBIOSBIOSVersion') }
    gpus = $gpus
    nvidia = [ordered]@{ nvidia_smi = [bool]$smi; driver_cuda_version = $driverCuda; video_adapters = $videoNames }
    prerequisites = [ordered]@{ powershell = $PSVersionTable.PSVersion.ToString(); python = $python; elevated = $elevated; lock_pages_in_memory = $lockPages; power_plan = $powerPlan; defender_realtime = $defender; gpg = $gpg; miners = $miners }
    limitations = $limits
}

if ($Json) {
    $text = Hide-Identity ($report | ConvertTo-Json -Depth 6)
} else {
    $sb = New-Object System.Text.StringBuilder
    [void]$sb.AppendLine("HomeMiner diagnostics ($($report.generated_at)) - read-only, nothing was changed")
    foreach ($section in @('os', 'cpu', 'memory', 'board', 'nvidia', 'prerequisites')) {
        [void]$sb.AppendLine('')
        [void]$sb.AppendLine("[$section]")
        foreach ($k in $report[$section].Keys) {
            $v = $report[$section][$k]
            if ($v -is [System.Collections.IDictionary]) { $v = ($v.GetEnumerator() | ForEach-Object { "$($_.Key): $($_.Value)" }) -join '; ' }
            elseif ($v -is [array]) { $v = $v -join ', ' }
            [void]$sb.AppendLine(('  {0,-24} {1}' -f $k, $v))
        }
    }
    foreach ($g in $gpus) {
        [void]$sb.AppendLine('')
        [void]$sb.AppendLine("[gpu $($g.index)]")
        foreach ($k in $g.Keys) { [void]$sb.AppendLine(('  {0,-24} {1}' -f $k, $g[$k])) }
    }
    if (-not $gpus.Count) { [void]$sb.AppendLine(''); [void]$sb.AppendLine('[gpu] no NVIDIA GPU reported by nvidia-smi') }
    [void]$sb.AppendLine('')
    [void]$sb.AppendLine('[limitations]')
    foreach ($l in $limits) { [void]$sb.AppendLine("  - $l") }
    $text = Hide-Identity $sb.ToString()
}

Write-Output $text
if ($OutFile) {
    Set-Content -LiteralPath $OutFile -Value $text -Encoding UTF8
    Write-Host "Report also written to $OutFile (contains no serial numbers or names; review before sharing)."
}
