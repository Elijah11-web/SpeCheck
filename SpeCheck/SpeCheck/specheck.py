import json
import os
import platform
import re
import subprocess
import tempfile
import threading
import tkinter as tk
import webbrowser
import ctypes
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

APP_NAME = "SpeCheck"
BG = "#F3F6FA"
PANEL = "#FFFFFF"
PANEL_2 = "#EDF2F7"
BORDER = "#D7E0EA"
TEXT = "#172436"
MUTED = "#5F6F83"
CYAN = "#0F7185"
BLUE = "#3567C8"
GREEN = "#18804B"
AMBER = "#A85B08"
RED = "#B42332"
SIDEBAR = "#FFFFFF"
TRACK = "#E4EAF1"
INFO_BG = "#EAF1FA"
SUCCESS_BG = "#E9F6EF"
WARNING_BG = "#FFF3E3"
DANGER_BG = "#FCEBED"

POWERSHELL = r'''
$ErrorActionPreference = 'SilentlyContinue'
$ProgressPreference = 'SilentlyContinue'
$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1 Name, NumberOfCores, NumberOfLogicalProcessors, MaxClockSpeed
$os = Get-CimInstance Win32_OperatingSystem
$cs = Get-CimInstance Win32_ComputerSystem
$gpus = @(Get-CimInstance Win32_VideoController | ForEach-Object {
  [pscustomobject]@{ Name=$_.Name; AdapterRAM=$_.AdapterRAM; DriverVersion=$_.DriverVersion; Status=$_.Status }
})
$memoryModules = @(Get-CimInstance Win32_PhysicalMemory | ForEach-Object {
  [pscustomobject]@{
    Capacity=[double]$_.Capacity; Manufacturer=$_.Manufacturer; PartNumber=([string]$_.PartNumber).Trim();
    DeviceLocator=$_.DeviceLocator; BankLabel=$_.BankLabel; Speed=[uint32]$_.Speed;
    ConfiguredClockSpeed=[uint32]$_.ConfiguredClockSpeed
  }
})
$static = @(Get-CimInstance -Namespace root/wmi -ClassName BatteryStaticData | ForEach-Object { [double]$_.DesignedCapacity })
$full = @(Get-CimInstance -Namespace root/wmi -ClassName BatteryFullChargedCapacity | ForEach-Object { [double]$_.FullChargedCapacity })
$batteryCycleCounts = @(Get-CimInstance -Namespace root/wmi -ClassName BatteryCycleCount | ForEach-Object {
  [pscustomobject]@{ CycleCount=[uint32]$_.CycleCount }
})
$designCapacity = ($static | Measure-Object -Sum).Sum
$fullCapacity = ($full | Measure-Object -Sum).Sum
$batt = @(Get-CimInstance Win32_Battery | ForEach-Object {
  [pscustomobject]@{ Name=$_.Name; Status=$_.Status; EstimatedChargeRemaining=$_.EstimatedChargeRemaining; BatteryStatus=$_.BatteryStatus }
})
$disks = @(Get-PhysicalDisk | ForEach-Object {
  [pscustomobject]@{
    FriendlyName=$_.FriendlyName; Model=$_.FriendlyName; MediaType=[string]$_.MediaType;
    BusType=[string]$_.BusType; HealthStatus=[string]$_.HealthStatus;
    OperationalStatus=(@($_.OperationalStatus) -join ', '); Size=[double]$_.Size
  }
})
$volumes = @(Get-CimInstance Win32_LogicalDisk | Where-Object { $_.DriveType -eq 3 } | ForEach-Object {
  [pscustomobject]@{ DeviceID=$_.DeviceID; VolumeName=$_.VolumeName; Size=[double]$_.Size; FreeSpace=[double]$_.FreeSpace }
})
$hardwareProviders = @('WHEA-Logger','Disk','stornvme','storahci','Ntfs','Display','nvlddmkm','amdkmdag')
$since = (Get-Date).AddDays(-14)
$systemErrorsAvailable = $true
$systemErrors = @()
try {
  $systemErrors = @(Get-WinEvent -FilterHashtable @{
    LogName='System'; ProviderName=$hardwareProviders; Level=@(1,2); StartTime=$since
  } -MaxEvents 500 -ErrorAction Stop | Group-Object ProviderName | ForEach-Object {
    [pscustomobject]@{ Provider=$_.Name; Count=$_.Count }
  })
} catch {
  $systemErrorsAvailable = $_.FullyQualifiedErrorId -like 'NoMatchingEventsFound*'
  $systemErrors = @()
}
$payload = [pscustomobject]@{
  computer=$env:COMPUTERNAME; manufacturer=$cs.Manufacturer; model=$cs.Model;
  os=$os.Caption; osVersion=$os.Version; cpu=$cpu; gpus=$gpus; batteries=$batt;
  designCapacity=$designCapacity; fullCapacity=$fullCapacity; batteryCycleCounts=$batteryCycleCounts;
  memoryModules=$memoryModules;
  memoryTotalKB=[double]$os.TotalVisibleMemorySize; memoryFreeKB=[double]$os.FreePhysicalMemory;
  disks=$disks; volumes=$volumes; systemErrors=$systemErrors; systemErrorsAvailable=$systemErrorsAvailable
}
$payload | ConvertTo-Json -Depth 5 -Compress
'''


