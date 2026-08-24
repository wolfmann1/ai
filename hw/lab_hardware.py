#!/usr/bin/env python3
"""
lab_hardware.py - hardware inventory for AI/ML lab planning.

Reports CPU, memory, GPU, storage and system details with an emphasis on what
actually constrains local AI workloads: VRAM headroom, memory channel
population, CUDA compute capability, and free disk for model weights.

Works on Windows (via PowerShell/CIM), Linux (/proc, /sys, lsblk) and macOS
(sysctl, system_profiler). No third-party packages required; psutil is used
if present but is never necessary.

Usage:
    python lab_hardware.py                          # summary
    python lab_hardware.py --target gpu
    python lab_hardware.py --target cpu memory
    python lab_hardware.py --target all --json
    python lab_hardware.py --target all --out inventory.txt
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from typing import Any

# ----------------------------------------------------------------- config ---

TARGETS = ["system", "cpu", "memory", "gpu", "storage", "summary", "all"]

# Rough Q4_K_M weight sizes. Weights only - KV cache is extra and grows with
# context length, so treat these as a floor, not a budget.
MODEL_SIZES_GB = [
    ("3B", 2.0),
    ("8B", 4.7),
    ("14B", 9.0),
    ("24B", 14.0),
    ("32B", 19.0),
    ("70B", 40.0),
]

IS_WINDOWS = platform.system() == "Windows"
IS_LINUX = platform.system() == "Linux"
IS_MACOS = platform.system() == "Darwin"

# ---------------------------------------------------------------- plumbing ---


class C:
    """ANSI colours, disabled when not a TTY or when NO_COLOR is set."""

    _on = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    HEAD = "\033[36m" if _on else ""
    WARN = "\033[33m" if _on else ""
    GOOD = "\033[32m" if _on else ""
    DIM = "\033[90m" if _on else ""
    BOLD = "\033[1m" if _on else ""
    END = "\033[0m" if _on else ""


def run(cmd: list[str], timeout: int = 20) -> str | None:
    """Run a command, return stdout or None. Never raises."""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.stdout if proc.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def powershell(script: str) -> Any:
    """Run a PowerShell snippet that emits JSON; return the parsed object."""
    if not IS_WINDOWS:
        return None
    exe = shutil.which("pwsh") or shutil.which("powershell")
    if not exe:
        return None
    wrapped = f"$ProgressPreference='SilentlyContinue'; {script}"
    out = run([exe, "-NoProfile", "-NonInteractive", "-Command", wrapped], timeout=45)
    if not out or not out.strip():
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def as_list(value: Any) -> list:
    """PowerShell ConvertTo-Json emits a bare object for single results."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def fmt_bytes(n: float | None) -> str:
    if not n or n <= 0:
        return "n/a"
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if n < 1024 or unit == "PB":
            return f"{n:,.2f} {unit}"
        n /= 1024
    return "n/a"


def section(title: str) -> None:
    print()
    print(f"{C.HEAD}{'=' * 74}{C.END}")
    print(f"{C.HEAD}  {title}{C.END}")
    print(f"{C.HEAD}{'=' * 74}{C.END}")


def note(text: str, level: str = "info") -> None:
    colour = {"warn": C.WARN, "good": C.GOOD}.get(level, C.DIM)
    print(f"  {colour}-> {text}{C.END}")


def fits_table(label: str, budget_gb: float, indent: str = "    ") -> None:
    print(f"{indent}{C.DIM}{label}{C.END}")
    for name, need in MODEL_SIZES_GB:
        ok = budget_gb >= need
        mark = "yes" if ok else "no"
        colour = C.GOOD if ok else C.DIM
        print(f"{indent}  {colour}{name:<6} {need:>5.1f} GB   {mark}{C.END}")


# -------------------------------------------------------------- dataclasses --


@dataclass
class SystemInfo:
    manufacturer: str = "unknown"
    model: str = "unknown"
    motherboard: str = "unknown"
    bios: str = "unknown"
    os_name: str = ""
    os_version: str = ""
    total_ram_bytes: float = 0.0


@dataclass
class CpuInfo:
    name: str = "unknown"
    cores: int = 0
    threads: int = 0
    max_clock_mhz: int = 0
    arch: str = ""
    flags: list[str] = field(default_factory=list)


