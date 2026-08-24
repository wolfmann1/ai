<#
.SYNOPSIS
    Hardware inventory for AI/ML lab planning.

.DESCRIPTION
    Reports CPU, memory, GPU, storage and system details with an emphasis on
    what actually constrains local AI workloads: VRAM headroom, memory channel
    population, CUDA compute capability, and free disk for model weights.

.PARAMETER Target
    What to report. One or more of:
      System   - manufacturer, model, motherboard, BIOS
      CPU      - cores, threads, clocks, PCIe lane estimate
      Memory   - DIMMs, speed, channel population analysis
      GPU      - video controllers, PnP status (incl. ghost devices), nvidia-smi
      Storage  - drives, media type, free space, SMART wear where available
      Summary  - condensed verdict for AI lab suitability
      All      - everything above

.PARAMETER AsJson
    Emit structured JSON instead of formatted text.

.PARAMETER OutFile
    Also write output to this path.

.EXAMPLE
    .\Get-LabHardware.ps1 -Target GPU

.EXAMPLE
    .\Get-LabHardware.ps1 -Target CPU,Memory -AsJson

.EXAMPLE
    .\Get-LabHardware.ps1 -Target All -OutFile .\lab-inventory.txt
#>

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('System', 'CPU', 'Memory', 'GPU', 'Storage', 'Summary', 'All')]
    [string[]]$Target = @('Summary'),

    [switch]$AsJson,

    [string]$OutFile
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# ---------------------------------------------------------------- helpers ----

function Format-Bytes {
    param([double]$Bytes)
    if ($null -eq $Bytes -or $Bytes -le 0) { return 'n/a' }
    $units = 'B', 'KB', 'MB', 'GB', 'TB', 'PB'
    $i = 0
    while ($Bytes -ge 1024 -and $i -lt $units.Count - 1) { $Bytes /= 1024; $i++ }
    '{0:N2} {1}' -f $Bytes, $units[$i]
}

function Write-Section {
    param([string]$Title)
    Write-Host ''
    Write-Host ('=' * 74) -ForegroundColor DarkCyan
    Write-Host "  $Title" -ForegroundColor Cyan
    Write-Host ('=' * 74) -ForegroundColor DarkCyan
}

function Write-Note {
    param([string]$Text, [ValidateSet('Info', 'Warn', 'Good')][string]$Level = 'Info')
    $color = switch ($Level) { 'Warn' { 'Yellow' } 'Good' { 'Green' } default { 'Gray' } }
    Write-Host "  -> $Text" -ForegroundColor $color
}

function Test-Command {
    param([string]$Name)
    $null -ne (Get-Command $Name -ErrorAction SilentlyContinue)
}

function Get-CimSafe {
    param([string]$ClassName)
    try { Get-CimInstance -ClassName $ClassName -ErrorAction Stop }
    catch { Write-Verbose "Could not query $ClassName : $_"; @() }
}

# ------------------------------------------------------------- collectors ----

function Get-SystemInfo {
    $cs    = Get-CimSafe Win32_ComputerSystem | Select-Object -First 1
    $bios  = Get-CimSafe Win32_BIOS           | Select-Object -First 1
    $board = Get-CimSafe Win32_BaseBoard      | Select-Object -First 1
    $os    = Get-CimSafe Win32_OperatingSystem | Select-Object -First 1

    [pscustomobject]@{
        Manufacturer   = if ($cs)    { $cs.Manufacturer }        else { 'unknown' }
        Model          = if ($cs)    { $cs.Model }               else { 'unknown' }
        Motherboard    = if ($board) { "$($board.Manufacturer) $($board.Product)" } else { 'unknown' }
        BiosVersion    = if ($bios)  { $bios.SMBIOSBIOSVersion } else { 'unknown' }
        BiosDate       = if ($bios)  { $bios.ReleaseDate }       else { $null }
        OS             = if ($os)    { $os.Caption }             else { 'unknown' }
        OSBuild        = if ($os)    { $os.BuildNumber }         else { 'unknown' }
        TotalRAMBytes  = if ($cs)    { [double]$cs.TotalPhysicalMemory } else { 0 }
    }
}

