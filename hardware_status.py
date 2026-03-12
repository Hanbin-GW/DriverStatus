import time
import re
import subprocess
import psutil
import json
import platform

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.live import Live
from rich.layout import Layout
from rich.console import Group

console = Console()
WINDOWS_DRIVE_TYPES = {}

def run_command(cmd):
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            shell=False,
        )
        return result.stdout.strip()
    except Exception:
        return ""
    
def get_windows_drive_types():
    """
    Return something like:
    {
        "C": "Internal",
        "D": "External",
        "E": "External"
    }
    """
    mapping = {}

    ps_cmd = [
        "powershell",
        "-NoProfile",
        "-Command",
        (
            "Get-CimInstance Win32_LogicalDisk | "
            "Select-Object DeviceID, DriveType | "
            "ConvertTo-Json -Compress"
        ),
    ]

    output = run_command(ps_cmd)
    if not output:
        return mapping

    try:
        data = json.loads(output)
        if isinstance(data, dict):
            data = [data]

        for item in data:
            device_id = item.get("DeviceID", "")
            drive_type = item.get("DriveType")

            # Win32_LogicalDisk DriveType:
            # 2 = Removable disk
            # 3 = Local disk
            # 4 = Network drive
            if not device_id:
                continue

            letter = device_id.replace(":", "")

            if drive_type == 2:
                mapping[letter] = "External"
            elif drive_type == 3:
                mapping[letter] = "Internal"
            else:
                mapping[letter] = "Other"

    except Exception:
        pass

    return mapping
def get_windows_bus_map():
    ps_cmd = [
        "powershell",
        "-NoProfile",
        "-Command",
        (
            "Get-Disk | Select Number, FriendlyName, BusType | ConvertTo-Json -Compress"
        ),
    ]

    output = run_command(ps_cmd)
    if not output:
        return {}

    try:
        data = json.loads(output)
        if isinstance(data, dict):
            data = [data]
        return {int(d["Number"]): str(d.get("BusType", "")) for d in data}
    except Exception:
        return {}

def bytes_to_gb(value):
    return f"{value / (1024 ** 3):.2f} GB"


def get_disk_type(mountpoint):
    system = platform.system()

    if system == "Darwin":
        if mountpoint.startswith("/Volumes/"):
            return "External"
        return "Internal"

    if system == "Windows":
        letter = mountpoint[:1].upper()
        return WINDOWS_DRIVE_TYPES.get(letter, "Internal")
    return "Internal"


def get_smart_devices():
    output = run_command(["smartctl", "--scan-open"])
    devices = []

    for line in output.splitlines():
        line = line.strip()
        if not (line.startswith("/dev/") or line.startswith("IOService:")):
            continue

        main = line.split("#")[0].strip()
        parts = main.split()

        device = parts[0]
        dtype = None

        if "-d" in parts:
            idx = parts.index("-d")
            if idx + 1 < len(parts):
                dtype = parts[idx + 1]

        devices.append({
            "device": device,
            "dtype": dtype
        })

    return devices


def get_smart_info():
    info = {}

    for dev in get_smart_devices():
        device = dev["device"]
        dtype = dev["dtype"]

        cmd = ["smartctl"]
        if dtype:
            cmd += ["-d", dtype]
        cmd += ["-a", device]

        output = run_command(cmd)
        if not output:
            continue

        model = "Unknown"
        health = "N/A"
        temperature = "N/A"

        for line in output.splitlines():
            m = re.search(r"Device Model:\s+(.+)", line)
            if m:
                model = m.group(1).strip()

            m = re.search(r"Product:\s+(.+)", line)
            if m and model == "Unknown":
                model = m.group(1).strip()

            m = re.search(r"Model Number:\s+(.+)", line)
            if m and model == "Unknown":
                model = m.group(1).strip()

            m = re.search(r"SMART.*result:\s+(.+)", line)
            if m:
                health = m.group(1).strip()

            m = re.search(r"SMART Health Status:\s+(.+)", line)
            if m and health == "N/A":
                health = m.group(1).strip()

            m = re.search(r"Current Drive Temperature:\s+(\d+)", line)
            if m:
                temperature = f"{m.group(1)} °C"

            m = re.search(r"Composite Temperature:\s+(\d+)", line)
            if m and temperature == "N/A":
                temperature = f"{m.group(1)} °C"

            m = re.search(r"Temperature.*?(\d+)", line)
            if m and temperature == "N/A":
                temperature = f"{m.group(1)} °C"

        info[device] = {
            "model": model,
            "health": health,
            "temperature": temperature,
        }

    return info