@dataclass
class Dimm:
    slot: str = ""
    capacity_gb: float = 0.0
    speed_mts: int = 0
    manufacturer: str = ""
    part_number: str = ""


@dataclass
class MemoryInfo:
    dimms: list[Dimm] = field(default_factory=list)
    total_gb: float = 0.0
    installed: int = 0
    total_slots: int | None = None
    mixed_speeds: bool = False


@dataclass
class Gpu:
    index: str = ""
    name: str = ""
    vram_total_mb: float = 0.0
    vram_used_mb: float = 0.0
    vram_free_mb: float = 0.0
    compute_cap: str = ""
    driver: str = ""
    temp_c: str = ""
    power_draw_w: str = ""
    power_limit_w: str = ""
    pcie_gen: str = ""
    pcie_width: str = ""


@dataclass
class PnpDisplay:
    name: str = ""
    status: str = ""
    present: bool | None = None
    problem: Any = None


@dataclass
class GpuInfo:
    nvidia: list[Gpu] = field(default_factory=list)
    pnp: list[PnpDisplay] = field(default_factory=list)
    controllers: list[dict] = field(default_factory=list)
    has_nvidia_smi: bool = False

    @property
    def ghosts(self) -> list[PnpDisplay]:
        return [d for d in self.pnp if d.present is False]


@dataclass
class Volume:
    mount: str = ""
    label: str = ""
    size_bytes: float = 0.0
    free_bytes: float = 0.0

    @property
    def free_pct(self) -> float:
        return round(self.free_bytes / self.size_bytes * 100, 1) if self.size_bytes else 0.0


@dataclass
class Disk:
    model: str = ""
    size_bytes: float = 0.0
    media_type: str = ""
    bus: str = ""
    health: str = ""
    wear_pct: Any = None
    temp_c: Any = None
    power_on_hours: Any = None


@dataclass
class StorageInfo:
    disks: list[Disk] = field(default_factory=list)
    volumes: list[Volume] = field(default_factory=list)


# -------------------------------------------------------------- collectors --


def collect_system() -> SystemInfo:
    info = SystemInfo(
        os_name=platform.system(),
        os_version=platform.version(),
    )

    if IS_WINDOWS:
        data = powershell(
            "$cs=Get-CimInstance Win32_ComputerSystem;"
            "$bb=Get-CimInstance Win32_BaseBoard|Select -First 1;"
            "$b=Get-CimInstance Win32_BIOS|Select -First 1;"
            "[pscustomobject]@{man=$cs.Manufacturer;model=$cs.Model;"
            "board=\"$($bb.Manufacturer) $($bb.Product)\";bios=$b.SMBIOSBIOSVersion;"
            "ram=[double]$cs.TotalPhysicalMemory}|ConvertTo-Json -Compress"
        )
        if data:
            info.manufacturer = data.get("man") or "unknown"
            info.model = data.get("model") or "unknown"
            info.motherboard = (data.get("board") or "").strip() or "unknown"
            info.bios = data.get("bios") or "unknown"
            info.total_ram_bytes = float(data.get("ram") or 0)

    elif IS_LINUX:
        def dmi(name: str) -> str:
            path = f"/sys/devices/virtual/dmi/id/{name}"
            try:
                with open(path) as fh:
                    return fh.read().strip()
            except OSError:
                return ""

        info.manufacturer = dmi("sys_vendor") or "unknown"
        info.model = dmi("product_name") or "unknown"
        info.motherboard = f"{dmi('board_vendor')} {dmi('board_name')}".strip() or "unknown"
        info.bios = dmi("bios_version") or "unknown"
        try:
            with open("/proc/meminfo") as fh:
                for line in fh:
                    if line.startswith("MemTotal:"):
                        info.total_ram_bytes = float(line.split()[1]) * 1024
                        break
        except OSError:
            pass

    elif IS_MACOS:
        info.manufacturer = "Apple"
        info.model = (run(["sysctl", "-n", "hw.model"]) or "unknown").strip()
        mem = run(["sysctl", "-n", "hw.memsize"])
        if mem:
            info.total_ram_bytes = float(mem.strip())

    return info