function Get-CpuInfo {
    Get-CimSafe Win32_Processor | ForEach-Object {
        [pscustomobject]@{
            Name              = $_.Name.Trim()
            Cores             = $_.NumberOfCores
            Threads           = $_.NumberOfLogicalProcessors
            MaxClockMHz       = $_.MaxClockSpeed
            Socket            = $_.SocketDesignation
            L3CacheKB         = $_.L3CacheSize
            Virtualization    = $_.VirtualizationFirmwareEnabled
            AddressWidth      = $_.AddressWidth
        }
    }
}

function Get-MemoryInfo {
    $dimms = Get-CimSafe Win32_PhysicalMemory | ForEach-Object {
        [pscustomobject]@{
            Slot         = $_.DeviceLocator
            Bank         = $_.BankLabel
            CapacityGB   = [math]::Round($_.Capacity / 1GB, 0)
            SpeedMTs     = $_.Speed
            Manufacturer = $_.Manufacturer
            PartNumber   = if ($_.PartNumber) { $_.PartNumber.Trim() } else { '' }
            FormFactor   = $_.FormFactor
        }
    }

    $array = Get-CimSafe Win32_PhysicalMemoryArray | Select-Object -First 1
    $slots = if ($array) { $array.MemoryDevices } else { $null }

    [pscustomobject]@{
        Dimms       = @($dimms)
        Installed   = @($dimms).Count
        TotalSlots  = $slots
        TotalGB     = ( @($dimms) | Measure-Object -Property CapacityGB -Sum ).Sum
        MixedSpeeds = @( @($dimms) | Select-Object -ExpandProperty SpeedMTs -Unique ).Count -gt 1
    }
}

function Get-GpuInfo {
    # WMI view
    $controllers = Get-CimSafe Win32_VideoController | ForEach-Object {
        [pscustomobject]@{
            Name           = $_.Name
            DriverVersion  = $_.DriverVersion
            DriverDate     = $_.DriverDate
            VideoRAMBytes  = [double]$_.AdapterRAM   # unreliable >4GB, noted below
            Status         = $_.Status
            PNPDeviceID    = $_.PNPDeviceID
        }
    }

    # PnP view - catches ghost/non-present devices
    $pnp = @()
    try {
        $pnp = Get-PnpDevice -Class Display -ErrorAction Stop | ForEach-Object {
            [pscustomobject]@{
                FriendlyName = $_.FriendlyName
                Status       = $_.Status
                Present      = $_.Present
                Problem      = $_.Problem
                InstanceId   = $_.InstanceId
            }
        }
    } catch {
        Write-Verbose "Get-PnpDevice unavailable: $_"
    }

    # nvidia-smi view - the only trustworthy VRAM numbers
    $nvidia = @()
    $smiRaw = $null
    if (Test-Command 'nvidia-smi') {
        try {
            $q = 'index,name,memory.total,memory.used,memory.free,compute_cap,driver_version,temperature.gpu,power.draw,power.limit,utilization.gpu,pcie.link.gen.current,pcie.link.width.current'
            $lines = & nvidia-smi --query-gpu=$q --format=csv,noheader,nounits 2>$null
            foreach ($line in $lines) {
                if ([string]::IsNullOrWhiteSpace($line)) { continue }
                $f = $line -split '\s*,\s*'
                $nvidia += [pscustomobject]@{
                    Index          = $f[0]
                    Name           = $f[1]
                    VramTotalMB    = [double]$f[2]
                    VramUsedMB     = [double]$f[3]
                    VramFreeMB     = [double]$f[4]
                    ComputeCap     = $f[5]
                    DriverVersion  = $f[6]
                    TempC          = $f[7]
                    PowerDrawW     = $f[8]
                    PowerLimitW    = $f[9]
                    UtilPct        = $f[10]
                    PcieGen        = $f[11]
                    PcieWidth      = $f[12]
                }
            }
            $smiRaw = (& nvidia-smi 2>$null) -join "`n"
        } catch {
            Write-Verbose "nvidia-smi query failed: $_"
        }
    }

    [pscustomobject]@{
        Controllers   = @($controllers)
        PnpDevices    = @($pnp)
        Nvidia        = @($nvidia)
        NvidiaSmiRaw  = $smiRaw
        HasNvidiaSmi  = (Test-Command 'nvidia-smi')
        GhostDevices  = @($pnp | Where-Object { $_.Present -eq $false })
    }
}

