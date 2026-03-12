import os
import re
import subprocess
from typing import Dict, List, Optional

import psutil
from rich.console import Console
from rich.table import Table
from rich.panel import Panel


console = Console()


def run_cmd(cmd: List[str]) -> str:
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def bytes_to_gb(value: int) -> str:
    return f"{value / (1024 ** 3):.2f} GB"


def get_disk_type(device: str, mountpoint: str) -> str:
    # macOS 기준 간단 분류
    if mountpoint.startswith("/Volumes/"):
        return "External"
    return "Internal"


def get_smart_devices() -> List[Dict[str, str]]:
    """
    smartctl --scan-open 결과 파싱
    예:
    /dev/disk0 -d sat # ...
    /dev/disk4 -d sat # ...
    """
    output = run_cmd(["smartctl", "--scan-open"])
    devices = []

    for line in output.splitlines():
        line = line.strip()
        if not line or not line.startswith("/dev/"):
            continue

        # "/dev/disk4 -d sat # comment"
        main = line.split("#", 1)[0].strip()
        parts = main.split()

        device = parts[0]
        dtype = None

        if "-d" in parts:
            idx = parts.index("-d")
            if idx + 1 < len(parts):
                dtype = parts[idx + 1]

        devices.append(
            {
                "device": device,
                "dtype": dtype or "",
            }
        )

    return devices


def smartctl_base_cmd(device: str, dtype: str) -> List[str]:
    cmd = ["smartctl"]
    if dtype:
        cmd += ["-d", dtype]
    cmd += ["-a", device]
    return cmd


def parse_temperature(text: str) -> Optional[str]:
    patterns = [
        r"Current Drive Temperature:\s+(\d+)\s+C",
        r"Temperature:\s+(\d+)\s+Celsius",
        r"Temperature_Celsius.*?(\d+)$",
        r"Airflow_Temperature_Cel.*?(\d+)$",
        r"Temperature Sensor 1:\s+(\d+)\s+C",
        r"Composite Temperature:\s+(\d+)\s+C",
    ]

    for line in text.splitlines():
        for pattern in patterns:
            m = re.search(pattern, line)
            if m:
                return f"{m.group(1)} °C"

    return None


def parse_health(text: str) -> Optional[str]:
    patterns = [
        r"SMART overall-health self-assessment test result:\s+(.+)",
        r"SMART Health Status:\s+(.+)",
        r"SMART overall-health self-assessment.*:\s+(.+)",
    ]

    for line in text.splitlines():
        for pattern in patterns:
            m = re.search(pattern, line)
            if m:
                return m.group(1).strip()

    if "PASSED" in text:
        return "PASSED"
    if "OK" in text:
        return "OK"

    return None


def parse_model(text: str) -> Optional[str]:
    patterns = [
        r"Device Model:\s+(.+)",
        r"Product:\s+(.+)",
        r"Model Number:\s+(.+)",
        r"Model Family:\s+(.+)",
    ]

    for line in text.splitlines():
        for pattern in patterns:
            m = re.search(pattern, line)
            if m:
                return m.group(1).strip()

    return None


def get_smart_info() -> Dict[str, Dict[str, str]]:
    """
    반환 예:
    {
        "/dev/disk4": {
            "model": "...",
            "health": "PASSED",
            "temperature": "38 °C"
        }
    }
    """
    info: Dict[str, Dict[str, str]] = {}

    scan = run_cmd(["smartctl", "--scan-open"])
    if not scan:
        return info

    for item in get_smart_devices():
        device = item["device"]
        dtype = item["dtype"]

        output = run_cmd(smartctl_base_cmd(device, dtype))
        if not output:
            continue

        info[device] = {
            "model": parse_model(output) or "Unknown",
            "health": parse_health(output) or "N/A",
            "temperature": parse_temperature(output) or "N/A",
        }

    return info