def collect_cpu() -> CpuInfo:
    info = CpuInfo(arch=platform.machine())

    if IS_WINDOWS:
        data = powershell(
            "Get-CimInstance Win32_Processor|Select-Object -First 1 "
            "Name,NumberOfCores,NumberOfLogicalProcessors,MaxClockSpeed|"
            "ConvertTo-Json -Compress"
        )
        if data:
            info.name = (data.get("Name") or "unknown").strip()
            info.cores = int(data.get("NumberOfCores") or 0)
            info.threads = int(data.get("NumberOfLogicalProcessors") or 0)
            info.max_clock_mhz = int(data.get("MaxClockSpeed") or 0)

    elif IS_LINUX:
        try:
            with open("/proc/cpuinfo") as fh:
                text = fh.read()
            model = re.search(r"^model name\s*:\s*(.+)$", text, re.M)
            if model:
                info.name = model.group(1).strip()
            cores = re.search(r"^cpu cores\s*:\s*(\d+)$", text, re.M)
            if cores:
                info.cores = int(cores.group(1))
            info.threads = text.count("processor\t:") or os.cpu_count() or 0
            flags = re.search(r"^flags\s*:\s*(.+)$", text, re.M)
            if flags:
                info.flags = [f for f in flags.group(1).split() if f in
                              ("avx", "avx2", "avx512f", "amx_bf16", "vmx", "svm")]
        except OSError:
            info.threads = os.cpu_count() or 0

    elif IS_MACOS:
        info.name = (run(["sysctl", "-n", "machdep.cpu.brand_string"]) or "unknown").strip()
        for key, attr in (("hw.physicalcpu", "cores"), ("hw.logicalcpu", "threads")):
            val = run(["sysctl", "-n", key])
            if val:
                setattr(info, attr, int(val.strip()))

    if not info.threads:
        info.threads = os.cpu_count() or 0
    if not info.cores:
        info.cores = info.threads

    return info


def collect_memory() -> MemoryInfo:
    info = MemoryInfo()

    if IS_WINDOWS:
        dimms = as_list(powershell(
            "Get-CimInstance Win32_PhysicalMemory|Select-Object "
            "DeviceLocator,Capacity,Speed,Manufacturer,PartNumber|ConvertTo-Json -Compress"
        ))
        for d in dimms:
            info.dimms.append(Dimm(
                slot=d.get("DeviceLocator") or "",
                capacity_gb=round(float(d.get("Capacity") or 0) / 1024**3),
                speed_mts=int(d.get("Speed") or 0),
                manufacturer=(d.get("Manufacturer") or "").strip(),
                part_number=(d.get("PartNumber") or "").strip(),
            ))
        arr = powershell(
            "Get-CimInstance Win32_PhysicalMemoryArray|Select-Object -First 1 "
            "MemoryDevices|ConvertTo-Json -Compress"
        )
        if arr:
            info.total_slots = arr.get("MemoryDevices")

    elif IS_LINUX:
        # dmidecode needs root; fall back to /proc/meminfo total only.
        out = run(["dmidecode", "-t", "memory"]) if shutil.which("dmidecode") else None
        if out:
            for block in out.split("Memory Device")[1:]:
                size = re.search(r"Size:\s*(\d+)\s*(MB|GB)", block)
                if not size:
                    continue
                gb = float(size.group(1)) / (1024 if size.group(2) == "MB" else 1)
                speed = re.search(r"Configured Memory Speed:\s*(\d+)", block) or \
                    re.search(r"Speed:\s*(\d+)", block)
                loc = re.search(r"Locator:\s*(.+)", block)
                part = re.search(r"Part Number:\s*(.+)", block)
                man = re.search(r"Manufacturer:\s*(.+)", block)
                info.dimms.append(Dimm(
                    slot=loc.group(1).strip() if loc else "",
                    capacity_gb=gb,
                    speed_mts=int(speed.group(1)) if speed else 0,
                    manufacturer=man.group(1).strip() if man else "",
                    part_number=part.group(1).strip() if part else "",
                ))
        if not info.dimms:
            try:
                with open("/proc/meminfo") as fh:
                    for line in fh:
                        if line.startswith("MemTotal:"):
                            info.total_gb = round(float(line.split()[1]) / 1024**2, 1)
                            break
            except OSError:
                pass

    elif IS_MACOS:
        mem = run(["sysctl", "-n", "hw.memsize"])
        if mem:
            info.total_gb = round(float(mem.strip()) / 1024**3, 1)

    if info.dimms:
        info.installed = len(info.dimms)
        info.total_gb = sum(d.capacity_gb for d in info.dimms)
        info.mixed_speeds = len({d.speed_mts for d in info.dimms if d.speed_mts}) > 1

    return info