function Get-StorageInfo {
    $drives = Get-CimSafe Win32_DiskDrive | Where-Object { $_.Size -gt 0 } | ForEach-Object {
        [pscustomobject]@{
            Model      = $_.Model
            SizeBytes  = [double]$_.Size
            Interface  = $_.InterfaceType
            MediaType  = $_.MediaType
            Serial     = if ($_.SerialNumber) { $_.SerialNumber.Trim() } else { '' }
            Partitions = $_.Partitions
        }
    }

    $physical = @()
    try {
        $physical = Get-PhysicalDisk -ErrorAction Stop | ForEach-Object {
            $rel = $null
            try { $rel = $_ | Get-StorageReliabilityCounter -ErrorAction Stop } catch { }
            [pscustomobject]@{
                DeviceId    = $_.DeviceId
                Model       = $_.FriendlyName
                MediaType   = $_.MediaType
                BusType     = $_.BusType
                SizeBytes   = [double]$_.Size
                Health      = $_.HealthStatus
                WearPct     = if ($rel) { $rel.Wear } else { $null }
                TempC       = if ($rel) { $rel.Temperature } else { $null }
                ReadErrors  = if ($rel) { $rel.ReadErrorsTotal } else { $null }
                WriteErrors = if ($rel) { $rel.WriteErrorsTotal } else { $null }
                PowerOnHrs  = if ($rel) { $rel.PowerOnHours } else { $null }
            }
        }
    } catch {
        Write-Verbose "Get-PhysicalDisk unavailable (needs admin on some systems): $_"
    }

    $volumes = Get-CimSafe Win32_LogicalDisk | Where-Object { $_.DriveType -eq 3 } | ForEach-Object {
        [pscustomobject]@{
            Drive     = $_.DeviceID
            Label     = $_.VolumeName
            SizeBytes = [double]$_.Size
            FreeBytes = [double]$_.FreeSpace
            FreePct   = if ($_.Size -gt 0) { [math]::Round(($_.FreeSpace / $_.Size) * 100, 1) } else { 0 }
        }
    }

    [pscustomobject]@{
        Drives   = @($drives)
        Physical = @($physical)
        Volumes  = @($volumes)
    }
}

# ------------------------------------------------------------- renderers ----

function Show-System {
    param($Data)
    Write-Section 'SYSTEM'
    Write-Host ("  {0,-16} {1}" -f 'Manufacturer:', $Data.Manufacturer)
    Write-Host ("  {0,-16} {1}" -f 'Model:',        $Data.Model)
    Write-Host ("  {0,-16} {1}" -f 'Motherboard:',  $Data.Motherboard)
    Write-Host ("  {0,-16} {1}" -f 'BIOS:',         $Data.BiosVersion)
    Write-Host ("  {0,-16} {1}" -f 'OS:',           "$($Data.OS) (build $($Data.OSBuild))")
    Write-Host ("  {0,-16} {1}" -f 'Total RAM:',    (Format-Bytes $Data.TotalRAMBytes))
}

function Show-Cpu {
    param($Data)
    Write-Section 'CPU'
    foreach ($c in $Data) {
        Write-Host ("  {0,-16} {1}" -f 'Name:',    $c.Name)
        Write-Host ("  {0,-16} {1}C / {2}T" -f 'Topology:', $c.Cores, $c.Threads)
        Write-Host ("  {0,-16} {1} MHz" -f 'Max clock:', $c.MaxClockMHz)
        Write-Host ("  {0,-16} {1}" -f 'Socket:',  $c.Socket)
        Write-Host ("  {0,-16} {1}" -f 'L3 cache:', (Format-Bytes ($c.L3CacheKB * 1KB)))
        Write-Host ("  {0,-16} {1}" -f 'Virt (FW):', $c.Virtualization)
        Write-Host ''

        if ($c.Threads -ge 12) {
            Write-Note "Enough threads for CPU inference. llama.cpp scales to ~physical core count; try -t $($c.Cores)." 'Good'
        }
        if ($c.Name -match 'Xeon.*v[34]|E5-') {
            Write-Note 'Workstation/server class: typically 40 PCIe lanes and quad-channel memory. Good GPU host.' 'Good'
        }
        if (-not $c.Virtualization) {
            Write-Note 'Virtualization not enabled in firmware. WSL2 and Docker need it - enable VT-x in BIOS.' 'Warn'
        }
    }
}