class _VisibleText(HTMLParser):
    """Collect report text without depending on browser-specific HTML formatting."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        if data and data.strip():
            self.parts.append(data.strip())


def parse_battery_report(report_html):
    parser = _VisibleText()
    parser.feed(report_html)
    text = " ".join(parser.parts).replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)

    def capacity(label):
        match = re.search(rf"\b{label}\s*[:\s]+([\d,]+(?:\.\d+)?)\s*mWh\b", text, re.IGNORECASE)
        return float(match.group(1).replace(",", "")) if match else None

    return {
        "designCapacity": capacity("DESIGN CAPACITY"),
        "fullCapacity": capacity("FULL CHARGE CAPACITY"),
    }


def read_windows_battery_report():
    """Use powercfg's local report only when WMI did not return both capacities."""
    fd, raw_path = tempfile.mkstemp(prefix="specheck-battery-", suffix=".html")
    os.close(fd)
    report_path = Path(raw_path)
    try:
        proc = subprocess.run(
            ["powercfg.exe", "/batteryreport", "/output", str(report_path)],
            capture_output=True, text=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if proc.returncode != 0 or not report_path.is_file():
            return None
        parsed = parse_battery_report(report_path.read_text(encoding="utf-8", errors="replace"))
        if parsed["designCapacity"] and parsed["fullCapacity"]:
            parsed["capacitySource"] = "Windows battery report"
            return parsed
        return None
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None
    finally:
        try:
            report_path.unlink(missing_ok=True)
        except OSError:
            pass


def powershell_scan(fallback_to_battery_report=True):
    if platform.system() != "Windows":
        raise RuntimeError("SpeCheck’s scan currently requires Windows 10 or 11.")
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", POWERSHELL],
            capture_output=True, text=True, timeout=45,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError as exc:
        raise RuntimeError("Windows PowerShell was not found. SpeCheck needs Windows PowerShell.") from exc
    if proc.returncode != 0 or not proc.stdout.strip():
        detail = (proc.stderr or "Windows did not return hardware information.").strip()
        raise RuntimeError(detail[-500:])
    try:
        data = json.loads(proc.stdout.strip().splitlines()[-1].lstrip("\ufeff"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("Windows returned unreadable scan data. Run Scan again.") from exc

    design = number(data.get("designCapacity"))
    full = number(data.get("fullCapacity"))
    if design <= 0 or full <= 0:
        if not fallback_to_battery_report:
            return data
        report = read_windows_battery_report()
        if report:
            data["designCapacity"] = report["designCapacity"]
            data["fullCapacity"] = report["fullCapacity"]
            data["batteryCapacitySource"] = report["capacitySource"]
    else:
        data["batteryCapacitySource"] = "Windows battery interface"
    return data


def number(value, default=0):
    try:
        return float(value) if value is not None else default
    except (ValueError, TypeError):
        return default


def as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def memory_capacity_text(capacity_kb):
    return f"{number(capacity_kb) / 1024 / 1024:.1f} GB"


def storage_usage(total_bytes, free_bytes):
    total = number(total_bytes)
    free = number(free_bytes, default=-1)
    if total <= 0 or free < 0 or free > total:
        return None
    return {"total": total, "free": free, "usedPercent": (total - free) / total * 100}


def gigabytes(byte_count):
    return f"{number(byte_count) / 1024**3:.1f} GB"


def memory_module_speed(module):
    configured = number(module.get("ConfiguredClockSpeed"))
    return configured or number(module.get("Speed"))


def memory_module_summary(modules):
    modules = as_list(modules)
    if not modules:
        return "Module details not reported"
    speeds = sorted({int(memory_module_speed(module)) for module in modules if memory_module_speed(module) > 0})
    if not speeds:
        return f"{len(modules)} module(s) · speed not reported"
    speed_text = ", ".join(f"{speed} MHz" for speed in speeds)
    return f"{len(modules)} module(s) · {speed_text} configured speed"


def memory_module_detail(module, index):
    parts = []
    capacity = number(module.get("Capacity"))
    if capacity > 0:
        parts.append(gigabytes(capacity))
    manufacturer = str(module.get("Manufacturer") or "").strip()
    part_number = str(module.get("PartNumber") or "").strip()
    if manufacturer and manufacturer not in {"0", "Unknown"}:
        parts.append(manufacturer)
    if part_number and part_number not in {"0", "Unknown"}:
        parts.append(part_number)
    locator = str(module.get("DeviceLocator") or module.get("BankLabel") or "").strip()
    if locator:
        parts.append(locator)
    speed = memory_module_speed(module)
    if speed > 0:
        parts.append(f"{int(speed)} MHz configured speed")
    return " · ".join(parts) or f"Module {index} · Windows did not return module details"


def battery_cycle_count_text(data):
    counts = [int(number(item.get("CycleCount"))) for item in as_list(data.get("batteryCycleCounts"))]
    counts = [count for count in counts if count > 0]
    if not counts:
        return "Not reported"
    if len(counts) == 1:
        return f"{counts[0]} cycles"
    return " · ".join(f"Battery {index}: {count} cycles" for index, count in enumerate(counts, start=1))


def disk_type_text(disk):
    media = str(disk.get("MediaType") or "").strip().casefold()
    mapped = {"3": "HDD", "4": "SSD", "5": "SCM", "solid state": "SSD", "rotational": "HDD"}
    if media in mapped:
        return mapped[media]
    if media in {"ssd", "hdd", "scm"}:
        return media.upper()
    return "Type not reported"


def disk_model_text(disk):
    return str(disk.get("Model") or disk.get("FriendlyName") or "Drive model not reported").strip()


def disk_spec_text(disk):
    parts = [disk_type_text(disk), disk_model_text(disk), f"Windows health: {disk_status(disk)}"]
    bus = str(disk.get("BusType") or "").strip()
    if bus and bus.casefold() not in {"unknown", "unspecified"}:
        parts.insert(1, bus)
    return " · ".join(parts)


def device_logo_key(manufacturer, model=""):
    value = f"{manufacturer or ''} {model or ''}".casefold()
    if "alienware" in value:
        return "alienware"
    if value.strip() == "hp" or value.startswith("hp ") or "hewlett" in value:
        return "hp"
    if value.strip() == "lg":
        return "lg"
    aliases = {
        "dell": ("dell",),
        "lenovo": ("lenovo",), "asus": ("asus",), "acer": ("acer",),
        "msi": ("micro-star", "msi"), "apple": ("apple",), "microsoft": ("microsoft",),
        "samsung": ("samsung",), "gigabyte": ("gigabyte",), "razer": ("razer",),
        "lg": ("lg electronics",), "huawei": ("huawei",), "toshiba": ("toshiba",),
        "framework": ("framework",), "alienware": ("alienware",), "fujitsu": ("fujitsu",),
    }
    for key, needles in aliases.items():
        if any(needle in value for needle in needles):
            return key
    return None


def reported_vram_text(byte_count):
    memory = number(byte_count)
    if memory <= 0:
        return ""
    if memory >= 3.9 * 1024**3:
        return "≥4 GB reported (verify exact GPU spec; Windows may cap this field)"
    return f"{memory / 1024**3:.1f} GB reported adapter memory"


def battery_health_percent(design_capacity, full_capacity):
    design = number(design_capacity)
    full = number(full_capacity)
    return full / design * 100 if design > 0 and full > 0 else None


def disk_status(disk):
    value = str(disk.get("HealthStatus") or "Unknown").strip()
    if value.lower() in {"healthy", "normal", "ok"}:
        return "Healthy"
    if value.lower() in {"warning", "degraded"}:
        return "Warning"
    if value.lower() in {"unhealthy", "failed", "critical"}:
        return "Unhealthy"
    return "Not reported"


def select_primary_gpu(gpus):
    adapters = as_list(gpus)
    if not adapters:
        return None
    dedicated = [g for g in adapters if re.search(r"\b(NVIDIA|GEFORCE)\b", str(g.get("Name", "")), re.I)]
    return max(dedicated or adapters, key=lambda g: number(g.get("AdapterRAM")))


def system_error_count(events):
    return sum(int(number(event.get("Count"))) for event in as_list(events))


def system_error_sources(events):
    labels = {
        "WHEA-Logger": "hardware (WHEA)", "Disk": "disk", "stornvme": "NVMe storage",
        "storahci": "storage controller", "Ntfs": "file system", "Display": "graphics driver",
        "nvlddmkm": "NVIDIA graphics driver", "amdkmdag": "AMD graphics driver",
    }
    return ", ".join(labels.get(str(event.get("Provider")), str(event.get("Provider"))) for event in as_list(events))


def quick_check_findings(data):
    findings = []
    battery = battery_health_percent(data.get("designCapacity"), data.get("fullCapacity"))
    if battery is not None and battery < 80:
        severity = "review" if battery >= 60 else "attention"
        note = "Battery capacity shows wear" if battery >= 60 else "Battery capacity is substantially reduced"
        findings.append((severity, f"{note} ({battery:.1f}% estimated remaining). Check expected runtime and replacement cost."))

    for disk in as_list(data.get("disks")):
        status = disk_status(disk)
        if status in {"Warning", "Unhealthy"}:
            findings.append(("attention", f"Windows reports {status.lower()} status for {disk.get('FriendlyName') or 'a drive'}. Back up important files and investigate."))

    for volume in as_list(data.get("volumes")):
        stats = storage_usage(volume.get("Size"), volume.get("FreeSpace"))
        if stats and stats["free"] / stats["total"] <= 0.10:
            drive = volume.get("DeviceID") or "A drive"
            findings.append(("review", f"{drive} has only {gigabytes(stats['free'])} free ({100 - stats['usedPercent']:.0f}% available)."))

    events = as_list(data.get("systemErrors"))
    count = system_error_count(events)
    if count:
        findings.append(("review", f"Windows logged {count} hardware, storage, or graphics-driver error(s) in the last 14 days. Review the event details; an event alone does not prove a part is failing."))
    return findings


def quick_check_missing(data):
    missing = []
    if battery_health_percent(data.get("designCapacity"), data.get("fullCapacity")) is None:
        missing.append("battery capacity")
    if not data.get("cpu"):
        missing.append("processor identification")
    if not as_list(data.get("gpus")):
        missing.append("graphics identification")
    if number(data.get("memoryTotalKB")) <= 0:
        missing.append("memory capacity")
    if not any(storage_usage(v.get("Size"), v.get("FreeSpace")) for v in as_list(data.get("volumes"))):
        missing.append("storage space")
    if not any(disk_status(d) != "Not reported" for d in as_list(data.get("disks"))):
        missing.append("drive health status")
    if not data.get("systemErrorsAvailable", False):
        missing.append("recent system events")
    return missing


def quick_check_summary(data):
    findings = quick_check_findings(data)
    if any(level == "attention" for level, _ in findings):
        return "REVIEW BEFORE BUYING", RED, len(findings)
    if findings:
        return "CHECK THESE ITEMS", AMBER, len(findings)
    if quick_check_missing(data):
        return "SOME CHECKS UNAVAILABLE", AMBER, 0
    return "NO HIGH-PRIORITY FLAGS", GREEN, 0


def set_windows_dpi_awareness():
    if platform.system() != "Windows":
        return
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


class SpeCheck(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("SpeCheck | Quick Device Check")
        self._set_window_icon()
        self.geometry("1280x820")
        self.minsize(1024, 640)
        self.configure(bg=BG)
        self.data = None
        self.scan_running = False
        self.current_page = "overview"
        self.nav_buttons = {}
        self._build()
        if platform.system() == "Windows":
            self.after(100, self._maximize_window)
        self.after(250, self.scan)

    def _set_window_icon(self):
        assets = Path(__file__).parent / "assets"
        png_path = assets / "specheck-logo.png"
        ico_path = assets / "specheck-logo.ico"
        try:
            self.app_icon_image = tk.PhotoImage(file=str(png_path))
            self.iconphoto(True, self.app_icon_image)
        except tk.TclError:
            self.app_icon_image = None
        if platform.system() == "Windows" and ico_path.is_file():
            try:
                self.iconbitmap(default=str(ico_path))
            except tk.TclError:
                pass

    def _maximize_window(self):
        try:
            self.state("zoomed")
        except tk.TclError:
            pass

    def _build(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self.sidebar = tk.Frame(self, bg=SIDEBAR, width=220, highlightbackground=BORDER, highlightthickness=1)
        self.sidebar.grid(row=0, column=0, sticky="ns")
        self.sidebar.grid_propagate(False)
        self.main = tk.Frame(self, bg=BG)
        self.main.grid(row=0, column=1, sticky="nsew")
        self.main.grid_columnconfigure(0, weight=1)
        self.main.grid_rowconfigure(1, weight=1)
        self._sidebar()
        self._header()
        self.page_host = tk.Frame(self.main, bg=BG)
        self.page_host.grid(row=1, column=0, sticky="nsew", padx=34, pady=(6, 10))
        self.page_host.grid_columnconfigure(0, weight=1)
        self.page_host.grid_rowconfigure(0, weight=1)
        self._build_overview()
        self._build_components()
        self._build_report()
        self._footer()
        self.show_page("overview")

    def _sidebar(self):
        brand = tk.Frame(self.sidebar, bg=SIDEBAR)
        brand.pack(anchor="w", padx=20, pady=(25, 0))
        if self.app_icon_image:
            tk.Label(brand, image=self.app_icon_image, bg=SIDEBAR).pack(side="left", padx=(0, 11))
        brand_text = tk.Frame(brand, bg=SIDEBAR)
        brand_text.pack(side="left")
        tk.Label(brand_text, text="SpeCheck", fg=TEXT, bg=SIDEBAR, font=("Segoe UI", 15, "bold")).pack(anchor="w")
        tk.Label(brand_text, text="DEVICE CHECK", fg=MUTED, bg=SIDEBAR, font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(1, 0))
        tk.Frame(self.sidebar, bg=BORDER, height=1).pack(fill="x", padx=20, pady=24)
        for key, label in [("overview", "Quick check"), ("components", "Components"), ("report", "Check details")]:
            item = tk.Frame(self.sidebar, bg=SIDEBAR, height=58, cursor="hand2")
            item.pack(fill="x", padx=12, pady=3)
            item.pack_propagate(False)
            icon = tk.Canvas(item, width=34, height=34, bg=SIDEBAR, highlightthickness=0)
            icon.place(x=13, rely=0.5, anchor="w")
            self._draw_nav_icon(icon, key, MUTED)
            title = tk.Label(item, text=label, fg=MUTED, bg=SIDEBAR, font=("Segoe UI", 11), anchor="w", cursor="hand2")
            title.place(x=61, rely=0.5, anchor="w")
            for widget in (item, icon, title):
                widget.bind("<Button-1>", lambda _event, page=key: self.show_page(page))
                widget.bind("<Enter>", lambda _event, frame=item: frame.configure(bg=PANEL_2))
                widget.bind("<Leave>", lambda _event, page=key: self._refresh_nav_item(page))
            self.nav_buttons[key] = (item, icon, title)
        tk.Frame(self.sidebar, bg=BORDER, height=1).pack(fill="x", padx=20, pady=22)
        tk.Label(self.sidebar, text="PRE-PURCHASE CHECK", fg=MUTED, bg=SIDEBAR, font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=24, pady=(0, 8))
        tk.Label(self.sidebar, text="Hardware data stays\non this computer.", justify="left", fg=MUTED, bg=SIDEBAR, font=("Segoe UI", 9), pady=3).pack(anchor="w", padx=24)
        tk.Label(self.sidebar, text="WINDOWS 10 / 11", fg=MUTED, bg=SIDEBAR, font=("Segoe UI", 9, "bold")).pack(side="bottom", anchor="w", padx=23, pady=20)

    def _draw_nav_icon(self, canvas, key, color):
        canvas.delete("all")
        if key == "overview":
            for x, y in ((5, 5), (18, 5), (5, 18), (18, 18)):
                canvas.create_rectangle(x, y, x + 9, y + 9, outline=color, width=2)
        elif key == "components":
            canvas.create_rectangle(10, 10, 24, 24, outline=color, width=2)
            for offset in (0, 6, 12):
                canvas.create_line(5 + offset, 7, 5 + offset, 10, fill=color, width=2)
                canvas.create_line(5 + offset, 24, 5 + offset, 27, fill=color, width=2)
                canvas.create_line(7, 5 + offset, 10, 5 + offset, fill=color, width=2)
                canvas.create_line(24, 5 + offset, 27, 5 + offset, fill=color, width=2)
            canvas.create_rectangle(15, 15, 19, 19, fill=color, outline=color)
        else:
            canvas.create_rectangle(8, 4, 25, 29, outline=color, width=2)
            canvas.create_line(12, 11, 21, 11, fill=color, width=2)
            canvas.create_line(12, 16, 21, 16, fill=color, width=2)
            canvas.create_line(12, 21, 16, 21, fill=color, width=2)
            canvas.create_line(17, 23, 19, 25, 23, 20, fill=color, width=2, smooth=True)

    def _refresh_nav_item(self, key):
        item, icon, title = self.nav_buttons[key]
        active = key == self.current_page
        background = PANEL_2 if active else SIDEBAR
        item.configure(bg=background)
        icon.configure(bg=background)
        title.configure(bg=background, fg=TEXT if active else MUTED, font=("Segoe UI", 11, "bold" if active else "normal"))
        self._draw_nav_icon(icon, key, CYAN if active else MUTED)

    def _header(self):
        top = tk.Frame(self.main, bg=BG)
        top.grid(row=0, column=0, sticky="ew", padx=34, pady=(26, 16))
        top.grid_columnconfigure(0, weight=1)
        title_box = tk.Frame(top, bg=BG)
        title_box.grid(row=0, column=0, sticky="w")
        self.page_title = tk.Label(title_box, text="Quick device check", fg=TEXT, bg=BG, font=("Segoe UI", 24, "bold"))
        self.page_title.pack(anchor="w")
        self.page_subtitle = tk.Label(title_box, text="Fast, local readings to help you review a laptop before buying.", fg=MUTED, bg=BG, font=("Segoe UI", 11))
        self.page_subtitle.pack(anchor="w", pady=(3, 0))
        self.export_btn = tk.Button(top, text="Export report", command=self.export_report, bg=PANEL, fg=TEXT, activebackground=PANEL_2, activeforeground=TEXT, relief="flat", bd=0, highlightbackground=BORDER, highlightthickness=1, padx=16, pady=12, font=("Segoe UI", 10, "bold"), cursor="hand2")
        self.export_btn.grid(row=0, column=1, sticky="e", padx=(10, 8))
        self.scan_btn = tk.Button(top, text="↻   Run quick check", command=self.scan, bg=CYAN, fg="#FFFFFF", activebackground="#0A6073", activeforeground="#FFFFFF", relief="flat", bd=0, padx=18, pady=12, font=("Segoe UI", 10, "bold"), cursor="hand2")
        self.scan_btn.grid(row=0, column=2, sticky="e")

    def _build_overview(self):
        page = tk.Frame(self.page_host, bg=BG)
        page.grid(row=0, column=0, sticky="nsew")
        for column in (0, 1):
            page.grid_columnconfigure(column, weight=1, uniform="overview")
        for row in (1, 2, 3):
            page.grid_rowconfigure(row, weight=1, uniform="overview_rows")
        self.pages = {"overview": page}

        self.device_banner = tk.Frame(page, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        self.device_banner.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 16))
        self.device_banner.grid_columnconfigure(1, weight=1)
        self.device_logo = tk.Label(self.device_banner, text="DEVICE", fg=CYAN, bg=PANEL, font=("Segoe UI", 11, "bold"), width=12)
        self.device_logo.grid(row=0, column=0, rowspan=2, padx=(18, 12), pady=16)
        self.device_name = tk.Label(self.device_banner, text="Scanning this computer…", fg=TEXT, bg=PANEL, font=("Segoe UI", 15, "bold"), anchor="w")
        self.device_name.grid(row=0, column=1, sticky="sw", pady=(16, 1))
        self.device_sub = tk.Label(self.device_banner, text="Reading locally reported device details", fg=MUTED, bg=PANEL, font=("Segoe UI", 10), anchor="w")
        self.device_sub.grid(row=1, column=1, sticky="nw", pady=(0, 16))
        self.status = tk.Label(self.device_banner, text="●  SCANNING", fg=CYAN, bg=INFO_BG, padx=13, pady=9, font=("Segoe UI", 9, "bold"))
        self.status.grid(row=0, column=2, rowspan=2, sticky="e", padx=18)

        specs = [
            ("battery", "BATTERY HEALTH", CYAN), ("cpu", "PROCESSOR", BLUE),
            ("memory", "MEMORY", GREEN), ("gpu", "GRAPHICS", AMBER),
            ("storage", "STORAGE", BLUE), ("events", "RECENT SYSTEM ERRORS", AMBER),
        ]
        self.cards = {}
        self.gauges = {}
        for i, (key, label, color) in enumerate(specs):
            card = tk.Frame(page, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
            card.grid(row=1 + i // 2, column=i % 2, sticky="nsew", padx=(0, 8) if i % 2 == 0 else (8, 0), pady=(0, 15))
            head = tk.Frame(card, bg=PANEL)
            head.pack(fill="x", padx=20, pady=(15, 6))
            tk.Label(head, text=label, fg=MUTED, bg=PANEL, font=("Segoe UI", 9, "bold")).pack(side="left")
            body = tk.Frame(card, bg=PANEL)
            body.pack(fill="both", expand=True, padx=19, pady=(3, 15))
            if key in {"battery", "memory", "storage"}:
                gauge = tk.Canvas(body, width=88, height=88, bg=PANEL, highlightthickness=0)
                gauge.pack(side="left", padx=(0, 18), pady=3)
                self.gauges[key] = (gauge, color)
                text_box = tk.Frame(body, bg=PANEL)
                text_box.pack(side="left", fill="both", expand=True)
            else:
                text_box = body
            value = tk.Label(text_box, text="Waiting for scan", fg=TEXT, bg=PANEL, font=("Segoe UI", 15, "bold"), anchor="w", justify="left", wraplength=480)
            value.pack(fill="x", pady=(7, 2))
            detail = tk.Label(text_box, text="—", fg=MUTED, bg=PANEL, font=("Segoe UI", 10), anchor="w", justify="left", wraplength=480)
            detail.pack(fill="x", pady=(2, 7))
            self.cards[key] = (value, detail)
            text_box.bind("<Configure>", lambda event, val=value, det=detail: self._resize_card_text(event, val, det))

    def _resize_card_text(self, event, value_label, detail_label):
        width = max(240, event.width - 4)
        value_label.configure(wraplength=width)
        detail_label.configure(wraplength=width)

    def _build_components(self):
        page = tk.Frame(self.page_host, bg=BG)
        page.grid(row=0, column=0, sticky="nsew")
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(1, weight=1)
        self.pages["components"] = page
        tk.Label(page, text="DETECTED COMPONENTS", fg=MUTED, bg=BG, font=("Segoe UI", 9, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 11))
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Core.Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT, rowheight=42, borderwidth=0, font=("Segoe UI", 10))
        style.configure("Core.Treeview.Heading", background=PANEL_2, foreground=TEXT, relief="flat", font=("Segoe UI", 9, "bold"))
        style.map("Core.Treeview", background=[("selected", "#DCE8F1")], foreground=[("selected", TEXT)])
        table_frame = tk.Frame(page, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        table_frame.grid(row=1, column=0, sticky="nsew")
        table_frame.grid_columnconfigure(0, weight=1)
        table_frame.grid_rowconfigure(0, weight=1)
        cols = ("component", "reading", "notes")
        self.component_table = ttk.Treeview(table_frame, columns=cols, show="headings", style="Core.Treeview")
        self.component_table.heading("component", text="COMPONENT")
        self.component_table.heading("reading", text="READING")
        self.component_table.heading("notes", text="WHAT THIS TELLS YOU")
        self.component_table.column("component", width=210, minwidth=130, stretch=False)
        self.component_table.column("reading", width=480, minwidth=200, stretch=True)
        self.component_table.column("notes", width=760, minwidth=200, stretch=True)
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.component_table.yview)
        self.component_table.configure(yscrollcommand=scroll.set)
        self.component_table.grid(row=0, column=0, sticky="nsew", padx=1, pady=1)
        self.component_table.bind("<Configure>", self._resize_component_columns)
        scroll.grid(row=0, column=1, sticky="ns")
        actions = tk.Frame(page, bg=BG)
        actions.grid(row=2, column=0, sticky="w", pady=(12, 0))
        tk.Button(actions, text="Run Windows Memory Diagnostic…", command=self.run_memory_diagnostic, bg=PANEL, fg=TEXT, relief="flat", bd=0, highlightbackground=BORDER, highlightthickness=1, padx=15, pady=10, font=("Segoe UI", 10, "bold"), cursor="hand2").pack(side="left")
        tk.Label(actions, text="The diagnostic may ask to restart Windows.", fg=MUTED, bg=BG, font=("Segoe UI", 10)).pack(side="left", padx=14)

    def _resize_component_columns(self, event):
        width = max(540, event.width - 4)
        component = max(130, min(230, int(width * 0.15)))
        reading = max(200, int(width * 0.36))
        notes = max(200, width - component - reading)
        self.component_table.column("component", width=component)
        self.component_table.column("reading", width=reading)
        self.component_table.column("notes", width=notes)

    def _build_report(self):
        page = tk.Frame(self.page_host, bg=BG)
        page.grid(row=0, column=0, sticky="nsew")
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(1, weight=1)
        self.pages["report"] = page
        tk.Label(page, text="WHAT THE SCAN CAN CONFIRM", fg=MUTED, bg=BG, font=("Segoe UI", 9, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 11))
        self.report_panel = tk.Frame(page, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        self.report_panel.grid(row=1, column=0, sticky="nsew")
        self.report_panel.grid_columnconfigure(0, weight=1)
        self.report_panel.grid_rowconfigure(0, weight=1)
        self.report_canvas = tk.Canvas(self.report_panel, bg=PANEL, highlightthickness=0)
        self.report_canvas.grid(row=0, column=0, sticky="nsew")
        report_scroll = ttk.Scrollbar(self.report_panel, orient="vertical", command=self.report_canvas.yview)
        report_scroll.grid(row=0, column=1, sticky="ns")
        self.report_canvas.configure(yscrollcommand=report_scroll.set)
        self.report_content = tk.Frame(self.report_canvas, bg=PANEL)
        self.report_window = self.report_canvas.create_window((0, 0), window=self.report_content, anchor="nw")
        self.report_content.bind("<Configure>", lambda _event: self.report_canvas.configure(scrollregion=self.report_canvas.bbox("all")), add="+")
        self.report_canvas.bind("<Configure>", lambda event: self.report_canvas.itemconfigure(self.report_window, width=event.width))
        self.report_content.grid_columnconfigure(0, minsize=190, weight=0)
        self.report_content.grid_columnconfigure(1, weight=1)
        self.report_wrap_labels = []
        tk.Label(self.report_content, text="Run a scan to build a report.", fg=TEXT, bg=PANEL, font=("Segoe UI", 11)).grid(row=0, column=0, sticky="w", padx=24, pady=18)
        actions = tk.Frame(page, bg=BG)
        actions.grid(row=2, column=0, sticky="w", pady=(12, 0))
        tk.Button(actions, text="Open Windows battery report", command=self.open_battery_report, bg=PANEL, fg=TEXT, relief="flat", bd=0, highlightbackground=BORDER, highlightthickness=1, padx=14, pady=10, font=("Segoe UI", 9, "bold"), cursor="hand2").pack(side="left")
        tk.Button(actions, text="Open Reliability Monitor", command=self.open_reliability_monitor, bg=PANEL, fg=TEXT, relief="flat", bd=0, highlightbackground=BORDER, highlightthickness=1, padx=14, pady=10, font=("Segoe UI", 9, "bold"), cursor="hand2").pack(side="left", padx=(9, 0))
        tk.Button(actions, text="Export scan as JSON", command=self.export_report, bg=PANEL, fg=TEXT, relief="flat", bd=0, highlightbackground=BORDER, highlightthickness=1, padx=14, pady=10, font=("Segoe UI", 9, "bold"), cursor="hand2").pack(side="left", padx=(9, 0))

    def _footer(self):
        self.footer = tk.Label(self.main, text="Ready to scan", fg=MUTED, bg=BG, font=("Segoe UI", 9), anchor="w")
        self.footer.grid(row=2, column=0, sticky="ew", padx=34, pady=(0, 12))

    def show_page(self, page):
        if page not in self.pages:
            return
        self.current_page = page
        for key, frame in self.pages.items():
            if key == page:
                frame.tkraise()
            else:
                frame.lower()
        for key in self.nav_buttons:
            self._refresh_nav_item(key)
        titles = {
            "overview": ("Quick device check", "Fast readings to help review a laptop before buying."),
            "components": ("Components", "Detected hardware, current readings, and safe diagnostic tools."),
            "report": ("Check details", "What Windows reported, what needs a closer look, and what was not tested."),
        }
        title, subtitle = titles[page]
        self.page_title.configure(text=title)
        self.page_subtitle.configure(text=subtitle)

    def scan(self):
        if self.scan_running:
            return
        self.scan_running = True
        self.scan_btn.configure(state="disabled", text="Checking…")
        self.status.configure(text="●  SCANNING", fg=CYAN, bg=INFO_BG)
        self.footer.configure(text="Running one quick, local hardware check…")
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self):
        try:
            data = powershell_scan()
            self.after(0, lambda: self._show_data(data))
        except Exception as exc:
            self.after(0, lambda e=str(exc): self._scan_error(e))

    def _scan_error(self, error):
        self.scan_running = False
        self.scan_btn.configure(state="normal", text="↻   Run quick check")
        if self.data:
            self.status.configure(text="●  CHECK FAILED", fg=AMBER, bg=WARNING_BG)
            self.footer.configure(text=f"Showing the last successful results. {error}")
        else:
            self.status.configure(text="●  SCAN FAILED", fg=AMBER, bg=WARNING_BG)
            self.device_name.configure(text="Couldn’t complete the scan")
            self.device_sub.configure(text="Check the Windows requirements and try again.")
            self.footer.configure(text=error)
            for value, detail in self.cards.values():
                value.configure(text="Unavailable")
                detail.configure(text="No reading was returned.")
            self._render_components(None)
            self._render_report(None)

    def _show_data(self, data):
        self.data = data
        self.scan_running = False
        self.scan_btn.configure(state="normal", text="↻   Run again")
        machine = " ".join(str(x) for x in [data.get("manufacturer"), data.get("model")] if x and str(x).strip()) or data.get("computer", "This computer")
        self._set_device_logo(data.get("manufacturer"), data.get("model"))
        self.device_name.configure(text=machine)
        self.device_sub.configure(text=f"{data.get('os', 'Windows')}  ·  {data.get('computer', '')}")
        summary, color, issue_count = quick_check_summary(data)
        status_bg = SUCCESS_BG if color == GREEN else (DANGER_BG if color == RED else WARNING_BG)
        self.status.configure(text=f"●  {summary}", fg=color, bg=status_bg)
        self.footer.configure(text=f"Quick check completed {datetime.now().strftime('%b %d, %Y at %I:%M:%S %p')} · {issue_count} item(s) flagged · readings stay on this computer")
        self._render_overview(data)
        self._render_components(data)
        self._render_report(data)

    def _set_device_logo(self, manufacturer, model=""):
        key = device_logo_key(manufacturer, model)
        logo_path = Path(__file__).parent / "assets" / "device-logos" / f"{key}.png" if key else None
        if logo_path and logo_path.is_file():
            try:
                self.device_logo_img = tk.PhotoImage(file=str(logo_path))
                self.device_logo.configure(image=self.device_logo_img, text="", width=0, height=0, padx=0, pady=0)
                return
            except tk.TclError:
                pass
        self.device_logo_img = None
        self.device_logo.configure(image="", text="DEVICE", width=12, height=2)

    def _render_overview(self, data):
        health = battery_health_percent(data.get("designCapacity"), data.get("fullCapacity"))
        battery_detail = "Estimated from full-charge capacity vs. design capacity" if health is not None else "Windows did not return both capacity readings"
        battery_detail += f"\nCycle count: {battery_cycle_count_text(data)}"
        self._card("battery", "Battery health", battery_detail, health)

        cpu = data.get("cpu") or {}
        if isinstance(cpu, list):
            cpu = cpu[0] if cpu else {}
        cpu_name = cpu.get("Name") or "CPU not reported"
        cpu_line = f"{cpu.get('NumberOfCores', '—')} cores · {cpu.get('NumberOfLogicalProcessors', '—')} logical processors"
        max_clock = number(cpu.get("MaxClockSpeed"))
        if max_clock > 0:
            cpu_line += f" · {max_clock / 1000:.2f} GHz reported max"
        self._card("cpu", cpu_name, cpu_line)

        total = number(data.get("memoryTotalKB"))
        free = number(data.get("memoryFreeKB"))
        if total > 0:
            used = max(0, total - free)
            pct = used / total * 100
            self._card("memory", f"{memory_capacity_text(total)} total", f"{memory_capacity_text(used)} used · {memory_capacity_text(free)} available\n{memory_module_summary(data.get('memoryModules'))}", pct)
        else:
            self._card("memory", "Not reported", "Windows did not return memory capacity.")

        gpus = as_list(data.get("gpus"))
        if gpus:
            gpu = select_primary_gpu(gpus)
            info = f"{len(gpus)} adapter(s) detected"
            vram = reported_vram_text(gpu.get("AdapterRAM"))
            if vram:
                info += f" · {vram}"
            other = [str(item.get("Name")) for item in gpus if item is not gpu and item.get("Name")]
            if other:
                info += "\nOther: " + ", ".join(other[:2])
            self._card("gpu", gpu.get("Name") or "Graphics adapter", info)
        else:
            self._card("gpu", "Not reported", "Windows did not return a graphics adapter.")

        volumes = as_list(data.get("volumes"))
        disks = as_list(data.get("disks"))
        if volumes:
            volume = volumes[0]
            stats = storage_usage(volume.get("Size"), volume.get("FreeSpace"))
            if stats:
                drive = volume.get("DeviceID") or "Local drive"
                detail = f"of {gigabytes(stats['total'])} total · {stats['usedPercent']:.0f}% used"
                if len(disks) == 1:
                    detail += "\n" + disk_spec_text(disks[0])
                elif disks:
                    detail += f"\n{len(disks)} physical drives · see Components for models and health"
                else:
                    detail += "\nDrive type, model, and health not reported"
                if len(volumes) > 1:
                    extra = [f"{v.get('DeviceID')}: {gigabytes(number(v.get('FreeSpace')))} free of {gigabytes(number(v.get('Size')))}" for v in volumes[1:3]]
                    detail += "\n" + " · ".join(extra)
                self._card("storage", f"{drive} · {gigabytes(stats['free'])} free", detail, stats["usedPercent"])
            else:
                self._card("storage", "Space not reported", "Windows returned invalid total or free-space values.")
        else:
            disk_detail = disk_spec_text(disks[0]) if len(disks) == 1 else (f"{len(disks)} physical drives detected" if disks else "Windows did not report physical-drive status.")
            self._card("storage", "Space not reported", disk_detail)

        event_count = system_error_count(data.get("systemErrors"))
        if not data.get("systemErrorsAvailable", False):
            self._card("events", "Not available", "Windows System event log could not be checked.")
        elif event_count:
            sources = system_error_sources(data.get("systemErrors"))
            self._card("events", f"{event_count} error(s) logged", f"Last 14 days · {sources or 'hardware-related providers'} · review event details")
        else:
            self._card("events", "No matching errors", "No recent critical/error events found for selected hardware providers.")

    def _card(self, key, value, detail, percent=None):
        val, det = self.cards[key]
        val.configure(text=str(value), font=("Segoe UI", 15 if len(str(value)) > 34 else 16, "bold"))
        det.configure(text=str(detail))
        gauge = self.gauges.get(key)
        if gauge:
            canvas, color = gauge
            canvas.delete("all")
            canvas.create_oval(8, 8, 80, 80, outline=TRACK, width=8)
            if percent is not None:
                bounded = max(0, min(100, number(percent)))
                if bounded > 0:
                    canvas.create_arc(8, 8, 80, 80, start=90, extent=-359.9 * bounded / 100, style=tk.ARC, outline=color, width=8)
                center = f"{bounded:.1f}%" if key == "battery" else f"{bounded:.0f}%"
                canvas.create_text(44, 44, text=center, fill=TEXT, font=("Segoe UI", 10, "bold"))
            else:
                canvas.create_text(44, 44, text="—", fill=MUTED, font=("Segoe UI", 13, "bold"))

    def _render_components(self, data):
        self.component_table.delete(*self.component_table.get_children())
        if not data:
            self.component_table.insert("", "end", values=("Scan", "Unavailable", "No component data was returned."))
            return
        cpu = data.get("cpu") or {}
        if isinstance(cpu, list):
            cpu = cpu[0] if cpu else {}
        cpu_specs = f"{cpu.get('NumberOfCores', '—')} cores · {cpu.get('NumberOfLogicalProcessors', '—')} logical processors"
        max_clock = number(cpu.get("MaxClockSpeed"))
        if max_clock > 0:
            cpu_specs += f" · {max_clock / 1000:.2f} GHz reported max"
        self.component_table.insert("", "end", values=("Processor", cpu.get("Name") or "Not reported", f"{cpu_specs}. Identification only; no performance test run."))
        total = number(data.get("memoryTotalKB"))
        free = number(data.get("memoryFreeKB"))
        mem = f"{memory_capacity_text(total)} installed; {memory_capacity_text(max(0, total-free))} in use" if total > 0 else "Not reported"
        self.component_table.insert("", "end", values=("Memory", mem, memory_module_summary(data.get("memoryModules"))))
        for index, module in enumerate(as_list(data.get("memoryModules")), start=1):
            self.component_table.insert("", "end", values=(f"RAM module {index}", memory_module_detail(module, index), "Module identification and configured speed reported by Windows."))
        for gpu in as_list(data.get("gpus")):
            read = gpu.get("Name") or "Graphics adapter"
            vram = reported_vram_text(gpu.get("AdapterRAM"))
            if vram:
                read += f" · {vram}"
            self.component_table.insert("", "end", values=("Graphics", read, "Adapter identification only; Windows did not run a GPU health test."))
        health = battery_health_percent(data.get("designCapacity"), data.get("fullCapacity"))
        battery_read = f"{health:.1f}% estimated health" if health is not None else "Health not reported"
        source = data.get("batteryCapacitySource") or "Windows battery information"
        self.component_table.insert("", "end", values=("Battery", battery_read, f"Cycle count: {battery_cycle_count_text(data)} · Capacity estimate from full-charge vs. design capacity ({source})."))
        volumes = as_list(data.get("volumes"))
        for volume in volumes:
            stats = storage_usage(volume.get("Size"), volume.get("FreeSpace"))
            if stats:
                drive = volume.get("DeviceID") or "Local volume"
                read = f"{gigabytes(stats['free'])} free of {gigabytes(stats['total'])} total"
                note = f"{stats['usedPercent']:.1f}% used · filesystem space reported by Windows"
                self.component_table.insert("", "end", values=(f"Volume {drive}", read, note))
        disks = as_list(data.get("disks"))
        if disks:
            for disk in disks:
                bus = str(disk.get("BusType") or "").strip()
                bus_note = f" · {bus}" if bus and bus.casefold() not in {"unknown", "unspecified"} else ""
                self.component_table.insert("", "end", values=("Physical drive", f"{disk_type_text(disk)} · {disk_model_text(disk)} · {gigabytes(disk.get('Size'))}", f"Windows health: {disk_status(disk)}{bus_note} · Operational state: {disk.get('OperationalStatus') or 'not reported'}."))
        elif not volumes:
            self.component_table.insert("", "end", values=("Storage", "Health not reported", "The Windows storage provider did not expose drive health."))
        event_count = system_error_count(data.get("systemErrors"))
        if data.get("systemErrorsAvailable", False):
            event_read = f"{event_count} error(s) in last 14 days" if event_count else "No matching errors found"
            event_detail = f"Providers: {system_error_sources(data.get('systemErrors')) or 'none'}; counts are clues to inspect, not proof of hardware failure."
        else:
            event_read = "Not available"
            event_detail = "Windows could not read the System event log."
        self.component_table.insert("", "end", values=("Recent system errors", event_read, event_detail))

    def _render_report(self, data):
        for widget in self.report_content.winfo_children():
            widget.destroy()
        self.report_wrap_labels = []
        self.report_footer_label = None
        if not data:
            tk.Label(self.report_content, text="No scan results are available. Choose Scan again.", fg=TEXT, bg=PANEL, font=("Segoe UI", 11)).grid(row=0, column=0, sticky="w")
            return
        battery = battery_health_percent(data.get("designCapacity"), data.get("fullCapacity"))
        if battery is None:
            battery_read = "Not reported"
            battery_detail = "Windows did not provide both design and full-charge capacity values."
        else:
            battery_read = f"{battery:.1f}% estimated remaining capacity"
            if battery < 60:
                battery_detail = "Substantial capacity loss. Check expected runtime and possible replacement cost."
            elif battery < 80:
                battery_detail = "Noticeable capacity wear. Ask about battery runtime."
            else:
                battery_detail = "At least 80% of design capacity by this estimate; actual runtime varies."

        disks = as_list(data.get("disks"))
        disk_findings = [disk_status(d) for d in disks]
        volumes = as_list(data.get("volumes"))
        if not disk_findings:
            disk_label = "Not reported"
        elif any(s == "Unhealthy" for s in disk_findings):
            disk_label = "Windows reports an unhealthy drive"
        elif any(s == "Warning" for s in disk_findings):
            disk_label = "Windows reports a drive warning"
        elif all(s == "Healthy" for s in disk_findings):
            disk_label = "Windows reports drives healthy"
        else:
            disk_label = "Some drive health values are unavailable"
        volume_space = [
            f"{v.get('DeviceID') or 'Volume'}: {gigabytes(storage_usage(v.get('Size'), v.get('FreeSpace'))['free'])} free of {gigabytes(storage_usage(v.get('Size'), v.get('FreeSpace'))['total'])}"
            for v in volumes if storage_usage(v.get("Size"), v.get("FreeSpace"))
        ]
        storage_detail = "Windows drive-health status where available."
        if volume_space:
            storage_detail = "Available space: " + " · ".join(volume_space) + ". "
        if disks:
            storage_detail += "Physical drives: " + " · ".join(disk_spec_text(disk) for disk in disks) + "."
        else:
            storage_detail += "Physical drive model, media type, and health were not reported."

        cpu = data.get("cpu") or {}
        if isinstance(cpu, list):
            cpu = cpu[0] if cpu else {}
        cpu_read = cpu.get("Name") or "Not reported"
        cpu_detail = f"{cpu.get('NumberOfCores', '—')} cores · {cpu.get('NumberOfLogicalProcessors', '—')} logical processors"
        max_clock = number(cpu.get("MaxClockSpeed"))
        if max_clock > 0:
            cpu_detail += f" · {max_clock / 1000:.2f} GHz reported max"
        cpu_detail += ". Identification only; no performance test was run."

        gpus = as_list(data.get("gpus"))
        primary_gpu = select_primary_gpu(gpus)
        gpu_read = (primary_gpu.get("Name") or "Graphics adapter") if primary_gpu else "Not reported"
        if primary_gpu:
            vram = reported_vram_text(primary_gpu.get("AdapterRAM"))
            if vram:
                gpu_read += f" · {vram}"
        gpu_detail = f"{len(gpus)} adapter(s) detected. Identification only; no game, benchmark, or stability test was run."

        total = number(data.get("memoryTotalKB"))
        free = number(data.get("memoryFreeKB"))
        memory_read = f"{memory_capacity_text(total)} installed · {memory_capacity_text(max(0, total-free))} in use" if total > 0 else "Not reported"
        module_details = "; ".join(memory_module_detail(module, index) for index, module in enumerate(as_list(data.get("memoryModules")), start=1))
        memory_detail = module_details or "RAM module details and speed were not reported."
        memory_detail += " Windows Memory Diagnostic is available on Components; this quick check does not test RAM for faults."
        module_count = len(as_list(data.get("memoryModules")))
        if module_count:
            memory_read += f" · {module_count} module(s)"

        battery_detail += f" Cycle count: {battery_cycle_count_text(data)}."

        event_count = system_error_count(data.get("systemErrors"))
        if data.get("systemErrorsAvailable", False):
            event_read = f"{event_count} matching event(s) in 14 days" if event_count else "No matching critical/error events found"
            event_detail = f"Sources: {system_error_sources(data.get('systemErrors')) or 'none'}. Review dates and details; logged events can have different causes."
        else:
            event_read = "Not available"
            event_detail = "Windows could not read the System event log."

        rows = [
            ("Battery", battery_read, battery_detail),
            ("Storage", disk_label, storage_detail),
            ("Processor", cpu_read, cpu_detail),
            ("Graphics", gpu_read, gpu_detail),
            ("Memory", memory_read, memory_detail),
            ("System errors", event_read, event_detail),
        ]
        tk.Label(self.report_content, text="CHECK", fg=MUTED, bg=PANEL, font=("Segoe UI", 9, "bold")).grid(row=0, column=0, sticky="w", padx=(24, 18), pady=(20, 11))
        tk.Label(self.report_content, text="RESULT AND DETAILS", fg=MUTED, bg=PANEL, font=("Segoe UI", 9, "bold")).grid(row=0, column=1, sticky="w", padx=(0, 24), pady=(20, 11))
        for i, (name, result, detail) in enumerate(rows, start=1):
            tk.Label(self.report_content, text=name, fg=CYAN, bg=PANEL, font=("Segoe UI", 10, "bold"), width=18, anchor="w").grid(row=i, column=0, sticky="w", padx=(24, 18), pady=12)
            cell = tk.Frame(self.report_content, bg=PANEL)
            cell.grid(row=i, column=1, sticky="ew", padx=(0, 24), pady=8)
            cell.grid_columnconfigure(0, weight=1)
            tk.Label(cell, text=result, fg=TEXT, bg=PANEL, font=("Segoe UI", 10, "bold"), anchor="w").grid(row=0, column=0, sticky="w")
            detail_label = tk.Label(cell, text=detail, fg=MUTED, bg=PANEL, font=("Segoe UI", 9), anchor="w", justify="left", wraplength=920)
            detail_label.grid(row=1, column=0, sticky="ew", pady=(3, 0))
            self.report_wrap_labels.append(detail_label)
        self.report_footer_label = tk.Label(self.report_content, text="Before buying, also check the screen, keyboard, ports, hinges, charger, Wi-Fi, and audio. Try a game or benchmark if possible. This quick check is not a full hardware test or certification.", fg=MUTED, bg=PANEL, font=("Segoe UI", 9), justify="left", wraplength=1100)
        self.report_footer_label.grid(row=len(rows)+1, column=0, columnspan=2, sticky="ew", padx=24, pady=(18, 20))
        self.report_content.bind("<Configure>", self._resize_report_text, add="+")
        self.report_canvas.after_idle(lambda: self._resize_report_text(None))

    def _resize_report_text(self, event):
        width = event.width if event else self.report_content.winfo_width()
        detail_width = max(320, width - 250)
        for label in self.report_wrap_labels:
            if label.winfo_exists():
                label.configure(wraplength=detail_width)
        footer = getattr(self, "report_footer_label", None)
        if footer is not None and footer.winfo_exists():
            footer.configure(wraplength=max(360, width - 52))

    def run_memory_diagnostic(self):
        if platform.system() != "Windows":
            messagebox.showerror("Windows required", "Windows Memory Diagnostic is available only on Windows.")
            return
        if not messagebox.askyesno("Windows Memory Diagnostic", "Windows Memory Diagnostic may ask you to restart. Continue to open it?"):
            return
        try:
            subprocess.Popen(["mdsched.exe"])
        except OSError as exc:
            messagebox.showerror("Could not open diagnostic", str(exc))

    def open_battery_report(self):
        if platform.system() != "Windows":
            messagebox.showerror("Windows required", "The Windows battery report is available only on Windows.")
            return
        folder = Path(tempfile.gettempdir()) / "SpeCheck"
        folder.mkdir(parents=True, exist_ok=True)
        report_path = folder / f"battery-report-{datetime.now().strftime('%Y%m%d-%H%M%S')}.html"
        try:
            proc = subprocess.run(["powercfg.exe", "/batteryreport", "/output", str(report_path)], capture_output=True, text=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if proc.returncode != 0 or not report_path.is_file():
                raise RuntimeError((proc.stderr or "Windows could not create the battery report.").strip())
            webbrowser.open(report_path.as_uri())
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            messagebox.showerror("Could not create battery report", str(exc))

    def open_reliability_monitor(self):
        if platform.system() != "Windows":
            messagebox.showerror("Windows required", "Reliability Monitor is available only on Windows.")
            return
        try:
            subprocess.Popen(["perfmon.exe", "/rel"])
        except OSError as exc:
            messagebox.showerror("Could not open Reliability Monitor", str(exc))

    def export_report(self):
        if not self.data:
            messagebox.showinfo("No scan yet", "Run a scan before exporting a report.")
            return
        path = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("SpeCheck JSON report", "*.json")], initialfile="SpeCheck-report.json")
        if not path:
            return
        try:
            payload = {"generated": datetime.now().isoformat(timespec="seconds"), "app": APP_NAME, "data": self.data}
            with open(path, "w", encoding="utf-8") as report_file:
                json.dump(payload, report_file, indent=2, ensure_ascii=False)
            messagebox.showinfo("Report saved", f"The local scan report was saved to:\n{path}")
        except OSError as exc:
            messagebox.showerror("Could not save report", str(exc))


if __name__ == "__main__":
    set_windows_dpi_awareness()
    app = SpeCheck()
    app.mainloop()