def collect_gpu() -> GpuInfo:
    info = GpuInfo(has_nvidia_smi=shutil.which("nvidia-smi") is not None)

    if info.has_nvidia_smi:
        fields = ("index,name,memory.total,memory.used,memory.free,compute_cap,"
                  "driver_version,temperature.gpu,power.draw,power.limit,"
                  "pcie.link.gen.current,pcie.link.width.current")
        out = run(["nvidia-smi", f"--query-gpu={fields}",
                   "--format=csv,noheader,nounits"])
        if out:
            for line in out.strip().splitlines():
                if not line.strip():
                    continue
                p = [x.strip() for x in line.split(",")]
                if len(p) < 12:
                    continue

                def num(v: str) -> float:
                    try:
                        return float(v)
                    except ValueError:
                        return 0.0

                info.nvidia.append(Gpu(
                    index=p[0], name=p[1],
                    vram_total_mb=num(p[2]), vram_used_mb=num(p[3]),
                    vram_free_mb=num(p[4]), compute_cap=p[5], driver=p[6],
                    temp_c=p[7], power_draw_w=p[8], power_limit_w=p[9],
                    pcie_gen=p[10], pcie_width=p[11],
                ))

    if IS_WINDOWS:
        ctrls = as_list(powershell(
            "Get-CimInstance Win32_VideoController|Select-Object "
            "Name,DriverVersion,Status|ConvertTo-Json -Compress"
        ))
        info.controllers = [c for c in ctrls if isinstance(c, dict)]

        pnp = as_list(powershell(
            "Get-PnpDevice -Class Display|Select-Object "
            "FriendlyName,Status,Present,Problem|ConvertTo-Json -Compress"
        ))
        for d in pnp:
            info.pnp.append(PnpDisplay(
                name=d.get("FriendlyName") or "",
                status=str(d.get("Status") or ""),
                present=d.get("Present"),
                problem=d.get("Problem"),
            ))

    elif IS_LINUX and shutil.which("lspci"):
        out = run(["lspci", "-nn"]) or ""
        for line in out.splitlines():
            if re.search(r"VGA|3D controller|Display controller", line):
                info.controllers.append({"Name": line.strip()})

    return info


def collect_storage() -> StorageInfo:
    info = StorageInfo()

    if IS_WINDOWS:
        disks = as_list(powershell(
            "Get-PhysicalDisk|ForEach-Object{"
            "$r=$null;try{$r=$_|Get-StorageReliabilityCounter -EA Stop}catch{};"
            "[pscustomobject]@{model=$_.FriendlyName;size=[double]$_.Size;"
            "media=[string]$_.MediaType;bus=[string]$_.BusType;"
            "health=[string]$_.HealthStatus;wear=$r.Wear;temp=$r.Temperature;"
            "hours=$r.PowerOnHours}}|ConvertTo-Json -Compress"
        ))
        if not disks:
            disks = as_list(powershell(
                "Get-CimInstance Win32_DiskDrive|Where-Object{$_.Size -gt 0}|"
                "ForEach-Object{[pscustomobject]@{model=$_.Model;"
                "size=[double]$_.Size;media=[string]$_.MediaType;"
                "bus=[string]$_.InterfaceType}}|ConvertTo-Json -Compress"
            ))
        for d in disks:
            info.disks.append(Disk(
                model=(d.get("model") or "").strip(),
                size_bytes=float(d.get("size") or 0),
                media_type=d.get("media") or "",
                bus=d.get("bus") or "",
                health=d.get("health") or "",
                wear_pct=d.get("wear"),
                temp_c=d.get("temp"),
                power_on_hours=d.get("hours"),
            ))

        vols = as_list(powershell(
            "Get-CimInstance Win32_LogicalDisk|Where-Object{$_.DriveType -eq 3}|"
            "Select-Object DeviceID,VolumeName,Size,FreeSpace|ConvertTo-Json -Compress"
        ))
        for v in vols:
            info.volumes.append(Volume(
                mount=v.get("DeviceID") or "",
                label=v.get("VolumeName") or "",
                size_bytes=float(v.get("Size") or 0),
                free_bytes=float(v.get("FreeSpace") or 0),
            ))

    else:
        if shutil.which("lsblk"):
            out = run(["lsblk", "-dbJ", "-o", "NAME,MODEL,SIZE,ROTA,TRAN"])
            if out:
                try:
                    for d in json.loads(out).get("blockdevices", []):
                        info.disks.append(Disk(
                            model=d.get("model") or d.get("name") or "",
                            size_bytes=float(d.get("size") or 0),
                            media_type="HDD" if d.get("rota") else "SSD",
                            bus=(d.get("tran") or "").upper(),
                        ))
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass

        seen: set[str] = set()
        for part in ("/", "/home", os.path.expanduser("~")):
            try:
                usage = shutil.disk_usage(part)
            except OSError:
                continue
            key = f"{usage.total}"
            if key in seen:
                continue
            seen.add(key)
            info.volumes.append(Volume(
                mount=part,
                size_bytes=float(usage.total),
                free_bytes=float(usage.free),
            ))

    return info