function Show-Memory {
    param($Data)
    Write-Section 'MEMORY'
    if ($Data.Installed -eq 0) { Write-Note 'No DIMM data returned (try running as Administrator).' 'Warn'; return }

    $Data.Dimms | Format-Table Slot, CapacityGB, SpeedMTs, Manufacturer, PartNumber -AutoSize | Out-Host

    Write-Host ("  {0,-16} {1} GB across {2} DIMM(s)" -f 'Total:', $Data.TotalGB, $Data.Installed)
    if ($Data.TotalSlots) { Write-Host ("  {0,-16} {1}" -f 'Board slots:', $Data.TotalSlots) }
    Write-Host ''

    if ($Data.MixedSpeeds) {
        Write-Note 'Mixed DIMM speeds detected. All modules clock down to the slowest.' 'Warn'
    }

    # Channel population analysis - the number most people get wrong
    if ($Data.TotalSlots -ge 8 -or $Data.Installed -in 3, 5, 6, 7) {
        $likelyChannels = if ($Data.TotalSlots -ge 8) { 4 } else { 2 }
        if ($Data.Installed % $likelyChannels -ne 0) {
            $next = [math]::Ceiling($Data.Installed / $likelyChannels) * $likelyChannels
            Write-Note "$($Data.Installed) DIMMs on a likely $likelyChannels-channel platform: you are NOT in full $likelyChannels-channel mode." 'Warn'
            Write-Note "Populating $next matched DIMMs could recover ~25-30% memory bandwidth." 'Warn'
            Write-Note 'CPU token generation is bandwidth-bound, so this maps almost directly to tokens/sec.' 'Warn'
        } else {
            Write-Note "$($Data.Installed) DIMMs divides evenly into $likelyChannels channels. Good." 'Good'
        }
    }

    # What fits in RAM for CPU inference (Q4_K_M rough sizing)
    Write-Host ''
    Write-Host '  CPU inference headroom (Q4_K_M, weights only, leave ~8GB for OS):' -ForegroundColor DarkGray
    $usable = [math]::Max(0, $Data.TotalGB - 8)
    foreach ($m in @(
        @{ N = 'Llama 3.1 8B';  GB = 4.7 },
        @{ N = 'Qwen 14B';      GB = 9.0 },
        @{ N = 'Mistral 24B';   GB = 14.0 },
        @{ N = 'Qwen 32B';      GB = 19.0 },
        @{ N = 'Llama 70B';     GB = 40.0 }
    )) {
        $fits = if ($usable -ge $m.GB) { 'yes' } else { 'no ' }
        $color = if ($usable -ge $m.GB) { 'Green' } else { 'DarkGray' }
        Write-Host ("    {0,-16} {1,6} GB   {2}" -f $m.N, $m.GB, $fits) -ForegroundColor $color
    }
}