def get_partitions():
    partitions = []
    seen = set()

    skip_prefixes = [
        "/System/Volumes/VM",
        "/System/Volumes/Preboot",
        "/System/Volumes/Update",
        "/System/Volumes/xarts",
        "/System/Volumes/iSCPreboot",
        "/System/Volumes/Hardware",
    ]

    for part in psutil.disk_partitions(all=False):
        key = (part.device, part.mountpoint)
        if key in seen:
            continue
        seen.add(key)

        if any(part.mountpoint.startswith(prefix) for prefix in skip_prefixes):
            continue

        try:
            usage = psutil.disk_usage(part.mountpoint)
        except Exception:
            continue

        partitions.append({
            "device": part.device,
            "mountpoint": part.mountpoint,
            "type": get_disk_type(part.mountpoint),
            "used": usage.used,
            "total": usage.total,
            "percent": usage.percent,
        })

    return partitions


def make_system_table():
    cpu = psutil.cpu_percent(interval=None)
    mem = psutil.virtual_memory()

    table = Table(title="System Summary", expand=True)
    table.add_column("Metric", style="cyan")
    table.add_column("Value")

    cpu_style = "green"
    if cpu >= 80:
        cpu_style = "bold red"
    elif cpu >= 50:
        cpu_style = "yellow"

    mem_style = "green"
    if mem.percent >= 80:
        mem_style = "bold red"
    elif mem.percent >= 60:
        mem_style = "yellow"

    table.add_row("CPU Usage", f"[{cpu_style}]{cpu:.1f}%[/{cpu_style}]")
    table.add_row("CPU Cores", str(psutil.cpu_count(logical=True)))
    table.add_row(
        "Memory Usage",
        f"[{mem_style}]{mem.percent:.1f}%[/{mem_style}]"
    )
    table.add_row(
        "Memory",
        f"{bytes_to_gb(mem.used)} / {bytes_to_gb(mem.total)}"
    )

    return table


def make_disk_table(partitions):
    table = Table(title="Disk Usage", expand=True)
    table.add_column("Type", style="cyan")
    table.add_column("Device")
    table.add_column("Mount")
    table.add_column("Used", justify="right")
    table.add_column("Total", justify="right")
    table.add_column("Usage %", justify="right")

    for p in partitions:
        percent = p["percent"]

        style = "green"
        if percent >= 85:
            style = "bold red"
        elif percent >= 70:
            style = "yellow"

        table.add_row(
            p["type"],
            p["device"],
            p["mountpoint"],
            bytes_to_gb(p["used"]),
            bytes_to_gb(p["total"]),
            f"[{style}]{percent:.1f}%[/{style}]",
        )

    return table


def make_smart_table(smart):
    table = Table(title="Physical Drive SMART / Temperature", expand=True)
    table.add_column("Device", style="cyan")
    table.add_column("Model")
    table.add_column("Health")
    table.add_column("Temp", justify="right")

    if not smart:
        table.add_row("N/A", "No SMART device found", "N/A", "N/A")
        return table

    for device, info in smart.items():
        temp = info.get("temperature", "N/A")

        if temp != "N/A":
            try:
                value = int(temp.split()[0])
                if value >= 55:
                    temp = f"[bold red]{temp}[/bold red]"
                elif value >= 45:
                    temp = f"[yellow]{temp}[/yellow]"
                else:
                    temp = f"[green]{temp}[/green]"
            except Exception:
                pass

        table.add_row(
            device,
            info.get("model", "Unknown"),
            info.get("health", "N/A"),
            temp,
        )

    return table

def make_header(partitions):
    internal_count = sum(1 for p in partitions if p["type"] == "Internal")
    external_count = sum(1 for p in partitions if p["type"] == "External")

    return Panel(
        f"Internal Drives: [cyan]{internal_count}[/cyan]    "
        f"External Drives: [cyan]{external_count}[/cyan]",
        title="Live Hardware Monitor",
        border_style="blue",
    )


def build_dashboard():
    partitions = get_partitions()
    smart = get_smart_info()

    group = Group(
        make_header(partitions),
        make_system_table(),
        make_disk_table(partitions),
        make_smart_table(smart),
    )
    return group


def main():
    global WINDOWS_DRIVE_TYPES

    if platform.system() == "Windows":
        WINDOWS_DRIVE_TYPES = get_windows_drive_types()

    psutil.cpu_percent(interval=None)

    with Live(build_dashboard(), console=console, refresh_per_second=1, screen=True) as live:
        while True:
            live.update(build_dashboard())
            time.sleep(2)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[bold red]Stopped.[/bold red]")