# --------------------------------------------------------------- renderers --


def show_system(s: SystemInfo) -> None:
    section("SYSTEM")
    print(f"  {'Manufacturer:':<16} {s.manufacturer}")
    print(f"  {'Model:':<16} {s.model}")
    print(f"  {'Motherboard:':<16} {s.motherboard}")
    print(f"  {'BIOS:':<16} {s.bios}")
    print(f"  {'OS:':<16} {s.os_name} {s.os_version}")
    print(f"  {'Total RAM:':<16} {fmt_bytes(s.total_ram_bytes)}")


def show_cpu(c: CpuInfo) -> None:
    section("CPU")
    print(f"  {'Name:':<16} {c.name}")
    print(f"  {'Topology:':<16} {c.cores}C / {c.threads}T")
    if c.max_clock_mhz:
        print(f"  {'Max clock:':<16} {c.max_clock_mhz} MHz")
    print(f"  {'Architecture:':<16} {c.arch}")
    if c.flags:
        print(f"  {'Notable flags:':<16} {', '.join(c.flags)}")
    print()

    if c.threads >= 12:
        note(f"{c.threads} threads is workable for CPU inference. "
             f"llama.cpp scales to about physical cores: try -t {c.cores}.", "good")
    else:
        note(f"Only {c.threads} thread(s). CPU inference will be slow.", "warn")

    if re.search(r"Xeon.*v[34]|E5-\d", c.name):
        note("Workstation/server class: typically 40 PCIe lanes and quad-channel "
             "memory. Good host for a future GPU.", "good")

    if c.flags and "avx512f" in c.flags:
        note("AVX-512 present: llama.cpp has optimised kernels for it.", "good")