function Show-Gpu {
    param($Data)
    Write-Section 'GPU'

    if ($Data.Controllers.Count -gt 0) {
        Write-Host '  Video controllers (WMI):' -ForegroundColor DarkGray
        $Data.Controllers | Format-Table Name, DriverVersion, Status -AutoSize | Out-Host
        Write-Note 'WMI AdapterRAM is unreliable above 4GB. Trust nvidia-smi below.' 'Info'
    }

    if ($Data.PnpDevices.Count -gt 0) {
        Write-Host ''
        Write-Host '  PnP display devices:' -ForegroundColor DarkGray
        $Data.PnpDevices | Format-Table FriendlyName, Status, Present, Problem -AutoSize | Out-Host
    }

    if ($Data.GhostDevices.Count -gt 0) {
        Write-Host ''
        Write-Note "$($Data.GhostDevices.Count) non-present (ghost) display device(s) found." 'Warn'
        Write-Note 'Present=False means the card is not enumerating on the PCIe bus.' 'Warn'
        Write-Note 'Check PCIe power connectors first, then reseat, then try another slot.' 'Warn'
        Write-Note 'To clear stale entries (as Admin):' 'Info'
        Write-Host '      Get-PnpDevice -Class Display | ? {$_.Present -eq $false} |' -ForegroundColor DarkGray
        Write-Host '        % { pnputil /remove-device $_.InstanceId }' -ForegroundColor DarkGray
    }

    Write-Host ''
    if (-not $Data.HasNvidiaSmi) {
        Write-Note 'nvidia-smi not on PATH. No NVIDIA driver, or not in this shell.' 'Warn'
        return
    }
    if ($Data.Nvidia.Count -eq 0) {
        Write-Note 'nvidia-smi present but returned no GPUs.' 'Warn'
        return
    }

    foreach ($g in $Data.Nvidia) {
        Write-Host "  GPU $($g.Index): $($g.Name)" -ForegroundColor White
        Write-Host ("    {0,-18} {1:N0} MB total / {2:N0} MB used / {3:N0} MB free" -f 'VRAM:', $g.VramTotalMB, $g.VramUsedMB, $g.VramFreeMB)
        Write-Host ("    {0,-18} {1}" -f 'Compute capability:', $g.ComputeCap)
        Write-Host ("    {0,-18} {1}" -f 'Driver:', $g.DriverVersion)
        Write-Host ("    {0,-18} PCIe gen {1} x{2}" -f 'Link:', $g.PcieGen, $g.PcieWidth)
        Write-Host ("    {0,-18} {1}C, {2}W / {3}W" -f 'Thermals:', $g.TempC, $g.PowerDrawW, $g.PowerLimitW)

        # Compute capability gates which parts of the modern stack you can run
        $cc = 0.0
        [double]::TryParse($g.ComputeCap, [ref]$cc) | Out-Null

        if ($cc -gt 0 -and $cc -lt 6.0) {
            Write-Note 'Maxwell or older. Dropped in CUDA 13. No tensor cores, no FlashAttention.' 'Warn'
            Write-Note 'vLLM requires compute capability 7.0+. llama.cpp/Ollama only.' 'Warn'
        } elseif ($cc -lt 7.0) {
            Write-Note 'Pascal era. Works with llama.cpp but no tensor cores; vLLM needs 7.0+.' 'Warn'
        } elseif ($cc -lt 8.0) {
            Write-Note 'Turing/Volta. vLLM works. FlashAttention 2 needs Ampere (8.0+).' 'Info'
        } else {
            Write-Note 'Ampere or newer. Full modern stack: vLLM, FlashAttention, bf16.' 'Good'
        }

        # Display driving off a compute card is the classic silent VRAM tax
        $usedPct = if ($g.VramTotalMB -gt 0) { ($g.VramUsedMB / $g.VramTotalMB) * 100 } else { 0 }
        if ($usedPct -gt 25) {
            Write-Note ("{0:N0}% of VRAM already consumed - likely driving the display. Move the monitor to another GPU to reclaim it." -f $usedPct) 'Warn'
        }

        $freeGB = $g.VramFreeMB / 1024
        Write-Host ''
        Write-Host '    Fits in free VRAM (Q4_K_M weights, before KV cache):' -ForegroundColor DarkGray
        foreach ($m in @(
            @{ N = '3B';   GB = 2.0 },
            @{ N = '8B';   GB = 4.7 },
            @{ N = '14B';  GB = 9.0 },
            @{ N = '32B';  GB = 19.0 }
        )) {
            $ok = $freeGB -ge $m.GB
            Write-Host ("      {0,-6} {1,5} GB   {2}" -f $m.N, $m.GB, $(if ($ok) { 'yes' } else { 'no' })) -ForegroundColor $(if ($ok) { 'Green' } else { 'DarkGray' })
        }
        Write-Host ''
    }

    $totalVram = ($Data.Nvidia | Measure-Object -Property VramTotalMB -Sum).Sum
    if ($Data.Nvidia.Count -gt 1) {
        Write-Note ("{0} GPUs, {1:N0} MB total VRAM. llama.cpp can split layers across them; note that pooling is not seamless." -f $Data.Nvidia.Count, $totalVram) 'Info'
    }
}