def get_partitions():
    partitions = []
    seen = set()

    for part in psutil.disk_partitions(all=False):
        key = (part.device, part.mountpoint)
        if key in seen:
            continue
        seen.add(key)

        try:
            usage = psutil.disk_usage(part.mountpoint)
        except PermissionError:
            continue
        except FileNotFoundError:
            continue

        # macOS 시스템 볼륨 일부 제외
        skip_prefixes = [
            "/System/Volumes/VM",
            "/System/Volumes/Preboot",
            "/System/Volumes/Update",
            "/System/Volumes/xarts",
            "/System/Volumes/iSCPreboot",
            "/System/Volumes/Hardware",
        ]
        if any(part.mountpoint.startswith(p) for p in skip_prefixes):
            continue

        partitions.append(
            {
                "device": part.device,
                "mountpoint": part.mountpoint,
                "fstype": part.fstype,
                "used": usage.used,
                "total": usage.total,
                "percent": usage.percent,
                "type": get_disk_type(part.device, part.mountpoint),
            }
        )

    return partitions


def disk_usage_table(partitions):
    table = Table(title="Disk Usage", expand=True)
    table.add_column("Type", style="cyan", no_wrap=True)
    table.add_column("Device", style="magenta")
    table.add_column("Mount")
    table.add_column("Used", justify="right")
    table.add_column("Total", justify="right")
    table.add_column("Usage %", justify="right")

    for p in partitions:
        percent_style = "green"
        if p["percent"] >= 85:
            percent_style = "bold red"
        elif p["percent"] >= 70:
            percent_style = "yellow"

        table.add_row(
            p["type"],
            p["device"],
            p["mountpoint"],
            bytes_to_gb(p["used"]),
            bytes_to_gb(p["total"]),
            f"[{percent_style}]{p['percent']:.1f}%[/{percent_style}]",
        )

    return table


def smart_table(partitions, smart_info):
    table = Table(title="Drive SMART / Temperature", expand=True)
    table.add_column("Type", style="cyan", no_wrap=True)
    table.add_column("Device", style="magenta")
    table.add_column("Mount")
    table.add_column("Model")
    table.add_column("Health")
    table.add_column("Temp", justify="right")

    used_devices = set()

    for p in partitions:
        device = p["device"]
        if device in used_devices:
            continue
        used_devices.add(device)

        info = smart_info.get(device, {})
        temp = info.get("temperature", "N/A")
        health = info.get("health", "N/A")
        model = info.get("model", "Unknown")

        temp_text = temp
        if temp != "N/A":
            try:
                temp_value = int(temp.split()[0])
                if temp_value >= 55:
                    temp_text = f"[bold red]{temp}[/bold red]"
                elif temp_value >= 45:
                    temp_text = f"[yellow]{temp}[/yellow]"
                else:
                    temp_text = f"[green]{temp}[/green]"
            except Exception:
                pass

        table.add_row(
            p["type"],
            device,
            p["mountpoint"],
            model,
            health,
            temp_text,
        )

    return table


def system_summary_panel(partitions):
    internal_count = sum(1 for p in partitions if p["type"] == "Internal")
    external_count = sum(1 for p in partitions if p["type"] == "External")

    return Panel(
        (
            f"[bold]Drives Summary[/bold]\n"
            f"Internal Drives: [cyan]{internal_count}[/cyan]\n"
            f"External Drives: [cyan]{external_count}[/cyan]"
        ),
        title="System Status",
        border_style="blue",
    )


def main():
    partitions = get_partitions()
    smart_info = get_smart_info()

    console.print(system_summary_panel(partitions))
    console.print(disk_usage_table(partitions))

    if not smart_info:
        console.print(
            Panel(
                "SMART 정보를 가져오지 못했습니다.\n"
                "macOS에서는 먼저 [bold]brew install smartmontools[/bold] 후,\n"
                "일부 외장하드는 USB 브리지 때문에 온도가 안 나올 수 있습니다.",
                title="SMART Notice",
                border_style="yellow",
            )
        )
    else:
        console.print(smart_table(partitions, smart_info))


if __name__ == "__main__":
    main()