def show_memory(m: MemoryInfo) -> None:
    section("MEMORY")

    if m.dimms:
        print(f"  {'Slot':<14}{'GB':>5}{'MT/s':>8}  {'Manufacturer':<16}Part")
        print(f"  {C.DIM}{'-' * 64}{C.END}")
        for d in m.dimms:
            print(f"  {d.slot:<14}{d.capacity_gb:>5.0f}{d.speed_mts:>8}  "
                  f"{d.manufacturer[:16]:<16}{d.part_number}")
        print()

    print(f"  {'Total:':<16} {m.total_gb:.0f} GB"
          + (f" across {m.installed} DIMM(s)" if m.installed else ""))
    if m.total_slots:
        print(f"  {'Board slots:':<16} {m.total_slots}")
    print()

    if m.mixed_speeds:
        note("Mixed DIMM speeds. All modules clock down to the slowest.", "warn")

    # Channel population - the number people most often leave on the table
    if m.installed:
        channels = 4 if (m.total_slots or 0) >= 8 else 2
        if m.installed % channels != 0:
            need = -(-m.installed // channels) * channels
            note(f"{m.installed} DIMMs on a likely {channels}-channel platform: "
                 f"not running full {channels}-channel.", "warn")
            note(f"Populating {need} matched DIMMs recovers roughly 25-30% "
                 f"memory bandwidth.", "warn")
            note("CPU token generation is bandwidth-bound, so that maps almost "
                 "directly to tokens/sec.", "warn")
        else:
            note(f"{m.installed} DIMMs divides evenly into {channels} channels.", "good")

    print()
    usable = max(0.0, m.total_gb - 8)
    fits_table(f"CPU inference headroom (Q4_K_M, {usable:.0f} GB usable after ~8 GB for OS):",
               usable, indent="  ")


def show_gpu(g: GpuInfo) -> None:
    section("GPU")

    if g.controllers:
        print(f"  {C.DIM}Video controllers:{C.END}")
        for c in g.controllers:
            name = c.get("Name", "")
            drv = c.get("DriverVersion", "")
            print(f"    {name}" + (f"  (driver {drv})" if drv else ""))
        note("OS-reported VRAM is unreliable above 4GB. Trust nvidia-smi below.")
        print()

    if g.pnp:
        print(f"  {C.DIM}PnP display devices:{C.END}")
        print(f"    {'Name':<34}{'Status':<10}{'Present':<9}Problem")
        for d in g.pnp:
            print(f"    {d.name[:33]:<34}{d.status:<10}{str(d.present):<9}{d.problem}")
        print()

    if g.ghosts:
        note(f"{len(g.ghosts)} non-present (ghost) display device(s).", "warn")
        note("Present=False means the card is not enumerating on the PCIe bus. "
             "Check PCIe power connectors first, then reseat, then try another slot.", "warn")
        note("Clear stale entries (elevated PowerShell):")
        print(f"      {C.DIM}Get-PnpDevice -Class Display | ? {{$_.Present -eq $false}} |{C.END}")
        print(f"        {C.DIM}% {{ pnputil /remove-device $_.InstanceId }}{C.END}")
        print()

    if not g.has_nvidia_smi:
        note("nvidia-smi not on PATH. No NVIDIA driver, or not in this shell.", "warn")
        return
    if not g.nvidia:
        note("nvidia-smi present but returned no GPUs.", "warn")
        return

    for gpu in g.nvidia:
        print(f"  {C.BOLD}GPU {gpu.index}: {gpu.name}{C.END}")
        print(f"    {'VRAM:':<20} {gpu.vram_total_mb:,.0f} MB total / "
              f"{gpu.vram_used_mb:,.0f} used / {gpu.vram_free_mb:,.0f} free")
        print(f"    {'Compute capability:':<20} {gpu.compute_cap}")
        print(f"    {'Driver:':<20} {gpu.driver}")
        print(f"    {'Link:':<20} PCIe gen {gpu.pcie_gen} x{gpu.pcie_width}")
        print(f"    {'Thermals:':<20} {gpu.temp_c}C, "
              f"{gpu.power_draw_w}W / {gpu.power_limit_w}W")

        try:
            cc = float(gpu.compute_cap)
        except ValueError:
            cc = 0.0

        if 0 < cc < 6.0:
            note("Maxwell or older. Dropped in CUDA 13. No tensor cores, "
                 "no FlashAttention. vLLM needs 7.0+, so llama.cpp/Ollama only.", "warn")
        elif cc < 7.0:
            note("Pascal era. llama.cpp works, but no tensor cores and vLLM needs 7.0+.", "warn")
        elif cc < 8.0:
            note("Turing/Volta. vLLM works; FlashAttention 2 needs Ampere (8.0+).")
        elif cc >= 8.0:
            note("Ampere or newer. Full modern stack: vLLM, FlashAttention, bf16.", "good")

        if gpu.vram_total_mb and gpu.vram_used_mb / gpu.vram_total_mb > 0.25:
            pct = gpu.vram_used_mb / gpu.vram_total_mb * 100
            note(f"{pct:.0f}% of VRAM already consumed, most likely driving the "
                 f"display. Move the monitor to another GPU to reclaim it.", "warn")

        print()
        fits_table("Fits in free VRAM (Q4_K_M weights, before KV cache):",
                   gpu.vram_free_mb / 1024)
        print()

    if len(g.nvidia) > 1:
        total = sum(x.vram_total_mb for x in g.nvidia)
        note(f"{len(g.nvidia)} GPUs, {total:,.0f} MB total VRAM. llama.cpp can split "
             f"layers across them, but pooling is not seamless.")


def show_storage(s: StorageInfo) -> None:
    section("STORAGE")

    if s.disks:
        print(f"  {'Model':<34}{'Size':>12}  {'Media':<8}{'Bus':<8}{'Health':<10}Wear")
        print(f"  {C.DIM}{'-' * 84}{C.END}")
        for d in s.disks:
            wear = "" if d.wear_pct is None else str(d.wear_pct)
            print(f"  {d.model[:33]:<34}{fmt_bytes(d.size_bytes):>12}  "
                  f"{d.media_type[:8]:<8}{d.bus[:8]:<8}{d.health[:10]:<10}{wear}")
        print()

        for d in s.disks:
            if d.wear_pct is not None and isinstance(d.wear_pct, (int, float)) and d.wear_pct > 80:
                note(f"{d.model}: wear at {d.wear_pct}. Plan replacement before "
                     f"it becomes urgent.", "warn")
            if d.health and d.health.lower() not in ("healthy", ""):
                note(f"{d.model}: health reported as {d.health}.", "warn")
    else:
        note("No disk detail available (may need an elevated shell).")

    if s.volumes:
        print(f"  {C.DIM}Volumes:{C.END}")
        print(f"    {'Mount':<12}{'Label':<16}{'Size':>12}{'Free':>12}{'Free %':>9}")
        for v in s.volumes:
            print(f"    {v.mount:<12}{v.label[:15]:<16}{fmt_bytes(v.size_bytes):>12}"
                  f"{fmt_bytes(v.free_bytes):>12}{v.free_pct:>8.1f}%")
        print()

        biggest = max(s.volumes, key=lambda v: v.free_bytes)
        free_gb = biggest.free_bytes / 1024**3
        note(f"Largest free volume: {biggest.mount} with {fmt_bytes(biggest.free_bytes)} "
             f"- room for roughly {int(free_gb // 5)} model(s) at ~5 GB each.")
        target = (f"{biggest.mount}\\ollama\\models" if IS_WINDOWS
                  else os.path.join(biggest.mount, "ollama", "models"))
        note(f"Point Ollama at it: OLLAMA_MODELS={target}")

        for v in s.volumes:
            if v.free_pct < 15:
                note(f"{v.mount} is only {v.free_pct}% free. Model weights fill "
                     f"this fast.", "warn")


def show_summary(s: SystemInfo, c: CpuInfo, m: MemoryInfo,
                 g: GpuInfo, st: StorageInfo) -> None:
    section("AI LAB SUMMARY")

    print(f"  {'System:':<14} {s.manufacturer} {s.model}")
    print(f"  {'CPU:':<14} {c.name} ({c.cores}C/{c.threads}T)")
    speed = m.dimms[0].speed_mts if m.dimms else 0
    print(f"  {'Memory:':<14} {m.total_gb:.0f} GB in {m.installed} DIMM(s)"
          + (f" @ {speed} MT/s" if speed else ""))

    if g.nvidia:
        for gpu in g.nvidia:
            print(f"  GPU {gpu.index}:{'':<8} {gpu.name} - {gpu.vram_free_mb:,.0f} MB free "
                  f"of {gpu.vram_total_mb:,.0f} MB (cc {gpu.compute_cap})")
    else:
        print(f"  {'GPU:':<14} none detected via nvidia-smi")

    total = sum(v.size_bytes for v in st.volumes)
    free = sum(v.free_bytes for v in st.volumes)
    print(f"  {'Storage:':<14} {fmt_bytes(free)} free of {fmt_bytes(total)}")

    print()
    print(f"  {C.HEAD}Verdict{C.END}")
    print(f"  {C.HEAD}-------{C.END}")

    if c.threads >= 12:
        note(f"CPU: {c.threads} threads is fine for CPU inference and containers.", "good")
    else:
        note(f"CPU: only {c.threads} thread(s). CPU inference will be slow.", "warn")

    if m.total_gb >= 32:
        note(f"Memory: {m.total_gb:.0f} GB is likely your strongest asset. You can run "
             f"larger models on CPU than the GPU can hold.", "good")
    else:
        note(f"Memory: {m.total_gb:.0f} GB is tight for the platform stack plus a model.", "warn")

    if not g.nvidia:
        note("GPU: nothing usable detected. CPU-only inference, or rent "
             "(Kaggle free tier; RunPod/Vast around $0.20-0.30 USD/hr).", "warn")
    else:
        best = max(g.nvidia, key=lambda x: x.vram_free_mb)
        try:
            cc = float(best.compute_cap)
        except ValueError:
            cc = 0.0
        free_gb = best.vram_free_mb / 1024
        if cc and cc < 6.0:
            note(f"GPU: compute capability {best.compute_cap} is off the modern "
                 f"software cliff. No vLLM, no FlashAttention.", "warn")
            note("Cheapest route onto modern CUDA: used RTX 3060 12GB "
                 "(~$300-400 CAD), no PSU change needed.")
        if free_gb < 4.7:
            note(f"GPU: only {free_gb:.1f} GB VRAM free - not enough for an 8B "
                 f"model at Q4 (~4.7 GB).", "warn")

    if g.ghosts:
        note(f"GPU: {len(g.ghosts)} ghost display device(s). A card is installed "
             f"but not enumerating - check PCIe power first.", "warn")

    print()
    print(f"  {C.HEAD}Next steps{C.END}")
    print(f"  {C.HEAD}----------{C.END}")
    for i, step in enumerate((
        "Stand up the platform layer CPU-only: Ollama, Open WebUI, Qdrant, Langfuse.",
        "Point the model layer at a hosted API for anything interactive.",
        "Use local CPU inference for batch work: embeddings, eval suites, overnight runs.",
        "Rent GPU time (Kaggle free tier, RunPod spot) before buying anything.",
        "Buy hardware only once a specific workload is demonstrably blocked.",
    ), 1):
        print(f"    {C.DIM}{i}. {step}{C.END}")


# -------------------------------------------------------------------- main --


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Hardware inventory for AI/ML lab planning.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage:")[-1],
    )
    parser.add_argument(
        "-t", "--target", nargs="+", choices=TARGETS, default=["summary"],
        metavar="TARGET",
        help="one or more of: " + ", ".join(TARGETS),
    )
    parser.add_argument("--json", action="store_true",
                        help="emit structured JSON instead of formatted text")
    parser.add_argument("--out", metavar="PATH",
                        help="also write output to this file")
    args = parser.parse_args()

    targets = set(args.target)
    if "all" in targets:
        targets = {"system", "cpu", "memory", "gpu", "storage", "summary"}
    need_all = "summary" in targets

    sysinfo = collect_system() if need_all or "system" in targets else None
    cpu = collect_cpu() if need_all or "cpu" in targets else None
    mem = collect_memory() if need_all or "memory" in targets else None
    gpu = collect_gpu() if need_all or "gpu" in targets else None
    sto = collect_storage() if need_all or "storage" in targets else None

    if args.json:
        payload = {
            k: asdict(v) for k, v in (
                ("system", sysinfo), ("cpu", cpu), ("memory", mem),
                ("gpu", gpu), ("storage", sto),
            ) if v is not None
        }
        text = json.dumps(payload, indent=2, default=str)
        print(text)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(text)
        return 0

    buffer: list[str] = []
    real_print = print

    if args.out:
        import builtins

        def capturing_print(*a, **kw):
            buffer.append(" ".join(str(x) for x in a))
            real_print(*a, **kw)

        builtins.print = capturing_print  # type: ignore[assignment]

    try:
        if "system" in targets and sysinfo:
            show_system(sysinfo)
        if "cpu" in targets and cpu:
            show_cpu(cpu)
        if "memory" in targets and mem:
            show_memory(mem)
        if "gpu" in targets and gpu:
            show_gpu(gpu)
        if "storage" in targets and sto:
            show_storage(sto)
        if "summary" in targets and all(x is not None for x in (sysinfo, cpu, mem, gpu, sto)):
            show_summary(sysinfo, cpu, mem, gpu, sto)  # type: ignore[arg-type]
        print()
    finally:
        if args.out:
            import builtins
            builtins.print = real_print  # type: ignore[assignment]
            clean = re.compile(r"\033\[[0-9;]*m")
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write("\n".join(clean.sub("", line) for line in buffer))
            print(f"{C.GOOD}Written to {args.out}{C.END}")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