function Show-Storage {
    param($Data)
    Write-Section 'STORAGE'

    if ($Data.Drives.Count -gt 0) {
        $Data.Drives |
            Select-Object Model,
                @{ N = 'Size'; E = { Format-Bytes $_.SizeBytes } },
                Interface, MediaType |
            Format-Table -AutoSize | Out-Host
    }

    if ($Data.Physical.Count -gt 0) {
        Write-Host '  Health / reliability:' -ForegroundColor DarkGray
        $Data.Physical |
            Select-Object DeviceId, Model, MediaType, BusType, Health, WearPct, TempC, PowerOnHrs |
            Format-Table -AutoSize | Out-Host

        foreach ($p in $Data.Physical) {
            if ($null -ne $p.WearPct -and $p.WearPct -gt 80) {
                Write-Note "$($p.Model): wear at $($p.WearPct). Plan replacement before it becomes urgent." 'Warn'
            }
            if ($p.Health -and $p.Health -ne 'Healthy') {
                Write-Note "$($p.Model): health reported as $($p.Health)." 'Warn'
            }
        }
    } else {
        Write-Note 'No reliability data. Get-PhysicalDisk often needs an elevated shell.' 'Info'
    }

    if ($Data.Volumes.Count -gt 0) {
        Write-Host '  Volumes:' -ForegroundColor DarkGray
        $Data.Volumes |
            Select-Object Drive, Label,
                @{ N = 'Size'; E = { Format-Bytes $_.SizeBytes } },
                @{ N = 'Free'; E = { Format-Bytes $_.FreeBytes } },
                FreePct |
            Format-Table -AutoSize | Out-Host

        $biggest = $Data.Volumes | Sort-Object FreeBytes -Descending | Select-Object -First 1
        $freeGB  = $biggest.FreeBytes / 1GB
        Write-Note ("Largest free volume: {0} with {1}. Room for roughly {2} model(s) at ~5GB each." -f $biggest.Drive, (Format-Bytes $biggest.FreeBytes), [math]::Floor($freeGB / 5)) 'Info'
        Write-Note "Point Ollama at the roomiest drive: setx OLLAMA_MODELS `"$($biggest.Drive)\ollama\models`"" 'Info'
    }
}

function Show-Summary {
    param($Sys, $Cpu, $Mem, $Gpu, $Sto)
    Write-Section 'AI LAB SUMMARY'

    $c = $Cpu | Select-Object -First 1
    Write-Host ("  {0,-14} {1} {2}" -f 'System:', $Sys.Manufacturer, $Sys.Model)
    Write-Host ("  {0,-14} {1} ({2}C/{3}T)" -f 'CPU:', $c.Name, $c.Cores, $c.Threads)
    Write-Host ("  {0,-14} {1} GB in {2} DIMM(s) @ {3} MT/s" -f 'Memory:', $Mem.TotalGB, $Mem.Installed, ($Mem.Dimms | Select-Object -First 1 -ExpandProperty SpeedMTs))

    if ($Gpu.Nvidia.Count -gt 0) {
        foreach ($g in $Gpu.Nvidia) {
            Write-Host ("  {0,-14} {1} - {2:N0} MB free of {3:N0} MB (cc {4})" -f "GPU $($g.Index):", $g.Name, $g.VramFreeMB, $g.VramTotalMB, $g.ComputeCap)
        }
    } else {
        Write-Host ("  {0,-14} none detected via nvidia-smi" -f 'GPU:')
    }

    $totalDisk = ($Sto.Volumes | Measure-Object -Property SizeBytes -Sum).Sum
    $freeDisk  = ($Sto.Volumes | Measure-Object -Property FreeBytes -Sum).Sum
    Write-Host ("  {0,-14} {1} free of {2}" -f 'Storage:', (Format-Bytes $freeDisk), (Format-Bytes $totalDisk))

    Write-Host ''
    Write-Host '  Verdict' -ForegroundColor Cyan
    Write-Host '  -------' -ForegroundColor Cyan

    $findings = @()

    if ($c.Threads -ge 12) { $findings += @{ L = 'Good'; T = "CPU: $($c.Threads) threads is workable for CPU inference and container workloads." } }
    else                   { $findings += @{ L = 'Warn'; T = "CPU: only $($c.Threads) threads. CPU inference will be slow." } }

    if ($Mem.TotalGB -ge 32) { $findings += @{ L = 'Good'; T = "Memory: $($Mem.TotalGB) GB is the strongest asset here. Run larger models on CPU than the GPU can hold." } }
    else                     { $findings += @{ L = 'Warn'; T = "Memory: $($Mem.TotalGB) GB is tight for running the platform stack plus a model." } }

    if ($Gpu.Nvidia.Count -eq 0) {
        $findings += @{ L = 'Warn'; T = 'GPU: nothing usable detected. CPU-only inference, or rent (RunPod/Vast at ~$0.20-0.30 USD/hr).' }
    } else {
        $best = $Gpu.Nvidia | Sort-Object VramFreeMB -Descending | Select-Object -First 1
        $cc = 0.0; [double]::TryParse($best.ComputeCap, [ref]$cc) | Out-Null
        $freeGB = [math]::Round($best.VramFreeMB / 1024, 1)

        if ($cc -lt 6.0) {
            $findings += @{ L = 'Warn'; T = "GPU: compute capability $($best.ComputeCap) is off the modern software cliff. No vLLM, no FlashAttention." }
            $findings += @{ L = 'Info'; T = 'Cheapest way onto modern CUDA: used RTX 3060 12GB (~$300-400 CAD), no PSU change needed.' }
        }
        if ($freeGB -lt 4.7) {
            $findings += @{ L = 'Warn'; T = "GPU: only $freeGB GB VRAM free - not enough for an 8B model at Q4 (~4.7GB)." }
        }
    }

    if ($Gpu.GhostDevices.Count -gt 0) {
        $findings += @{ L = 'Warn'; T = "GPU: $($Gpu.GhostDevices.Count) ghost display device(s). A card is installed but not enumerating - check PCIe power." }
    }

    $lowDisk = $Sto.Volumes | Where-Object { $_.FreePct -lt 15 }
    foreach ($v in $lowDisk) { $findings += @{ L = 'Warn'; T = "Storage: $($v.Drive) is $($v.FreePct)% free. Model weights will fill this fast." } }

    foreach ($f in $findings) { Write-Note $f.T $f.L }

    Write-Host ''
    Write-Host '  Next steps' -ForegroundColor Cyan
    Write-Host '  ----------' -ForegroundColor Cyan
    Write-Host '    1. Stand up the platform layer CPU-only: Ollama, Open WebUI, Qdrant, Langfuse.' -ForegroundColor Gray
    Write-Host '    2. Point the model layer at a hosted API for anything interactive.' -ForegroundColor Gray
    Write-Host '    3. Use local CPU inference for batch jobs: embeddings, eval suites, overnight runs.' -ForegroundColor Gray
    Write-Host '    4. Rent GPU time (Kaggle free tier, RunPod spot) before buying anything.' -ForegroundColor Gray
    Write-Host '    5. Buy hardware only once a specific workload is demonstrably blocked.' -ForegroundColor Gray
}

# ------------------------------------------------------------------ main ----

if ($Target -contains 'All') {
    $Target = @('System', 'CPU', 'Memory', 'GPU', 'Storage', 'Summary')
}

$needsAll = ($Target -contains 'Summary')

$collected = [ordered]@{}
if ($needsAll -or $Target -contains 'System')  { $collected.System  = Get-SystemInfo }
if ($needsAll -or $Target -contains 'CPU')     { $collected.CPU     = @(Get-CpuInfo) }
if ($needsAll -or $Target -contains 'Memory')  { $collected.Memory  = Get-MemoryInfo }
if ($needsAll -or $Target -contains 'GPU')     { $collected.GPU     = Get-GpuInfo }
if ($needsAll -or $Target -contains 'Storage') { $collected.Storage = Get-StorageInfo }

if ($AsJson) {
    $json = [pscustomobject]$collected | ConvertTo-Json -Depth 8
    if ($OutFile) { $json | Out-File -FilePath $OutFile -Encoding utf8 }
    $json
    return
}

$render = {
    if ($Target -contains 'System')  { Show-System  $collected.System }
    if ($Target -contains 'CPU')     { Show-Cpu     $collected.CPU }
    if ($Target -contains 'Memory')  { Show-Memory  $collected.Memory }
    if ($Target -contains 'GPU')     { Show-Gpu     $collected.GPU }
    if ($Target -contains 'Storage') { Show-Storage $collected.Storage }
    if ($Target -contains 'Summary') {
        Show-Summary $collected.System $collected.CPU $collected.Memory $collected.GPU $collected.Storage
    }
    Write-Host ''
}

if ($OutFile) {
    & $render *>&1 | Tee-Object -FilePath $OutFile
    Write-Host "Written to $OutFile" -ForegroundColor Green
} else {
    & $render
}
