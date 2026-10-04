#!/usr/bin/env python3
import argparse
import contextlib
import html
import ipaddress
import io
import json
import os
import plistlib
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "servers.json"
EXAMPLE_CONFIG = ROOT / "servers.example.json"
STATE_DIR = ROOT / ".state"
PID_DIR = STATE_DIR / "pids"
LOG_DIR = STATE_DIR / "logs"
EVENTS_FILE = STATE_DIR / "events.jsonl"
LAUNCHD_LABEL = os.environ.get("SERVER_MANAGER_LAUNCHD_LABEL", "com.local.server-manager")
LAUNCHD_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
WEB_LAUNCHD_LABEL = os.environ.get("SERVER_MANAGER_WEB_LAUNCHD_LABEL", "com.local.server-manager.web")
WEB_LAUNCHD_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{WEB_LAUNCHD_LABEL}.plist"
DEFAULT_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
DEFAULT_SUPERVISE_INTERVAL = 1800
DEFAULT_SERVICE_HOST = "0.0.0.0"
DEFAULT_WEB_HOST = "0.0.0.0"
DEFAULT_WEB_PORT = 8765
POWER_DAYS = "MTWRFSU"
POWER_DAY_LABELS = {
    "M": "Mon",
    "T": "Tue",
    "W": "Wed",
    "R": "Thu",
    "F": "Fri",
    "S": "Sat",
    "U": "Sun",
}
POWER_DAY_NAMES = {
    "monday": "M",
    "tuesday": "T",
    "wednesday": "W",
    "thursday": "R",
    "friday": "F",
    "saturday": "S",
    "sunday": "U",
}
POWER_INTERVAL_CONFIG = STATE_DIR / "power-interval.json"
POWER_INTERVAL_LABEL = os.environ.get(
    "SERVER_MANAGER_POWER_INTERVAL_LABEL",
    "com.local.server-manager.power-interval",
)
POWER_INTERVAL_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{POWER_INTERVAL_LABEL}.plist"
STOP_REQUESTED = False


@dataclass(frozen=True)
class Service:
    name: str
    description: str
    cwd: Path | None
    command: list[str]
    kind: str = "process"
    port: int | None = None
    extra_ports: list[int] | None = None
    url: str | None = None
    env: dict[str, str] | None = None
    launchd_label: str | None = None
    launchd_domain: str | None = None
    launchd_auto_start: bool = True
    launchd_plist: Path | None = None
    stdout_path: Path | None = None
    stderr_path: Path | None = None
    start_wait_seconds: int = 2
    enabled: bool = True
    created_at: str = ""
    updated_at: str = ""

    @property
    def pid_file(self) -> Path:
        return PID_DIR / f"{self.name}.pid"

    @property
    def log_dir(self) -> Path:
        if self.cwd is None:
            return LOG_DIR
        return self.cwd / ".server-manager" / "logs"

    @property
    def log_file(self) -> Path:
        if self.kind == "launchd" and self.stdout_path:
            return self.stdout_path
        return self.log_dir / f"{self.name}.log"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def valid_lan_ipv4(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return address.version == 4 and not (address.is_loopback or address.is_link_local or address.is_unspecified)


def lan_ip_address() -> str:
    override = os.environ.get("SERVER_MANAGER_LAN_IP", "").strip()
    if valid_lan_ipv4(override):
        return override

    interfaces = []
    route = shutil.which("route") or "/sbin/route"
    route_result = subprocess.run(
        [route, "-n", "get", "default"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    match = re.search(r"^\s*interface:\s*(\S+)", route_result.stdout, re.MULTILINE)
    if match:
        interfaces.append(match.group(1))
    interfaces.extend(interface for interface in ("en0", "en1") if interface not in interfaces)

    ipconfig = shutil.which("ipconfig") or "/usr/sbin/ipconfig"
    for interface in interfaces:
        result_obj = subprocess.run(
            [ipconfig, "getifaddr", interface],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        candidate = result_obj.stdout.strip()
        if valid_lan_ipv4(candidate):
            return candidate

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 9))
            candidate = sock.getsockname()[0]
            if valid_lan_ipv4(candidate):
                return candidate
    except OSError:
        pass
    return "127.0.0.1"


def local_url_host(host: str) -> bool:
    lowered = host.lower().strip("[]")
    if lowered in {"localhost", "0.0.0.0", "127.0.0.1", "::", "::1"}:
        return True
    try:
        address = ipaddress.ip_address(lowered)
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_unspecified


def url_with_lan_ip(url: str | None, lan_ip: str | None = None) -> str | None:
    if not url:
        return url
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or not local_url_host(parsed.hostname):
        return url
    try:
        port = parsed.port
    except ValueError:
        return url
    host = lan_ip or lan_ip_address()
    netloc = f"{host}:{port}" if port else host
    return parsed._replace(netloc=netloc).geturl()


def ensure_dirs() -> None:
    PID_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def read_config() -> dict:
    if not CONFIG.exists():
        source = EXAMPLE_CONFIG if EXAMPLE_CONFIG.exists() else None
        if source:
            CONFIG.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            write_config({"services": []})
    with CONFIG.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def write_config(raw: dict) -> None:
    CONFIG.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def service_to_config(service: Service) -> dict:
    item: dict = {
        "name": service.name,
        "description": service.description,
        "kind": service.kind,
        "enabled": bool(service.enabled),
        "createdAt": service.created_at or now_iso(),
        "updatedAt": service.updated_at or now_iso(),
    }
    if service.kind == "launchd":
        if service.launchd_label:
            item["launchdLabel"] = service.launchd_label
        if service.launchd_domain:
            item["launchdDomain"] = service.launchd_domain
        item["launchdAutoStart"] = service.launchd_auto_start
        if service.launchd_plist:
            item["launchdPlist"] = str(service.launchd_plist)
        if service.cwd:
            item["cwd"] = str(service.cwd)
        if service.port is not None:
            item["primaryPort"] = service.port
        if service.extra_ports:
            item["extraPorts"] = service.extra_ports
        if service.stdout_path:
            item["stdoutPath"] = str(service.stdout_path)
        if service.stderr_path:
            item["stderrPath"] = str(service.stderr_path)
    else:
        item["cwd"] = str(service.cwd)
        item["command"] = list(service.command)
        if service.port is not None:
            item["port"] = service.port
        if service.env:
            item["env"] = service.env
    if service.start_wait_seconds != 2:
        item["startWaitSeconds"] = service.start_wait_seconds
    if service.url:
        item["url"] = service.url
    return item


def normalize_port(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("連接埠必須是數字") from exc
    if port < 1 or port > 65535:
        raise ValueError("連接埠必須介於 1 至 65535")
    return port


def normalize_ports(value) -> list[int]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        raw_values = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, list):
        raw_values = value
    else:
        raise ValueError("其他連接埠請用逗號分隔")
    ports = []
    for raw in raw_values:
        port = normalize_port(raw)
        if port is not None and port not in ports:
            ports.append(port)
    return ports


def load_services() -> dict[str, Service]:
    raw = read_config()
    services = {}
    for item in raw.get("services", []):
        kind = item.get("kind", "process")
        command = item.get("command", [])
        if isinstance(command, str):
            command = shlex.split(command)
        if not isinstance(command, list):
            raise SystemExit(f"Invalid command for service {item.get('name', '<unknown>')}")
        cwd_raw = item.get("cwd")
        service = Service(
            name=item["name"],
            description=item.get("description", ""),
            cwd=Path(cwd_raw).expanduser() if cwd_raw else None,
            command=[str(part) for part in command],
            kind=kind,
            port=normalize_port(item.get("primaryPort", item.get("port"))),
            extra_ports=normalize_ports(item.get("extraPorts", [])),
            url=item.get("url"),
            env=item.get("env"),
            launchd_label=item.get("launchdLabel"),
            launchd_domain=item.get("launchdDomain"),
            launchd_auto_start=bool(item.get("launchdAutoStart", True)),
            launchd_plist=Path(item["launchdPlist"]).expanduser() if item.get("launchdPlist") else None,
            stdout_path=Path(item["stdoutPath"]).expanduser() if item.get("stdoutPath") else None,
            stderr_path=Path(item["stderrPath"]).expanduser() if item.get("stderrPath") else None,
            start_wait_seconds=max(0, int(item.get("startWaitSeconds", 2) or 0)),
            enabled=bool(item.get("enabled", True)),
            created_at=item.get("createdAt", ""),
            updated_at=item.get("updatedAt", ""),
        )
        if service.kind not in {"process", "launchd"}:
            raise SystemExit(f"Invalid service kind in {CONFIG}: {service.name} has {service.kind}")
        if service.kind == "process" and service.cwd is None:
            raise SystemExit(f"Process service missing cwd in {CONFIG}: {service.name}")
        if service.kind == "launchd" and not service.launchd_label:
            raise SystemExit(f"Launchd service missing launchdLabel in {CONFIG}: {service.name}")
        if service.name in services:
            raise SystemExit(f"Duplicate service name in {CONFIG}: {service.name}")
        services[service.name] = service
    return services


def save_services(services: dict[str, Service]) -> None:
    timestamp = now_iso()
    normalized = {}
    for name, service in services.items():
        created_at = service.created_at or timestamp
        updated_at = service.updated_at or timestamp
        normalized[name] = replace(service, created_at=created_at, updated_at=updated_at)
    write_config({"services": [service_to_config(service) for service in normalized.values()]})


def touch_service(services: dict[str, Service], name: str, **changes) -> Service:
    changes["updated_at"] = now_iso()
    services[name] = replace(services[name], **changes)
    save_services(services)
    return services[name]


def select_services(all_services: dict[str, Service], names: list[str] | None) -> list[Service]:
    if not names or names == ["all"]:
        return list(all_services.values())

    missing = [name for name in names if name not in all_services]
    if missing:
        known = ", ".join(all_services)
        raise SystemExit(f"Unknown service: {', '.join(missing)}\nKnown services: {known}")
    return [all_services[name] for name in names]


def slugify_service_name(value: str) -> str:
    slug = re.sub(r"[^a-z0-9_-]+", "-", value.strip().lower())
    slug = slug.strip("-_")
    return slug or "server"


def unique_service_name(candidate: str, services: dict[str, Service], original: str | None = None) -> str:
    base = slugify_service_name(candidate)
    if base == original or base not in services:
        return base
    index = 2
    while f"{base}-{index}" in services:
        index += 1
    return f"{base}-{index}"


def parse_command_payload(payload: dict, existing: list[str] | None = None) -> list[str]:
    if "command" in payload:
        command = payload["command"]
    elif "commandText" in payload:
        command = payload["commandText"]
    elif existing is not None:
        command = list(existing)
    else:
        command = ""

    if isinstance(command, list):
        parsed = [str(part) for part in command]
    elif isinstance(command, str):
        try:
            parsed = shlex.split(command)
        except ValueError as exc:
            raise ValueError(f"無法解析啟動指令：{exc}") from exc
    else:
        raise ValueError("啟動指令格式不正確")
    if not parsed:
        raise ValueError("請填寫啟動指令")
    return normalize_bind_command(parsed)


def normalize_bind_command(command: list[str]) -> list[str]:
    def wildcard_value(value: str) -> str:
        if value.startswith(("unix:", "fd:")):
            return value
        host, separator, port = value.rpartition(":")
        if separator and port.isdigit() and host:
            return f"{DEFAULT_SERVICE_HOST}:{port}"
        return DEFAULT_SERVICE_HOST

    normalized = list(command)
    for index, part in enumerate(normalized):
        if part in {"--host", "--bind"} and index + 1 < len(normalized):
            normalized[index + 1] = wildcard_value(normalized[index + 1])
            continue
        if part.startswith("--host=") or part.startswith("--bind="):
            flag, value = part.split("=", 1)
            normalized[index] = f"{flag}={wildcard_value(value)}"
            continue
        if re.fullmatch(r"(?:localhost|127\.0\.0\.1|0\.0\.0\.0):\d+", part, re.IGNORECASE):
            normalized[index] = re.sub(r"^[^:]+", DEFAULT_SERVICE_HOST, part)
    return normalized


def record_event(service_name: str, action: str, message: str = "", pid: int | None = None) -> None:
    ensure_dirs()
    event = {
        "ts": now_iso(),
        "service": service_name,
        "action": action,
        "message": message,
    }
    if pid is not None:
        event["pid"] = pid
    with EVENTS_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def load_events(limit: int = 5000) -> list[dict]:
    try:
        lines = EVENTS_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return []
    events = []
    for line in lines[-limit:]:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def latest_service_events() -> dict[str, dict[str, str]]:
    mapping = {
        "started": "lastStartedAt",
        "stopped": "lastStoppedAt",
        "restarted": "lastRestartedAt",
        "checked": "lastCheckedAt",
        "checked_skipped": "lastCheckedAt",
    }
    latest: dict[str, dict[str, str]] = {}
    for event in load_events():
        field = mapping.get(event.get("action"))
        if not field:
            continue
        service = event.get("service")
        ts = event.get("ts")
        if service and ts:
            latest.setdefault(service, {})[field] = ts
    return latest


def read_pid(service: Service) -> int | None:
    try:
        return int(service.pid_file.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return None


def remove_pid(service: Service) -> None:
    try:
        service.pid_file.unlink()
    except FileNotFoundError:
        pass


def process_stat(pid: int) -> str:
    ps = shutil.which("ps")
    if not ps:
        return ""
    try:
        result = subprocess.run([ps, "-o", "stat=", "-p", str(pid)], text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    except OSError:
        return ""
    return result.stdout.strip()


def pid_zombie(pid: int) -> bool:
    stat = process_stat(pid)
    return bool(stat) and stat[0].upper() == "Z"


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return not pid_zombie(pid)
    return not pid_zombie(pid)


def process_finished(pid: int) -> bool:
    if pid_zombie(pid):
        return True
    try:
        waited, _status = os.waitpid(pid, os.WNOHANG)
        if waited == pid:
            return True
    except ChildProcessError:
        pass
    return not pid_alive(pid)


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.25) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        if sock.connect_ex((host, port)) == 0:
            return True
    if host == "127.0.0.1":
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            if sock.connect_ex(("::1", port)) == 0:
                return True
        return bool(listener_pids(port))
    return False


def wait_for_port(port: int, seconds: int) -> bool:
    deadline = time.monotonic() + max(0, seconds)
    while time.monotonic() <= deadline:
        if port_open(port):
            return True
        time.sleep(0.5)
    return port_open(port)


def listener_pids(port: int) -> list[int]:
    lsof = shutil.which("lsof")
    if not lsof:
        return []
    result = subprocess.run(
        [lsof, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    pids = []
    for line in result.stdout.splitlines():
        try:
            pids.append(int(line.strip()))
        except ValueError:
            pass
    return sorted(set(pids))


def open_port_details(ports: list[int]) -> list[str]:
    details = []
    for port in ports:
        if port_open(port):
            pids = listener_pids(port)
            detail = f"port {port} open"
            if pids:
                detail += f" by pid(s) {', '.join(map(str, pids))}"
            details.append(detail)
    return details


def launchd_target(label: str, domain: str | None) -> str:
    if domain:
        if domain == "gui":
            return f"gui/{os.getuid()}/{label}"
        if "/" in domain:
            return f"{domain}/{label}"
        return f"{domain}/{label}"
    return f"gui/{os.getuid()}/{label}"


def launchd_domain_target(domain: str | None) -> str:
    if domain == "system":
        return "system"
    if domain == "gui" or not domain:
        return f"gui/{os.getuid()}"
    return domain


def launchd_print(label: str, domain: str | None) -> str:
    target = launchd_target(label, domain)
    result = subprocess.run(["launchctl", "print", target], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode == 0:
        return result.stdout
    return result.stderr.strip() or f"{target} is not loaded"


def infer_launchd_plist(service: Service, raw: str = "") -> Path | None:
    if service.launchd_plist:
        return service.launchd_plist
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("path ="):
            return Path(stripped.split("=", 1)[1].strip())
    label = service.launchd_label or service.name
    candidates = [
        Path.home() / "Library" / "LaunchAgents" / f"{label}.plist",
        Path("/Library/LaunchAgents") / f"{label}.plist",
        Path("/Library/LaunchDaemons") / f"{label}.plist",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def run_launchctl(args: list[str], service: Service) -> subprocess.CompletedProcess:
    cmd = ["launchctl", *args]
    result = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode == 0 or service.launchd_domain != "system":
        return result
    sudo = shutil.which("sudo")
    if not sudo:
        return result
    sudo_result = subprocess.run([sudo, "-n", *cmd], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    return sudo_result


def ensure_launchd_loaded(service: Service) -> subprocess.CompletedProcess | None:
    raw = launchd_print(service.launchd_label or service.name, service.launchd_domain)
    parsed = parse_launchd_status(raw, service.launchd_label or service.name)
    if parsed.get("loaded"):
        return None
    plist = infer_launchd_plist(service, raw)
    if not plist:
        return subprocess.CompletedProcess(["launchctl", "bootstrap"], 1, "", f"plist not found for {service.name}")
    # A disabled label can block bootstrap with launchd error 119. Start/restart
    # temporarily enables it, then kickstart_launchd_service disables it again
    # when launchdAutoStart is false.
    target = launchd_target(service.launchd_label or service.name, service.launchd_domain)
    enable_result = run_launchctl(["enable", target], service)
    if enable_result.returncode != 0:
        return enable_result
    return run_launchctl(["bootstrap", launchd_domain_target(service.launchd_domain), str(plist)], service)


def launchctl_message(command: str, result_obj: subprocess.CompletedProcess) -> str:
    output = (result_obj.stderr or result_obj.stdout or "").strip()
    if output:
        return f"{command} failed: {output}"
    return f"{command} failed with code {result_obj.returncode}"


def kickstart_launchd_service(service: Service, action_name: str = "start") -> dict:
    target = launchd_target(service.launchd_label or service.name, service.launchd_domain)
    load_result = ensure_launchd_loaded(service)
    if load_result is not None and load_result.returncode != 0:
        message = f"fail  {service.name}: {launchctl_message('bootstrap', load_result)}"
        record_event(service.name, f"{action_name}_failed", message)
        return result(False, message)

    enable_result = run_launchctl(["enable", target], service)
    if enable_result.returncode != 0:
        message = f"fail  {service.name}: {launchctl_message('enable', enable_result)}"
        record_event(service.name, f"{action_name}_failed", message)
        return result(False, message)

    kickstart_result = run_launchctl(["kickstart", "-k", target], service)
    if kickstart_result.returncode != 0:
        message = f"fail  {service.name}: {launchctl_message('kickstart', kickstart_result)}"
        record_event(service.name, f"{action_name}_failed", message)
        return result(False, message)

    if not service.launchd_auto_start:
        run_launchctl(["disable", target], service)

    time.sleep(1.2)
    if service.port and not wait_for_port(service.port, service.start_wait_seconds):
        new_state, new_pid, new_detail = service_state(service)
        message = f"fail  {service.name}: primary port {service.port} did not open after {service.start_wait_seconds}s ({new_detail})"
        record_event(service.name, f"{action_name}_failed", message, new_pid)
        return result(False, message, state=new_state, pid=new_pid)

    new_state, new_pid, new_detail = service_state(service)
    if new_state != "running":
        message = f"fail  {service.name}: launchd state is {new_state} after kickstart ({new_detail})"
        record_event(service.name, f"{action_name}_failed", message, new_pid)
        return result(False, message, state=new_state, pid=new_pid)

    message = f"{action_name} {service.name}: launchd {target}; {new_detail}"
    event_name = "restarted" if action_name == "restart" else "started"
    record_event(service.name, event_name, message, new_pid)
    return result(True, message, state=new_state, pid=new_pid)


def start_launchd_service(service: Service) -> dict:
    target = launchd_target(service.launchd_label or service.name, service.launchd_domain)
    state, pid, detail = service_state(service)
    if state == "running":
        if not service.launchd_auto_start:
            run_launchctl(["disable", target], service)
        return result(True, f"ok    {service.name}: already running ({detail})", state=state, pid=pid)

    return kickstart_launchd_service(service, "start")


def stop_launchd_service(service: Service) -> dict:
    target = launchd_target(service.launchd_label or service.name, service.launchd_domain)
    disable_result = run_launchctl(["disable", target], service)
    kill_result = run_launchctl(["kill", "SIGTERM", target], service)
    if disable_result.returncode != 0:
        message = f"fail  {service.name}: {launchctl_message('disable', disable_result)}"
        record_event(service.name, "stop_failed", message)
        return result(False, message, disableReturnCode=disable_result.returncode)
    if kill_result.returncode != 0:
        # launchctl returns an error when the job is loaded but not currently running; that is OK after disable.
        state, pid, detail = service_state(service)
        if state not in {"stopped", "scheduled"} or pid:
            message = f"fail  {service.name}: {launchctl_message('kill', kill_result)}"
            record_event(service.name, "stop_failed", message, pid)
            return result(False, message, state=state, pid=pid)

    time.sleep(0.8)
    state, pid, detail = service_state(service)
    if pid:
        message = f"fail  {service.name}: launchd target still has pid {pid} ({detail})"
        record_event(service.name, "stop_failed", message, pid)
        return result(False, message, state=state, pid=pid)
    message = f"stop  {service.name}: disabled {target}; {detail}"
    record_event(service.name, "stopped", message)
    return result(True, message, state=state, pid=pid)


def parse_launchd_status(raw: str, label: str = LAUNCHD_LABEL) -> dict:
    lowered = raw.lower()
    missing_markers = (
        "not loaded",
        "could not find service",
        "service is disabled",
        "bad request",
    )
    loaded = bool(raw.strip()) and not any(marker in lowered for marker in missing_markers)
    status = {"label": label, "loaded": loaded, "raw": raw}
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("state =") and "state" not in status:
            status["state"] = stripped.split("=", 1)[1].strip()
        elif stripped.startswith("pid =") and "pid" not in status:
            status["pid"] = stripped.split("=", 1)[1].strip()
        elif stripped.startswith("last exit code =") and "lastExitCode" not in status:
            status["lastExitCode"] = stripped.split("=", 1)[1].strip()
        elif stripped.startswith("last terminating signal =") and "lastTerminatingSignal" not in status:
            status["lastTerminatingSignal"] = stripped.split("=", 1)[1].strip()
        elif stripped.startswith("run interval =") and "runInterval" not in status:
            status["runInterval"] = stripped.split("=", 1)[1].strip()
        elif "com.apple.launchd.calendarinterval" in stripped:
            status["calendarInterval"] = "configured"
        elif stripped.startswith('"Hour" =>') and "calendarHour" not in status:
            status["calendarHour"] = stripped.split("=>", 1)[1].strip()
        elif stripped.startswith('"Minute" =>') and "calendarMinute" not in status:
            status["calendarMinute"] = stripped.split("=>", 1)[1].strip()
        elif stripped.startswith('"Weekday" =>') and "calendarWeekday" not in status:
            status["calendarWeekday"] = stripped.split("=>", 1)[1].strip()
    if status.get("calendarInterval"):
        parts = []
        if status.get("calendarWeekday"):
            parts.append(f"weekday {status['calendarWeekday']}")
        if status.get("calendarHour") is not None and status.get("calendarMinute") is not None:
            try:
                parts.append(f"{int(status['calendarHour']):02d}:{int(status['calendarMinute']):02d}")
            except ValueError:
                parts.append(f"{status['calendarHour']}:{status['calendarMinute']}")
        status["calendarInterval"] = ", ".join(parts) if parts else "configured"
    return status


def launchd_service_state(service: Service) -> tuple[str, int | None, str]:
    raw = launchd_print(service.launchd_label or service.name, service.launchd_domain)
    parsed = parse_launchd_status(raw, service.launchd_label or service.name)
    if not parsed.get("loaded"):
        return "stopped", None, f"launchd not loaded: {launchd_target(service.launchd_label or service.name, service.launchd_domain)}"

    pid = None
    try:
        pid = int(parsed["pid"]) if parsed.get("pid") else None
    except ValueError:
        pid = None

    launchd_state = parsed.get("state", "unknown")
    detail_parts = [f"launchd {launchd_state}"]
    if parsed.get("runInterval"):
        detail_parts.append(f"interval {parsed['runInterval']}")
    if parsed.get("calendarInterval"):
        detail_parts.append(f"calendar {parsed['calendarInterval']}")
    if parsed.get("lastExitCode"):
        detail_parts.append(f"last exit {parsed['lastExitCode']}")
    if parsed.get("lastTerminatingSignal"):
        detail_parts.append(f"last signal {parsed['lastTerminatingSignal']}")
    port_details = open_port_details([port for port in [service.port, *(service.extra_ports or [])] if port])
    detail_parts.extend(port_details)

    if launchd_state == "running":
        return "running", pid, "; ".join(detail_parts)
    if launchd_state in {"spawn scheduled", "not running"} and (parsed.get("runInterval") or parsed.get("calendarInterval")):
        return "scheduled", pid, "; ".join(detail_parts)
    if launchd_state == "spawn scheduled":
        return "scheduled", pid, "; ".join(detail_parts)
    return "stopped", pid, "; ".join(detail_parts)


def service_state(service: Service) -> tuple[str, int | None, str]:
    if service.kind == "launchd":
        return launchd_service_state(service)

    pid = read_pid(service)
    if pid and pid_alive(pid):
        if service.port:
            if port_open(service.port):
                pids = listener_pids(service.port)
                detail = f"pid alive; port {service.port} open"
                if pids:
                    detail += f" by pid(s) {', '.join(map(str, pids))}"
                return "managed", pid, detail
            return "unhealthy", pid, f"pid alive but port {service.port} is closed"
        return "managed", pid, "pid alive"
    if pid:
        remove_pid(service)

    if service.port and port_open(service.port):
        pids = listener_pids(service.port)
        detail = f"port {service.port} is open"
        if pids:
            detail += f" by pid(s) {', '.join(map(str, pids))}"
        return "external", None, detail

    return "stopped", None, "not running"


def command_exists(command: str) -> bool:
    if "/" in command:
        return Path(command).exists()
    return shutil.which(command) is not None


def result(ok: bool, message: str, **extra) -> dict:
    print(message)
    payload = {"ok": ok, "message": message}
    payload.update(extra)
    return payload


def start_service(service: Service) -> dict:
    if service.kind == "launchd":
        return start_launchd_service(service)
    ensure_dirs()
    state, pid, detail = service_state(service)
    if state == "managed":
        return result(True, f"ok    {service.name}: already managed by pid {pid}", state=state, pid=pid)
    if state == "unhealthy":
        stop_result = stop_service(service)
        if not stop_result.get("ok"):
            return stop_result
    if state == "external":
        return result(True, f"skip  {service.name}: already running outside manager ({detail})", state=state, skipped=True)
    if not service.cwd.exists():
        message = f"fail  {service.name}: cwd does not exist: {service.cwd}"
        record_event(service.name, "start_failed", message)
        return result(False, message)
    if not service.command:
        message = f"fail  {service.name}: command is empty"
        record_event(service.name, "start_failed", message)
        return result(False, message)
    if not command_exists(service.command[0]):
        message = f"fail  {service.name}: command not found: {service.command[0]}"
        record_event(service.name, "start_failed", message)
        return result(False, message)

    service.log_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PATH"] = env.get("PATH") or DEFAULT_PATH
    for path_part in reversed(DEFAULT_PATH.split(":")):
        if path_part not in env["PATH"].split(":"):
            env["PATH"] = f"{path_part}:{env['PATH']}"
    if service.env:
        env.update(service.env)
    if service.url:
        env["HOST"] = DEFAULT_SERVICE_HOST

    with service.log_file.open("ab") as log:
        log.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} starting {service.name} ---\n".encode())
        process = subprocess.Popen(
            service.command,
            cwd=service.cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    service.pid_file.write_text(str(process.pid), encoding="utf-8")
    time.sleep(0.8)
    if process.poll() is not None:
        remove_pid(service)
        message = f"fail  {service.name}: exited with code {process.returncode}; see {service.log_file}"
        record_event(service.name, "start_failed", message)
        return result(False, message, returncode=process.returncode)
    if service.port and not wait_for_port(service.port, service.start_wait_seconds):
        if process.poll() is None:
            terminate_pid(process.pid)
        remove_pid(service)
        message = f"fail  {service.name}: port {service.port} did not open after {service.start_wait_seconds}s; see {service.log_file}"
        record_event(service.name, "start_failed", message, process.pid)
        return result(False, message, pid=process.pid)

    message = f"start {service.name}: pid {process.pid}; log {service.log_file}"
    record_event(service.name, "started", message, process.pid)
    return result(True, message, state="managed", pid=process.pid, logPath=str(service.log_file))


def terminate_pid(pid: int, grace_seconds: float = 8.0) -> bool:
    if pid_zombie(pid):
        return True
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except PermissionError:
        print(f"fail  pid {pid}: permission denied")
        return False

    deadline = time.time() + grace_seconds
    while time.time() < deadline:
        if process_finished(pid):
            return True
        time.sleep(0.2)

    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except PermissionError:
        if process_finished(pid):
            return True
        print(f"fail  pid {pid}: permission denied")
        return False
    return process_finished(pid)


def stop_service(service: Service, by_port: bool = False) -> dict:
    if service.kind == "launchd":
        return stop_launchd_service(service)
    pid = read_pid(service)
    if pid and pid_alive(pid):
        if terminate_pid(pid):
            remove_pid(service)
            message = f"stop  {service.name}: stopped pid {pid}"
            record_event(service.name, "stopped", message, pid)
            return result(True, message, pid=pid)
        return result(False, f"fail  {service.name}: could not stop pid {pid}", pid=pid)
    if pid:
        remove_pid(service)

    if by_port and service.port:
        pids = listener_pids(service.port)
        if not pids:
            return result(True, f"ok    {service.name}: already stopped")
        for listener_pid in pids:
            try:
                pgid = os.getpgid(listener_pid)
                if terminate_pid(pgid):
                    record_event(service.name, "stopped", f"stopped listener pid {listener_pid} on port {service.port}", listener_pid)
                    print(f"stop  {service.name}: stopped listener pid {listener_pid} on port {service.port}")
                else:
                    print(f"fail  {service.name}: could not stop listener pid {listener_pid} on port {service.port}")
            except ProcessLookupError:
                pass
            except PermissionError:
                print(f"fail  {service.name}: permission denied for listener pid {listener_pid}")

        deadline = time.time() + 5.0
        while time.time() < deadline:
            if not port_open(service.port):
                return {"ok": True, "message": f"stop  {service.name}: stopped {len(pids)} listener(s)", "pids": pids}
            time.sleep(0.2)
        return {"ok": False, "message": f"fail  {service.name}: port {service.port} is still open", "pids": listener_pids(service.port)}

    return result(True, f"ok    {service.name}: no managed pid")


def restart_service(service: Service) -> dict:
    if service.kind == "launchd":
        return kickstart_launchd_service(service, "restart")
    stop_result = stop_service(service)
    if not stop_result.get("ok"):
        return {"ok": False, "message": f"restart {service.name}: stop failed: {stop_result.get('message')}", "stop": stop_result}
    port_stop_result = None
    if service.port and port_open(service.port):
        port_stop_result = stop_service(service, by_port=True)
        if not port_stop_result.get("ok"):
            return {
                "ok": False,
                "message": f"restart {service.name}: port {service.port} is still in use",
                "stop": stop_result,
                "portStop": port_stop_result,
            }
    start_result = start_service(service)
    message = f"restart {service.name}: {start_result['message']}"
    if start_result.get("ok"):
        record_event(service.name, "restarted", message, start_result.get("pid"))
    return {"ok": bool(start_result.get("ok")), "message": message, "stop": stop_result, "portStop": port_stop_result, "start": start_result}


def check_service(service: Service) -> dict:
    if not service.enabled:
        message = f"skip  {service.name}: disabled"
        record_event(service.name, "checked_skipped", message)
        return result(True, message, skipped=True)
    record_event(service.name, "checked", "check requested")
    if service.kind == "launchd":
        state, pid, detail = service_state(service)
        if state == "stopped":
            return start_launchd_service(service)
        return result(True, f"ok    {service.name}: launchd status {state} ({detail})", state=state, pid=pid)
    return start_service(service)


def request_stop(signum, _frame) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True
    print(f"received signal {signum}; shutting down supervisor", flush=True)


def supervise_services(names: list[str] | None, interval: int) -> None:
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    last_states: dict[str, str] = {}
    print(f"supervising {', '.join(names or ['all'])}; interval={interval}s", flush=True)
    while not STOP_REQUESTED:
        services = select_services(load_services(), names)
        enabled_services = [service for service in services if service.enabled]
        disabled = [service.name for service in services if not service.enabled]
        if disabled:
            print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} disabled: {', '.join(disabled)}", flush=True)
        for service in enabled_services:
            state, _pid, detail = service_state(service)
            current = f"{state}: {detail}"
            if last_states.get(service.name) != current:
                print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {service.name}: {current}", flush=True)
                last_states[service.name] = current
            if state in {"stopped", "unhealthy"}:
                start_service(service)
        time.sleep(interval)

    for service in select_services(load_services(), names):
        if service.enabled and service.kind == "process":
            stop_service(service)


def print_status(services: list[Service]) -> None:
    width = max([len(service.name) for service in services] + [4])
    for service in services:
        state, pid, detail = service_state(service)
        enabled = "enabled" if service.enabled else "disabled"
        port = f":{service.port}" if service.port else ""
        pid_text = f"pid {pid}" if pid else "-"
        print(f"{service.name:<{width}}  {service.kind:<7} {enabled:<8} {state:<9} {pid_text:<10} {port:<6} {detail}")


def tail_log_text(service: Service, lines: int) -> str:
    if service.kind == "launchd":
        paths = []
        for path in (service.stdout_path, service.stderr_path):
            if path and path not in paths:
                paths.append(path)
        if not paths:
            return f"No launchd log path configured for {service.name}\n"
        chunks = []
        for path in paths:
            if path.exists():
                data = path.read_text(encoding="utf-8", errors="replace").splitlines()
                chunks.append(f"==> {path} <==\n" + "\n".join(data[-lines:]))
            else:
                chunks.append(f"==> {path} <==\nNo log yet")
        return "\n\n".join(chunks) + "\n"
    if not service.log_file.exists():
        return f"No log yet: {service.log_file}\n"
    data = service.log_file.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(data[-lines:]) + ("\n" if data else "")


def tail_log(service: Service, lines: int) -> None:
    print(tail_log_text(service, lines), end="")


def build_launchd_plist() -> dict:
    python = sys.executable or shutil.which("python3") or "/usr/bin/python3"
    return {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [
            python,
            str(ROOT / "server_manager.py"),
            "supervise",
            "all",
            "--interval",
            str(DEFAULT_SUPERVISE_INTERVAL),
        ],
        "WorkingDirectory": str(ROOT),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(LOG_DIR / "launchd.out.log"),
        "StandardErrorPath": str(LOG_DIR / "launchd.err.log"),
        "EnvironmentVariables": {"PATH": DEFAULT_PATH},
    }


def build_web_launchd_plist(host: str = DEFAULT_WEB_HOST, port: int = DEFAULT_WEB_PORT) -> dict:
    python = sys.executable or shutil.which("python3") or "/usr/bin/python3"
    return {
        "Label": WEB_LAUNCHD_LABEL,
        "ProgramArguments": [
            python,
            str(ROOT / "server_manager.py"),
            "web",
            "--host",
            host,
            "--port",
            str(port),
        ],
        "WorkingDirectory": str(ROOT),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(LOG_DIR / "web-launchd.out.log"),
        "StandardErrorPath": str(LOG_DIR / "web-launchd.err.log"),
        "EnvironmentVariables": {"PATH": DEFAULT_PATH},
    }


def install_launchd() -> None:
    ensure_dirs()
    LAUNCHD_PLIST.parent.mkdir(parents=True, exist_ok=True)
    with LAUNCHD_PLIST.open("wb") as fh:
        plistlib.dump(build_launchd_plist(), fh)

    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(LAUNCHD_PLIST)], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    result = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(LAUNCHD_PLIST)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode != 0:
        print(result.stderr.strip() or result.stdout.strip())
        raise SystemExit(result.returncode)
    subprocess.run(["launchctl", "enable", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"], check=False)
    print(f"installed launchd agent: {LAUNCHD_PLIST}")
    print("it will run at login; it has also been loaded for this login session")


def install_web_launchd(host: str = DEFAULT_WEB_HOST, port: int = DEFAULT_WEB_PORT) -> None:
    ensure_dirs()
    WEB_LAUNCHD_PLIST.parent.mkdir(parents=True, exist_ok=True)
    with WEB_LAUNCHD_PLIST.open("wb") as fh:
        plistlib.dump(build_web_launchd_plist(host, port), fh)

    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(WEB_LAUNCHD_PLIST)], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    result = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(WEB_LAUNCHD_PLIST)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode != 0:
        print(result.stderr.strip() or result.stdout.strip())
        raise SystemExit(result.returncode)
    subprocess.run(["launchctl", "enable", f"gui/{os.getuid()}/{WEB_LAUNCHD_LABEL}"], check=False)
    print(f"installed web launchd agent: {WEB_LAUNCHD_PLIST}")
    shown_host = lan_ip_address() if host in {"", DEFAULT_WEB_HOST} else host
    print(f"web panel will run at login on http://{shown_host}:{port}")


def uninstall_launchd() -> None:
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(LAUNCHD_PLIST)], check=False)
    try:
        LAUNCHD_PLIST.unlink()
        print(f"removed launchd agent: {LAUNCHD_PLIST}")
    except FileNotFoundError:
        print(f"launchd agent not installed: {LAUNCHD_PLIST}")


def uninstall_web_launchd() -> None:
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(WEB_LAUNCHD_PLIST)], check=False)
    try:
        WEB_LAUNCHD_PLIST.unlink()
        print(f"removed web launchd agent: {WEB_LAUNCHD_PLIST}")
    except FileNotFoundError:
        print(f"web launchd agent not installed: {WEB_LAUNCHD_PLIST}")


def launchd_status_text() -> str:
    result = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode == 0:
        return result.stdout
    return result.stderr.strip() or f"{LAUNCHD_LABEL} is not loaded"


def web_launchd_status_text() -> str:
    result = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{WEB_LAUNCHD_LABEL}"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode == 0:
        return result.stdout
    return result.stderr.strip() or f"{WEB_LAUNCHD_LABEL} is not loaded"


def launchd_status() -> None:
    print(launchd_status_text())


def web_launchd_status() -> None:
    print(web_launchd_status_text())


def normalize_power_time(value: str | None) -> str:
    if not value:
        raise ValueError("time is required")
    match = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", value.strip())
    if not match:
        raise ValueError("time must use HH:MM or HH:MM:SS")
    hour = int(match.group(1))
    minute = int(match.group(2))
    second = int(match.group(3) or "0")
    if hour > 23 or minute > 59 or second > 59:
        raise ValueError("time is out of range")
    return f"{hour:02d}:{minute:02d}:{second:02d}"


def normalize_power_days(value) -> str:
    if value in (None, "", "all", "everyday"):
        return POWER_DAYS
    if isinstance(value, str):
        days = value.upper().replace(",", "").replace(" ", "")
    elif isinstance(value, list):
        days = "".join(str(part).upper() for part in value)
    else:
        raise ValueError("days must be an array or weekday string")
    normalized = "".join(day for day in POWER_DAYS if day in days)
    if not normalized:
        raise ValueError("choose at least one day")
    return normalized


def display_power_days(days: str) -> str:
    if days == POWER_DAYS:
        return "every day"
    return ", ".join(POWER_DAY_LABELS[day] for day in POWER_DAYS if day in days)


def parse_pmset_restart_line(line: str) -> dict:
    parsed = {"enabled": True, "time": "05:00", "timeWithSeconds": "05:00:00", "days": list(POWER_DAYS)}
    match = re.search(
        r"restart\s+at\s+(\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM)?)\s+(.+)$",
        line,
        re.IGNORECASE,
    )
    if not match:
        return parsed

    raw_time = match.group(1).strip().upper().replace(" ", "")
    for fmt in ("%I:%M%p", "%I:%M:%S%p", "%H:%M", "%H:%M:%S"):
        try:
            dt = datetime.strptime(raw_time, fmt)
            parsed["time"] = dt.strftime("%H:%M")
            parsed["timeWithSeconds"] = dt.strftime("%H:%M:%S")
            break
        except ValueError:
            pass

    raw_days = match.group(2).strip().lower().removeprefix("on ").strip()
    if raw_days != "every day":
        named_days = [POWER_DAY_NAMES[name] for name in POWER_DAY_NAMES if name in raw_days]
        days = normalize_power_days(named_days or raw_days.upper())
        parsed["days"] = list(days)
    return parsed


def load_power_interval_config() -> dict | None:
    try:
        data = json.loads(POWER_INTERVAL_CONFIG.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("mode") != "interval":
        return None
    return data


def save_power_interval_config(config: dict) -> None:
    ensure_dirs()
    POWER_INTERVAL_CONFIG.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def interval_power_target(config: dict) -> datetime:
    return datetime.fromisoformat(f"{config['nextDate']}T{config['time']}")


def advance_power_interval(config: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now()
    interval_days = int(config["intervalDays"])
    target = interval_power_target(config)
    while target <= now:
        target += timedelta(days=interval_days)
    updated = dict(config)
    updated["nextDate"] = target.date().isoformat()
    updated["updatedAt"] = now_iso()
    return updated


def build_power_interval_launchd_plist() -> dict:
    python = sys.executable or shutil.which("python3") or "/usr/bin/python3"
    return {
        "Label": POWER_INTERVAL_LABEL,
        "ProgramArguments": [python, str(ROOT / "server_manager.py"), "power-interval-sync"],
        "WorkingDirectory": str(ROOT),
        "RunAtLoad": True,
        "StartInterval": 1800,
        "ProcessType": "Background",
        "StandardOutPath": str(LOG_DIR / "power-interval.out.log"),
        "StandardErrorPath": str(LOG_DIR / "power-interval.err.log"),
        "EnvironmentVariables": {"PATH": DEFAULT_PATH},
    }


def install_power_interval_launchd() -> None:
    ensure_dirs()
    POWER_INTERVAL_PLIST.parent.mkdir(parents=True, exist_ok=True)
    with POWER_INTERVAL_PLIST.open("wb") as handle:
        plistlib.dump(build_power_interval_launchd_plist(), handle)
    domain = f"gui/{os.getuid()}"
    subprocess.run(
        ["launchctl", "bootout", domain, str(POWER_INTERVAL_PLIST)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    result_obj = subprocess.run(
        ["launchctl", "bootstrap", domain, str(POWER_INTERVAL_PLIST)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result_obj.returncode != 0:
        output = (result_obj.stderr or result_obj.stdout or "").strip()
        raise RuntimeError(output or f"launchctl bootstrap failed with code {result_obj.returncode}")
    subprocess.run(["launchctl", "enable", f"{domain}/{POWER_INTERVAL_LABEL}"], check=False)


def remove_power_interval_schedule() -> None:
    domain = f"gui/{os.getuid()}"
    subprocess.run(
        ["launchctl", "bootout", domain, str(POWER_INTERVAL_PLIST)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    POWER_INTERVAL_PLIST.unlink(missing_ok=True)
    POWER_INTERVAL_CONFIG.unlink(missing_ok=True)


def sync_power_interval_schedule() -> dict:
    config = load_power_interval_config()
    if not config:
        raise RuntimeError("power interval schedule is not configured")
    updated = advance_power_interval(config)
    if updated != config:
        save_power_interval_config(updated)
    target = interval_power_target(updated)
    weekday = POWER_DAYS[target.weekday()]
    result_obj = run_pmset_repeat(["restart", weekday, updated["time"]])
    if result_obj.returncode != 0:
        output = (result_obj.stderr or result_obj.stdout or "").strip()
        raise RuntimeError(output or f"pmset repeat restart failed with code {result_obj.returncode}")
    message = (
        f"next restart {updated['nextDate']} at {updated['time'][:5]}; "
        f"every {updated['intervalDays']} days"
    )
    print(message)
    return {**updated, "message": message, "weekday": weekday}


def power_schedule_status() -> dict:
    pmset = shutil.which("pmset")
    if not pmset:
        return {"summary": "pmset not available", "raw": "", "lines": [], "enabled": False, "time": "05:00", "days": list(POWER_DAYS)}
    result = subprocess.run([pmset, "-g", "sched"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    raw = result.stdout or result.stderr
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    interval_config = load_power_interval_config()
    if interval_config:
        target = interval_power_target(interval_config)
        return {
            "summary": (
                f"restart at {interval_config['time'][:5]} every {interval_config['intervalDays']} days; "
                f"next {interval_config['nextDate']}"
            ),
            "raw": raw,
            "lines": lines,
            "enabled": True,
            "mode": "interval",
            "time": interval_config["time"][:5],
            "timeWithSeconds": interval_config["time"],
            "days": [POWER_DAYS[target.weekday()]],
            "daySummary": f"every {interval_config['intervalDays']} days",
            "intervalDays": interval_config["intervalDays"],
            "nextDate": interval_config["nextDate"],
        }
    summary = "no repeating restart schedule found"
    structured = {"enabled": False, "time": "05:00", "timeWithSeconds": "05:00:00", "days": list(POWER_DAYS)}
    for line in lines:
        if "restart" in line.lower():
            summary = line
            structured = parse_pmset_restart_line(line)
            break
    structured["daySummary"] = display_power_days("".join(structured["days"]))
    return {"summary": summary, "raw": raw, "lines": lines, "mode": "weekdays", **structured}


def run_pmset_repeat(args: list[str]) -> subprocess.CompletedProcess:
    pmset = shutil.which("pmset")
    if not pmset:
        return subprocess.CompletedProcess(["pmset", *args], 1, "", "pmset not available")
    result = subprocess.run([pmset, "repeat", *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode == 0:
        return result
    sudo = shutil.which("sudo")
    if not sudo:
        return result
    return subprocess.run([sudo, "-n", pmset, "repeat", *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)


def set_power_schedule(payload: dict) -> dict:
    enabled = bool(payload.get("enabled", True))
    if not enabled:
        result_obj = run_pmset_repeat(["cancel"])
        if result_obj.returncode != 0:
            output = (result_obj.stderr or result_obj.stdout or "").strip()
            raise ValueError(output or f"pmset repeat cancel failed with code {result_obj.returncode}")
        remove_power_interval_schedule()
        return {"ok": True, "message": "system restart schedule disabled", "powerSchedule": power_schedule_status()}

    time_value = normalize_power_time(str(payload.get("time", "")).strip())
    mode = str(payload.get("mode") or "weekdays")
    if mode == "interval":
        try:
            interval_days = int(payload.get("intervalDays", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("interval days must be a number") from exc
        if interval_days < 1 or interval_days > 365:
            raise ValueError("interval days must be between 1 and 365")
        start_date = str(payload.get("startDate") or "").strip()
        if not start_date:
            start_date = (datetime.now() + timedelta(days=1)).date().isoformat()
        try:
            target = datetime.fromisoformat(f"{start_date}T{time_value}")
        except ValueError as exc:
            raise ValueError("start date is invalid") from exc
        if target <= datetime.now():
            raise ValueError("first restart must be in the future")
        config = {
            "mode": "interval",
            "intervalDays": interval_days,
            "time": time_value,
            "nextDate": start_date,
            "updatedAt": now_iso(),
        }
        save_power_interval_config(config)
        try:
            synced = sync_power_interval_schedule()
            install_power_interval_launchd()
        except (OSError, RuntimeError) as exc:
            remove_power_interval_schedule()
            raise ValueError(f"could not install interval schedule: {exc}") from exc
        return {
            "ok": True,
            "message": synced["message"],
            "powerSchedule": power_schedule_status(),
        }
    if mode != "weekdays":
        raise ValueError("schedule mode must be weekdays or interval")
    remove_power_interval_schedule()
    days = normalize_power_days(payload.get("days"))
    result_obj = run_pmset_repeat(["restart", days, time_value])
    if result_obj.returncode != 0:
        output = (result_obj.stderr or result_obj.stdout or "").strip()
        raise ValueError(output or f"pmset repeat restart failed with code {result_obj.returncode}")
    return {
        "ok": True,
        "message": f"system restart scheduled at {time_value[:5]} on {display_power_days(days)}",
        "powerSchedule": power_schedule_status(),
    }


def service_payload(service: Service, event_summary: dict[str, dict[str, str]], lan_ip: str) -> dict:
    state, pid, detail = service_state(service)
    events = event_summary.get(service.name, {})
    payload = {
        "name": service.name,
        "description": service.description,
        "kind": service.kind,
        "cwd": str(service.cwd) if service.cwd else "",
        "command": list(service.command),
        "commandText": shlex.join(service.command) if service.command else "",
        "enabled": service.enabled,
        "port": service.port,
        "extraPorts": service.extra_ports or [],
        "url": url_with_lan_ip(service.url, lan_ip),
        "state": state,
        "pid": pid,
        "detail": detail,
        "logPath": str(service.log_file),
        "launchdLabel": service.launchd_label,
        "launchdDomain": service.launchd_domain,
        "launchdAutoStart": service.launchd_auto_start,
        "launchdPlist": str(service.launchd_plist) if service.launchd_plist else "",
        "stdoutPath": str(service.stdout_path) if service.stdout_path else "",
        "stderrPath": str(service.stderr_path) if service.stderr_path else "",
        "startWaitSeconds": service.start_wait_seconds,
        "createdAt": service.created_at,
        "updatedAt": service.updated_at,
    }
    payload.update(events)
    return payload


def status_payload() -> dict:
    services = load_services()
    events = latest_service_events()
    launchd_raw = launchd_status_text()
    lan_ip = lan_ip_address()
    return {
        "services": [service_payload(service, events, lan_ip) for service in services.values()],
        "lanIp": lan_ip,
        "bindHost": DEFAULT_SERVICE_HOST,
        "powerSchedule": power_schedule_status(),
        "supervisor": parse_launchd_status(launchd_raw),
        "eventLog": str(EVENTS_FILE),
        "now": now_iso(),
    }


def load_launchd_plist(path: Path) -> dict:
    if not path.is_absolute():
        raise ValueError("plist 路徑必須是絕對路徑")
    if not path.is_file():
        raise ValueError(f"找不到 plist：{path}")
    try:
        with path.open("rb") as handle:
            data = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException) as exc:
        raise ValueError(f"無法讀取 plist：{exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("plist 內容格式不正確")
    return data


def infer_launchd_domain(path: Path) -> str:
    if path.parent in {Path("/Library/LaunchDaemons"), Path("/System/Library/LaunchDaemons")}:
        return "system"
    return "gui"


def launchd_suggestion(plist_raw: str) -> dict:
    if not plist_raw.strip():
        raise ValueError("請先填寫 plist 路徑")
    plist_path = Path(plist_raw).expanduser()
    data = load_launchd_plist(plist_path)
    label = str(data.get("Label") or "").strip()
    if not label:
        raise ValueError("plist 內沒有 Label")

    arguments = data.get("ProgramArguments")
    if isinstance(arguments, list):
        command = [str(part) for part in arguments]
    elif data.get("Program"):
        command = [str(data["Program"])]
    else:
        command = []

    keep_alive = data.get("KeepAlive", False)
    return {
        "detected": True,
        "kind": "launchd",
        "name": label.rsplit(".", 1)[-1],
        "launchdPlist": str(plist_path),
        "launchdLabel": label,
        "launchdDomain": infer_launchd_domain(plist_path),
        "launchdAutoStart": bool(data.get("RunAtLoad", False) or keep_alive),
        "cwd": str(data.get("WorkingDirectory") or ""),
        "commandText": shlex.join(command) if command else "",
        "stdoutPath": str(data.get("StandardOutPath") or ""),
        "stderrPath": str(data.get("StandardErrorPath") or ""),
        "reason": f"已讀取 {plist_path.name}",
    }


def process_suggestion(cwd_raw: str) -> dict:
    if not cwd_raw.strip():
        raise ValueError("請先填寫專案資料夾")
    cwd = Path(cwd_raw).expanduser()
    if not cwd.is_absolute():
        raise ValueError("專案資料夾必須是絕對路徑")
    if not cwd.is_dir():
        raise ValueError(f"找不到專案資料夾：{cwd}")

    suggestion = {
        "detected": False,
        "kind": "process",
        "name": cwd.name,
        "cwd": str(cwd),
        "commandText": "",
        "port": None,
        "reason": "找不到常見啟動檔，請手動填寫啟動指令",
    }

    package_path = cwd / "package.json"
    if package_path.is_file():
        try:
            package = json.loads(package_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            package = {}
        if isinstance(package, dict):
            package_name = str(package.get("name") or "").strip()
            if package_name:
                suggestion["name"] = package_name
            scripts = package.get("scripts")
            scripts = scripts if isinstance(scripts, dict) else {}
            script_name = "start" if scripts.get("start") else "dev" if scripts.get("dev") else ""
            if script_name:
                package_manager = "npm"
                declared_manager = str(package.get("packageManager") or "").split("@", 1)[0]
                if declared_manager in {"npm", "pnpm", "yarn", "bun"}:
                    package_manager = declared_manager
                elif (cwd / "pnpm-lock.yaml").exists():
                    package_manager = "pnpm"
                elif (cwd / "yarn.lock").exists():
                    package_manager = "yarn"
                elif (cwd / "bun.lock").exists() or (cwd / "bun.lockb").exists():
                    package_manager = "bun"
                suggestion.update(
                    detected=True,
                    commandText=f"{package_manager} run {script_name}",
                    reason=f"從 package.json 找到 {script_name} 指令",
                )
                return suggestion

    if (cwd / "manage.py").is_file():
        suggestion.update(
            detected=True,
            commandText=f"python3 manage.py runserver {DEFAULT_SERVICE_HOST}:8000",
            port=8000,
            reason="偵測到 Django manage.py",
        )
        return suggestion

    for filename in ("main.py", "app.py", "server.py"):
        candidate = cwd / filename
        if not candidate.is_file():
            continue
        try:
            source_head = candidate.read_text(encoding="utf-8", errors="replace")[:200_000]
        except OSError:
            source_head = ""
        module = candidate.stem
        if "FastAPI(" in source_head:
            suggestion.update(
                detected=True,
                commandText=f"python3 -m uvicorn {module}:app --host {DEFAULT_SERVICE_HOST} --port 8000",
                port=8000,
                reason=f"偵測到 {filename} 的 FastAPI app",
            )
        else:
            suggestion.update(
                detected=True,
                commandText=f"python3 {filename}",
                reason=f"偵測到 {filename}",
            )
        return suggestion

    if (cwd / "start.sh").is_file():
        suggestion.update(
            detected=True,
            commandText="./start.sh",
            reason="偵測到 start.sh",
        )
        return suggestion

    compose_name = next(
        (name for name in ("compose.yml", "compose.yaml", "docker-compose.yml", "docker-compose.yaml") if (cwd / name).is_file()),
        "",
    )
    if compose_name:
        suggestion.update(
            detected=True,
            commandText="docker compose up",
            reason=f"偵測到 {compose_name}",
        )
    return suggestion


def service_suggestion(payload: dict) -> dict:
    kind = str(payload.get("kind") or "process")
    if kind == "process":
        return process_suggestion(str(payload.get("cwd") or ""))
    if kind == "launchd":
        return launchd_suggestion(str(payload.get("launchdPlist") or ""))
    raise ValueError("不支援的服務類型")


def validate_service_payload(payload: dict, services: dict[str, Service], existing: Service | None = None) -> Service:
    kind = payload.get("kind", existing.kind if existing else "process")
    if kind not in {"process", "launchd"}:
        raise ValueError("不支援的服務類型")

    if kind == "launchd":
        plist_value = payload.get(
            "launchdPlist",
            str(existing.launchd_plist) if existing and existing.launchd_plist else "",
        )
        launchd_plist = Path(str(plist_value)).expanduser() if plist_value else None
        plist_data = load_launchd_plist(launchd_plist) if launchd_plist else {}
        label = payload.get("launchdLabel") or plist_data.get("Label") or (existing.launchd_label if existing else "")
        if not label:
            raise ValueError("請填寫 plist 路徑或 Launchd Label")
        raw_name = payload.get("name") or (existing.name if existing else str(label).rsplit(".", 1)[-1])
        name = unique_service_name(str(raw_name), services, original=existing.name if existing else None)
        url = payload.get("url", existing.url if existing else None)
        if url == "":
            url = None
        url = url_with_lan_ip(url)
        cwd_raw = (
            payload.get("cwd")
            or plist_data.get("WorkingDirectory")
            or (str(existing.cwd) if existing and existing.cwd else "")
        )
        cwd = Path(str(cwd_raw)).expanduser() if cwd_raw else None
        if cwd and (not cwd.is_absolute() or not cwd.exists()):
            raise ValueError(f"工作資料夾不存在或不是絕對路徑：{cwd}")
        created_at = existing.created_at if existing and existing.created_at else now_iso()
        start_wait_seconds = int(payload.get("startWaitSeconds", existing.start_wait_seconds if existing else 15) or 0)
        domain = (
            payload.get("launchdDomain")
            or (infer_launchd_domain(launchd_plist) if launchd_plist else None)
            or (existing.launchd_domain if existing else "gui")
        )
        if domain not in {"gui", "system"} and "/" not in str(domain):
            raise ValueError("Launchd Domain 必須是 gui 或 system")
        stdout_value = payload.get("stdoutPath") or plist_data.get("StandardOutPath")
        stderr_value = payload.get("stderrPath") or plist_data.get("StandardErrorPath")
        if "launchdAutoStart" in payload:
            launchd_auto_start = bool(payload["launchdAutoStart"])
        elif existing:
            launchd_auto_start = existing.launchd_auto_start
        else:
            launchd_auto_start = bool(plist_data.get("RunAtLoad") or plist_data.get("KeepAlive"))
        return Service(
            name=name,
            description=str(payload.get("description", existing.description if existing else "") or ""),
            cwd=cwd,
            command=[],
            kind="launchd",
            port=normalize_port(payload.get("primaryPort", payload.get("port", existing.port if existing else None))),
            extra_ports=normalize_ports(payload.get("extraPorts", existing.extra_ports if existing else [])),
            url=url,
            launchd_label=str(label),
            launchd_domain=str(domain),
            launchd_auto_start=launchd_auto_start,
            launchd_plist=launchd_plist,
            stdout_path=Path(str(stdout_value)).expanduser() if stdout_value else (existing.stdout_path if existing else None),
            stderr_path=Path(str(stderr_value)).expanduser() if stderr_value else (existing.stderr_path if existing else None),
            start_wait_seconds=max(0, start_wait_seconds),
            enabled=bool(payload.get("enabled", existing.enabled if existing else True)),
            created_at=created_at,
            updated_at=now_iso(),
        )

    if existing:
        raw_name = payload.get("name", existing.name)
        name = unique_service_name(str(raw_name), services, original=existing.name)
        cwd_raw = payload.get("cwd", str(existing.cwd))
        command = parse_command_payload(payload, existing.command)
        description = payload.get("description", existing.description)
        port = normalize_port(payload.get("port", existing.port))
        url = payload.get("url", existing.url)
        enabled = bool(payload.get("enabled", existing.enabled))
        created_at = existing.created_at or now_iso()
        start_wait_seconds = int(payload.get("startWaitSeconds", existing.start_wait_seconds) or 0)
    else:
        raw_name = payload.get("name") or Path(str(payload.get("cwd", ""))).name
        name = unique_service_name(str(raw_name), services)
        cwd_raw = payload.get("cwd")
        command = parse_command_payload(payload)
        description = payload.get("description", "")
        port = normalize_port(payload.get("port"))
        url = payload.get("url")
        enabled = bool(payload.get("enabled", True))
        created_at = now_iso()
        start_wait_seconds = int(payload.get("startWaitSeconds", 2) or 0)

    if not cwd_raw:
        raise ValueError("請填寫專案資料夾")
    cwd = Path(str(cwd_raw)).expanduser()
    if not cwd.is_absolute():
        raise ValueError("專案資料夾必須是絕對路徑")
    if not cwd.exists():
        raise ValueError(f"找不到專案資料夾：{cwd}")
    if url == "":
        url = None
    url = url_with_lan_ip(url)
    return Service(
        name=name,
        description=str(description or ""),
        cwd=cwd,
        command=command,
        port=port,
        extra_ports=normalize_ports(payload.get("extraPorts", existing.extra_ports if existing else [])),
        url=url,
        env=existing.env if existing else None,
        start_wait_seconds=max(0, start_wait_seconds),
        enabled=enabled,
        created_at=created_at,
        updated_at=now_iso(),
    )


def capture_operation(func, *args, **kwargs) -> dict:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        payload = func(*args, **kwargs)
    if not isinstance(payload, dict):
        payload = {"ok": True, "message": buffer.getvalue().strip()}
    output = buffer.getvalue()
    if output:
        payload["output"] = output
    return payload


def read_json_body(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return {}
    data = handler.rfile.read(length)
    if not data:
        return {}
    try:
        return json.loads(data.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON body: {exc}") from exc


def send_json(handler: BaseHTTPRequestHandler, payload: dict, status: int = 200) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def send_text(handler: BaseHTTPRequestHandler, body: str, content_type: str = "text/plain; charset=utf-8", status: int = 200) -> None:
    encoded = body.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(encoded)))
    handler.end_headers()
    handler.wfile.write(encoded)


INDEX_HTML = """<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Server Manager</title>
  <link rel="stylesheet" href="/styles.css">
</head>
<body>
  <main id="app">
    <header class="topbar">
      <div>
        <h1>Server Manager</h1>
        <p id="subtitle">Server manager panel</p>
      </div>
      <button id="refreshBtn" type="button">Refresh</button>
    </header>
    <section id="summary" class="summary"></section>
    <section class="panel power-panel">
      <div class="panel-head">
        <h2>System restart</h2>
        <button id="savePowerBtn" type="submit" form="powerForm">Save</button>
      </div>
      <form id="powerForm" class="power-form">
        <label class="checkline"><input id="powerEnabled" type="checkbox"> Enabled</label>
        <label>Time<input id="powerTime" type="time" step="60"></label>
        <label>Schedule
          <select id="powerMode">
            <option value="weekdays">Weekdays</option>
            <option value="interval">Every N days</option>
          </select>
        </label>
        <div id="powerWeekdayFields" class="power-mode-fields">
          <span class="muted">Days</span>
          <div class="weekday-row" id="powerDays">
            <label class="checkline"><input type="checkbox" value="M"> Mon</label>
            <label class="checkline"><input type="checkbox" value="T"> Tue</label>
            <label class="checkline"><input type="checkbox" value="W"> Wed</label>
            <label class="checkline"><input type="checkbox" value="R"> Thu</label>
            <label class="checkline"><input type="checkbox" value="F"> Fri</label>
            <label class="checkline"><input type="checkbox" value="S"> Sat</label>
            <label class="checkline"><input type="checkbox" value="U"> Sun</label>
          </div>
        </div>
        <div id="powerIntervalFields" class="power-mode-fields interval-fields" hidden>
          <label>Interval days<input id="powerIntervalDays" type="number" min="1" max="365" value="3"></label>
          <label>First restart<input id="powerStartDate" type="date"></label>
        </div>
        <div id="powerRaw" class="muted"></div>
      </form>
    </section>
    <section class="layout">
      <div>
        <section class="panel">
          <div class="panel-head">
            <div>
              <h2 id="formTitle">新增服務</h2>
              <p id="formModeHint" class="muted">選擇類型後，只需填寫基本資料。</p>
            </div>
            <button id="resetFormBtn" type="button" class="ghost">重新填寫</button>
          </div>
          <form id="serviceForm" class="service-form">
            <input type="hidden" id="originalName">

            <fieldset class="form-block kind-block">
              <legend>服務類型</legend>
              <div class="segmented" role="radiogroup" aria-label="服務類型">
                <label>
                  <input type="radio" name="kind" value="process" checked>
                  <span>一般程式或網站</span>
                </label>
                <label>
                  <input type="radio" name="kind" value="launchd">
                  <span>macOS 背景服務</span>
                </label>
              </div>
            </fieldset>

            <section class="form-section" data-kind-section="process">
              <h3>啟動資料</h3>
              <label class="field"><span class="field-label">專案資料夾 <span class="required">必填</span></span>
                <div class="field-with-action">
                  <input id="processCwd" name="processCwd" placeholder="/Users/你的名稱/Documents/my-server" autocomplete="off">
                  <button id="detectProcessBtn" type="button" class="ghost">自動偵測</button>
                </div>
              </label>
              <label class="field"><span class="field-label">啟動指令 <span class="required">必填</span></span>
                <input id="commandText" name="commandText" placeholder="例如 npm run start 或 python3 main.py" autocomplete="off">
              </label>
              <div id="processDetectionStatus" class="detection-status" hidden></div>
            </section>

            <section class="form-section" data-kind-section="launchd" hidden>
              <h3>Launchd 設定</h3>
              <label class="field"><span class="field-label">plist 檔案路徑 <span class="required">必填</span></span>
                <div class="field-with-action">
                  <input id="launchdPlist" name="launchdPlist" placeholder="/Users/你的名稱/Library/LaunchAgents/com.example.service.plist" autocomplete="off">
                  <button id="detectLaunchdBtn" type="button" class="ghost">讀取設定</button>
                </div>
              </label>
              <div id="launchdDetectionStatus" class="detection-status" hidden></div>
            </section>

            <section class="form-section">
              <h3>顯示資料</h3>
              <label class="field">服務名稱
                <input id="name" name="name" autocomplete="off" placeholder="留空會自動命名">
              </label>
              <label class="field">備註
                <input id="description" name="description" placeholder="例如：公司網站 API">
              </label>
            </section>

            <details id="advancedSettings" class="advanced-settings">
              <summary>進階設定</summary>
              <div class="advanced-body">
                <div class="form-row">
                  <label>主要連接埠
                    <input id="port" name="port" inputmode="numeric" min="1" max="65535" placeholder="例如 8000">
                  </label>
                  <label>啟動等待秒數
                    <input id="startWaitSeconds" name="startWaitSeconds" type="number" min="0" max="300" step="1" value="15">
                  </label>
                </div>
                <label>服務網址
                  <input id="url" name="url" placeholder="http://192.168.x.x:8000">
                </label>
                <label>其他連接埠
                  <input id="extraPorts" name="extraPorts" placeholder="例如 8080, 18110">
                </label>

                <div data-kind-advanced="launchd" hidden>
                  <div class="form-row">
                    <label>Launchd Label
                      <input id="launchdLabel" name="launchdLabel" placeholder="讀取 plist 後自動填寫">
                    </label>
                    <label>執行範圍
                      <select id="launchdDomain" name="launchdDomain">
                        <option value="">自動判斷</option>
                        <option value="gui">目前使用者</option>
                        <option value="system">整部電腦</option>
                      </select>
                    </label>
                  </div>
                  <label>工作資料夾
                    <input id="launchdCwd" name="launchdCwd" placeholder="通常由 plist 自動填寫">
                  </label>
                  <label>標準輸出日誌
                    <input id="stdoutPath" name="stdoutPath" placeholder="/tmp/service.out.log">
                  </label>
                  <label>錯誤日誌
                    <input id="stderrPath" name="stderrPath" placeholder="/tmp/service.err.log">
                  </label>
                  <label class="checkline">
                    <input id="launchdAutoStart" name="launchdAutoStart" type="checkbox">
                    讓 launchd 自行在登入或開機時啟動
                  </label>
                </div>
              </div>
            </details>

            <div class="toggle-stack">
              <label class="checkline">
                <input id="enabled" name="enabled" type="checkbox" checked>
                交給 Project Server 持續監管
              </label>
              <label class="checkline">
                <input id="startAfterSave" name="startAfterSave" type="checkbox" checked>
                儲存後立即啟動
              </label>
            </div>
            <div id="formError" class="inline-error" role="alert" hidden></div>
            <div class="form-actions">
              <button id="saveServiceBtn" type="submit">新增並啟動</button>
              <button id="cancelEditBtn" type="button" class="ghost" hidden>取消編輯</button>
            </div>
          </form>
        </section>
        <section class="panel">
          <div class="panel-head">
            <h2>Log viewer</h2>
            <button id="refreshLogBtn" type="button" class="ghost">Refresh log</button>
          </div>
          <div id="logMeta" class="muted">Select a server.</div>
          <pre id="logs" class="logs"></pre>
        </section>
      </div>
      <div>
        <section>
          <h2>Enabled</h2>
          <div id="enabledServices" class="service-list"></div>
        </section>
        <section>
          <h2>Disabled</h2>
          <div id="disabledServices" class="service-list"></div>
        </section>
      </div>
    </section>
    <div id="toast" class="toast" hidden></div>
  </main>
  <script src="/app.js"></script>
</body>
</html>
"""


STYLE_CSS = """
:root {
  color-scheme: light;
  --bg: #f6f7f9;
  --panel: #ffffff;
  --ink: #17202a;
  --muted: #687385;
  --line: #d8dde6;
  --accent: #146c94;
  --ok: #0f7a4f;
  --warn: #a36200;
  --bad: #a83232;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-size: 14px;
}
main { max-width: 1440px; margin: 0 auto; padding: 22px; }
h1, h2, h3, p { margin: 0; }
h1 { font-size: 24px; font-weight: 720; }
h2 { font-size: 17px; margin: 0 0 12px; }
h3 { font-size: 15px; }
button {
  border: 1px solid var(--accent);
  background: var(--accent);
  color: white;
  border-radius: 6px;
  min-height: 34px;
  padding: 0 12px;
  font: inherit;
  cursor: pointer;
}
button:hover { filter: brightness(0.96); }
button:disabled { cursor: wait; opacity: .58; }
button.ghost {
  background: #fff;
  color: var(--accent);
}
button.danger {
  border-color: var(--bad);
  background: #fff;
  color: var(--bad);
}
button.small { min-height: 30px; padding: 0 9px; }
input, select {
  width: 100%;
  min-height: 36px;
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 7px 9px;
  font: inherit;
  background: white;
}
input:focus, select:focus {
  border-color: var(--accent);
  outline: 3px solid rgba(20, 108, 148, .12);
}
label { display: grid; gap: 5px; color: var(--muted); font-size: 12px; }
.topbar, .panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.topbar { margin-bottom: 16px; }
.muted, #subtitle { color: var(--muted); }
.summary {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
  margin-bottom: 16px;
}
.metric, .panel, .service-card {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
}
.metric { padding: 12px; min-height: 74px; }
.metric span { display: block; color: var(--muted); font-size: 12px; margin-bottom: 6px; }
.metric strong { font-size: 16px; line-height: 1.35; overflow-wrap: anywhere; }
.layout {
  display: grid;
  grid-template-columns: minmax(380px, 500px) minmax(0, 1fr);
  gap: 16px;
  align-items: start;
}
.panel { padding: 14px; margin-bottom: 16px; }
.service-form { display: grid; gap: 14px; }
.form-block {
  border: 0;
  margin: 0;
  padding: 0;
}
.form-block legend {
  margin-bottom: 7px;
  color: var(--muted);
  font-size: 12px;
}
.segmented {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  border: 1px solid var(--line);
  border-radius: 6px;
  overflow: hidden;
}
.segmented label {
  display: block;
  position: relative;
  color: var(--ink);
  font-size: 13px;
}
.segmented label + label { border-left: 1px solid var(--line); }
.segmented input {
  position: absolute;
  width: 1px;
  min-height: 1px;
  opacity: 0;
  pointer-events: none;
}
.segmented span {
  display: grid;
  place-items: center;
  min-height: 40px;
  padding: 7px 10px;
  text-align: center;
  cursor: pointer;
}
.segmented input:checked + span {
  background: #e8f3f7;
  color: #0c5e7d;
  font-weight: 650;
  box-shadow: inset 0 0 0 1px var(--accent);
}
.segmented input:focus-visible + span {
  outline: 3px solid rgba(20, 108, 148, .18);
  outline-offset: -3px;
}
.form-section {
  display: grid;
  gap: 10px;
  padding-top: 13px;
  border-top: 1px solid var(--line);
}
.form-section h3 {
  color: var(--ink);
  font-size: 13px;
}
.field-label { color: var(--muted); }
.required {
  color: var(--bad);
  font-size: 11px;
  margin-left: 4px;
}
.field-with-action {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 92px;
  gap: 8px;
}
.field-with-action button {
  width: 92px;
  padding: 0 8px;
}
.detection-status {
  border-left: 3px solid var(--ok);
  padding: 7px 9px;
  background: #edf8f3;
  color: #155f43;
  font-size: 12px;
  line-height: 1.45;
}
.detection-status.error {
  border-left-color: var(--bad);
  background: #fff1f1;
  color: var(--bad);
}
.detection-status.notice {
  border-left-color: var(--warn);
  background: #fff7e7;
  color: #7a4d00;
}
.advanced-settings {
  border-top: 1px solid var(--line);
  border-bottom: 1px solid var(--line);
}
.advanced-settings summary {
  cursor: pointer;
  padding: 11px 2px;
  color: var(--accent);
  font-weight: 650;
}
.advanced-body {
  display: grid;
  gap: 10px;
  padding: 2px 0 13px;
}
.toggle-stack {
  display: grid;
  gap: 9px;
}
.form-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.form-actions button { min-width: 120px; }
.inline-error {
  border: 1px solid #e3a1a1;
  border-radius: 6px;
  padding: 9px 10px;
  background: #fff1f1;
  color: var(--bad);
  line-height: 1.45;
}
[hidden] { display: none !important; }
.power-form {
  display: grid;
  grid-template-columns: 120px 140px minmax(0, 1fr);
  gap: 12px;
  align-items: end;
}
.power-form .muted {
  grid-column: 1 / -1;
  overflow-wrap: anywhere;
}
.power-mode-fields {
  grid-column: 1 / -1;
  display: flex;
  flex-wrap: wrap;
  gap: 10px 16px;
  align-items: end;
}
.power-mode-fields > .muted { min-width: 44px; }
.interval-fields label { min-width: 180px; }
.weekday-row {
  display: flex;
  flex-wrap: wrap;
  gap: 10px 12px;
  align-items: center;
}
.form-row { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
.checkline {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
  color: var(--ink);
}
.checkline input { width: 18px; min-height: 18px; }
.service-list { display: grid; gap: 10px; margin-bottom: 16px; }
.service-card { padding: 13px; }
.service-top {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 9px;
}
.service-title { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.badge {
  display: inline-flex;
  align-items: center;
  min-height: 22px;
  border-radius: 999px;
  padding: 0 8px;
  font-size: 12px;
  border: 1px solid var(--line);
  color: var(--muted);
}
.badge.managed { color: var(--ok); border-color: #9bd4bd; background: #edf8f3; }
.badge.running { color: var(--ok); border-color: #9bd4bd; background: #edf8f3; }
.badge.scheduled { color: var(--accent); border-color: #a3c8dc; background: #eef8fc; }
.badge.external { color: var(--warn); border-color: #e8c47d; background: #fff7e7; }
.badge.stopped { color: var(--bad); border-color: #e3a1a1; background: #fff1f1; }
.details {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 6px 12px;
  color: var(--muted);
  margin: 8px 0 10px;
}
.details div { overflow-wrap: anywhere; }
.actions { display: flex; flex-wrap: wrap; gap: 8px; }
.logs {
  min-height: 360px;
  max-height: 520px;
  overflow: auto;
  background: #101418;
  color: #dce7ef;
  border-radius: 6px;
  padding: 12px;
  white-space: pre-wrap;
  font-size: 12px;
}
.toast {
  position: fixed;
  right: 18px;
  bottom: 18px;
  max-width: 420px;
  padding: 12px 14px;
  border-radius: 8px;
  background: #17202a;
  color: white;
  box-shadow: 0 12px 30px rgba(0,0,0,.18);
}
@media (max-width: 900px) {
  main { padding: 14px; }
  .summary { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .layout { grid-template-columns: 1fr; }
}
@media (max-width: 560px) {
  .summary, .power-form, .form-row, .details, .segmented, .field-with-action { grid-template-columns: 1fr; }
  .segmented label + label { border-left: 0; border-top: 1px solid var(--line); }
  .field-with-action button { width: 100%; }
  .form-actions button { flex: 1 1 140px; }
  .topbar { align-items: flex-start; }
}
"""


APP_JS = r"""
const $ = (id) => document.getElementById(id);
let current = null;
let selectedLog = "";
let suggestedName = "";

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[ch]));
}

function formatTime(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function toast(message) {
  const box = $("toast");
  box.textContent = message;
  box.hidden = false;
  clearTimeout(window.__toastTimer);
  window.__toastTimer = setTimeout(() => box.hidden = true, 3500);
}

async function api(path, options = {}) {
  const init = { ...options, headers: { ...(options.headers || {}) } };
  if (init.body && typeof init.body !== "string") {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(init.body);
  }
  const response = await fetch(path, init);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || data.message || response.statusText);
  return data;
}

async function refresh() {
  current = await api("/api/status");
  render();
  if (selectedLog) refreshLog();
}

function renderSummary() {
  const services = current.services || [];
  const enabled = services.filter((s) => s.enabled);
  const running = services.filter((s) => ["managed", "external", "running", "scheduled"].includes(s.state));
  const supervisor = current.supervisor || {};
  const power = current.powerSchedule || {};
  $("summary").innerHTML = `
    <div class="metric"><span>System restart</span><strong>${esc(power.summary || "-")}</strong></div>
    <div class="metric"><span>Supervisor</span><strong>${esc(supervisor.state || (supervisor.loaded ? "loaded" : "not loaded"))}</strong></div>
    <div class="metric"><span>Enabled</span><strong>${enabled.length} / ${services.length}</strong></div>
    <div class="metric"><span>Running</span><strong>${running.length} / ${services.length}</strong></div>
  `;
}

function renderPowerSchedule() {
  const power = current.powerSchedule || {};
  $("powerEnabled").checked = !!power.enabled;
  $("powerTime").value = power.time || "05:00";
  $("powerMode").value = power.mode || "weekdays";
  $("powerIntervalDays").value = power.intervalDays || 3;
  $("powerStartDate").value = power.nextDate || "";
  const days = new Set(power.days || ["M", "T", "W", "R", "F", "S", "U"]);
  document.querySelectorAll("#powerDays input").forEach((input) => {
    input.checked = days.has(input.value);
  });
  $("powerRaw").textContent = power.summary || "";
  updatePowerMode();
}

function card(service) {
  const url = service.url ? `<a href="${esc(service.url)}" target="_blank" rel="noreferrer">${esc(service.url)}</a>` : "-";
  const ports = [service.port, ...(service.extraPorts || [])].filter(Boolean).join(", ") || "-";
  const commandOrLaunchd = service.kind === "launchd"
    ? `${service.launchdDomain || "gui"}/${service.launchdLabel || ""}`
    : service.commandText;
  const serviceActions = `<button class="small" data-action="start">Start</button>
        <button class="small ghost" data-action="stop">Stop</button>
        <button class="small ghost" data-action="restart">Restart</button>
        <button class="small ghost" data-action="check">Check</button>
        <button class="small ghost" data-action="edit">Edit</button>`;
  return `
    <article class="service-card" data-name="${esc(service.name)}">
      <div class="service-top">
        <div>
          <div class="service-title">
            <h3>${esc(service.name)}</h3>
            <span class="badge ${esc(service.state)}">${esc(service.state)}</span>
            <span class="badge">${service.enabled ? "enabled" : "disabled"}</span>
            <span class="badge">${esc(service.kind || "process")}</span>
          </div>
          <div class="muted">${esc(service.description || "")}</div>
        </div>
        <label class="checkline"><input type="checkbox" data-action="toggle" ${service.enabled ? "checked" : ""}> Enabled</label>
      </div>
      <div class="details">
        <div><strong>PID</strong> ${esc(service.pid || "-")}</div>
        <div><strong>Ports</strong> ${esc(ports)}</div>
        <div><strong>URL</strong> ${url}</div>
        <div><strong>Detail</strong> ${esc(service.detail)}</div>
        <div><strong>Started</strong> ${esc(formatTime(service.lastStartedAt))}</div>
        <div><strong>Restarted</strong> ${esc(formatTime(service.lastRestartedAt))}</div>
        <div><strong>Checked</strong> ${esc(formatTime(service.lastCheckedAt))}</div>
        <div><strong>Log</strong> ${esc(service.logPath)}</div>
      </div>
      <div class="details">
        <div><strong>CWD</strong> ${esc(service.cwd)}</div>
        <div><strong>${service.kind === "launchd" ? "Launchd" : "Command"}</strong> ${esc(commandOrLaunchd)}</div>
      </div>
      <div class="actions">
        ${serviceActions}
        <button class="small ghost" data-action="logs">Logs</button>
        <button class="small danger" data-action="delete">Delete</button>
      </div>
    </article>
  `;
}

function renderServices() {
  const services = current.services || [];
  const enabled = services.filter((s) => s.enabled);
  const disabled = services.filter((s) => !s.enabled);
  $("enabledServices").innerHTML = enabled.length ? enabled.map(card).join("") : `<div class="muted">No enabled services.</div>`;
  $("disabledServices").innerHTML = disabled.length ? disabled.map(card).join("") : `<div class="muted">No disabled services.</div>`;
}

function render() {
  renderSummary();
  renderPowerSchedule();
  renderServices();
  $("url").placeholder = `http://${current.lanIp || "192.168.x.x"}:8000`;
}

function powerPayload() {
  const mode = $("powerMode").value;
  const days = Array.from(document.querySelectorAll("#powerDays input:checked")).map((input) => input.value);
  if ($("powerEnabled").checked && mode === "weekdays" && days.length === 0) {
    throw new Error("Choose at least one restart day.");
  }
  return {
    enabled: $("powerEnabled").checked,
    time: $("powerTime").value,
    mode,
    days,
    intervalDays: $("powerIntervalDays").value,
    startDate: $("powerStartDate").value,
  };
}

function updatePowerMode() {
  const interval = $("powerMode").value === "interval";
  $("powerWeekdayFields").hidden = interval;
  $("powerIntervalFields").hidden = !interval;
}

function formPayload() {
  const kind = selectedKind();
  return {
    kind,
    name: $("name").value.trim(),
    cwd: (kind === "launchd" ? $("launchdCwd").value : $("processCwd").value).trim(),
    commandText: $("commandText").value.trim(),
    port: $("port").value.trim(),
    launchdLabel: $("launchdLabel").value.trim(),
    launchdDomain: $("launchdDomain").value.trim(),
    launchdAutoStart: $("launchdAutoStart").checked,
    launchdPlist: $("launchdPlist").value.trim(),
    extraPorts: $("extraPorts").value.trim(),
    stdoutPath: $("stdoutPath").value.trim(),
    stderrPath: $("stderrPath").value.trim(),
    startWaitSeconds: $("startWaitSeconds").value.trim(),
    url: $("url").value.trim(),
    description: $("description").value.trim(),
    enabled: $("enabled").checked,
  };
}

function selectedKind() {
  return document.querySelector('input[name="kind"]:checked')?.value || "process";
}

function selectKind(kind) {
  const input = document.querySelector(`input[name="kind"][value="${kind}"]`);
  if (input) input.checked = true;
  updateKindUI();
}

function updateSaveButton() {
  const editing = !!$("originalName").value;
  const startsNow = $("enabled").checked && $("startAfterSave").checked;
  $("saveServiceBtn").textContent = editing
    ? (startsNow ? "儲存並重新啟動" : "儲存變更")
    : (startsNow ? "新增並啟動" : "儲存設定");
}

function updateKindUI() {
  const kind = selectedKind();
  const isLaunchd = kind === "launchd";
  document.querySelectorAll("[data-kind-section]").forEach((section) => {
    section.hidden = section.dataset.kindSection !== kind;
  });
  document.querySelectorAll("[data-kind-advanced]").forEach((section) => {
    section.hidden = section.dataset.kindAdvanced !== kind;
  });
  $("processCwd").required = !isLaunchd;
  $("commandText").required = !isLaunchd;
  $("launchdPlist").required = isLaunchd;
  $("formModeHint").textContent = isLaunchd
    ? "填入 plist 路徑，其餘設定可自動讀取。"
    : "填入專案資料夾，可自動尋找常見啟動指令。";
  updateSaveButton();
}

function setDetectionStatus(id, message, tone = "success") {
  const box = $(id);
  box.textContent = message;
  box.classList.toggle("error", tone === "error");
  box.classList.toggle("notice", tone === "notice");
  box.hidden = !message;
}

function showFormError(message = "") {
  $("formError").textContent = message;
  $("formError").hidden = !message;
}

function applySuggestion(data) {
  const currentName = $("name").value.trim();
  if (data.name && (!currentName || currentName === suggestedName)) {
    $("name").value = data.name;
    suggestedName = data.name;
  }
  if (data.kind === "process") {
    if (data.cwd) $("processCwd").value = data.cwd;
    if (data.commandText) $("commandText").value = data.commandText;
    if (data.port && !$("port").value) $("port").value = data.port;
    if (data.port && !$("url").value) $("url").value = `http://${current.lanIp || location.hostname}:${data.port}`;
    setDetectionStatus(
      "processDetectionStatus",
      data.reason,
      data.detected ? "success" : "notice",
    );
    return;
  }
  $("launchdPlist").value = data.launchdPlist || $("launchdPlist").value;
  $("launchdLabel").value = data.launchdLabel || "";
  $("launchdDomain").value = data.launchdDomain || "";
  $("launchdCwd").value = data.cwd || "";
  $("stdoutPath").value = data.stdoutPath || "";
  $("stderrPath").value = data.stderrPath || "";
  $("launchdAutoStart").checked = !!data.launchdAutoStart;
  setDetectionStatus(
    "launchdDetectionStatus",
    `${data.reason} · ${data.launchdDomain}/${data.launchdLabel}`,
  );
}

async function detectService(kind, button) {
  const statusId = kind === "launchd" ? "launchdDetectionStatus" : "processDetectionStatus";
  setDetectionStatus(statusId, "正在讀取設定...", "notice");
  button.disabled = true;
  try {
    const body = kind === "launchd"
      ? { kind, launchdPlist: $("launchdPlist").value.trim() }
      : { kind, cwd: $("processCwd").value.trim() };
    const suggestion = await api("/api/service-suggestions", { method: "POST", body });
    applySuggestion(suggestion);
    showFormError();
  } catch (error) {
    setDetectionStatus(statusId, error.message, "error");
  } finally {
    button.disabled = false;
  }
}

function resetForm() {
  $("originalName").value = "";
  suggestedName = "";
  $("formTitle").textContent = "新增服務";
  $("serviceForm").reset();
  $("startWaitSeconds").value = "15";
  $("launchdAutoStart").checked = false;
  $("enabled").checked = true;
  $("startAfterSave").checked = true;
  $("startAfterSave").disabled = false;
  $("advancedSettings").open = false;
  $("cancelEditBtn").hidden = true;
  setDetectionStatus("processDetectionStatus", "");
  setDetectionStatus("launchdDetectionStatus", "");
  showFormError();
  selectKind("process");
  updateSaveButton();
}

function editService(service) {
  resetForm();
  $("originalName").value = service.name;
  $("formTitle").textContent = `編輯 ${service.name}`;
  selectKind(service.kind || "process");
  $("name").value = service.name;
  $("processCwd").value = service.cwd || "";
  $("launchdCwd").value = service.cwd || "";
  $("commandText").value = service.commandText;
  $("port").value = service.port || "";
  $("startWaitSeconds").value = service.startWaitSeconds ?? 15;
  $("launchdPlist").value = service.launchdPlist || "";
  $("launchdLabel").value = service.launchdLabel || "";
  $("launchdDomain").value = service.launchdDomain || "";
  $("launchdAutoStart").checked = service.launchdAutoStart !== false;
  $("extraPorts").value = (service.extraPorts || []).join(", ");
  $("stdoutPath").value = service.stdoutPath || "";
  $("stderrPath").value = service.stderrPath || "";
  $("url").value = service.url || "";
  $("description").value = service.description || "";
  $("enabled").checked = !!service.enabled;
  $("startAfterSave").checked = false;
  $("startAfterSave").disabled = !service.enabled;
  $("cancelEditBtn").hidden = false;
  updateSaveButton();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

async function runAction(name, action, button) {
  button.disabled = true;
  try {
    const encoded = encodeURIComponent(name);
    let response;
    if (action === "delete") {
      if (!confirm(`Delete ${name} from manager? Process services will be stopped first. Project files will not be deleted.`)) return;
      response = await api(`/api/services/${encoded}`, { method: "DELETE" });
    } else if (action === "toggle") {
      const service = current.services.find((s) => s.name === name);
      if (service && service.kind === "launchd") {
        response = await api(`/api/services/${encoded}/${button.checked ? "start" : "stop"}`, { method: "POST" });
      } else {
        response = await api(`/api/services/${encoded}`, {
          method: "PATCH",
          body: { enabled: button.checked }
        });
      }
    } else if (action === "edit") {
      editService(current.services.find((s) => s.name === name));
      return;
    } else if (action === "logs") {
      selectedLog = name;
      await refreshLog();
      return;
    } else {
      response = await api(`/api/services/${encoded}/${action}`, { method: "POST" });
    }
    toast(response.message || "Saved");
    await refresh();
  } finally {
    button.disabled = false;
  }
}

async function refreshLog() {
  if (!selectedLog) return;
  const data = await api(`/api/services/${encodeURIComponent(selectedLog)}/logs?lines=300`);
  $("logMeta").textContent = `${selectedLog} · ${data.logPath}`;
  $("logs").textContent = data.text || "";
}

document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-action]");
  if (!button) return;
  const card = button.closest("[data-name]");
  if (!card) return;
  try {
    await runAction(card.dataset.name, button.dataset.action, button);
  } catch (error) {
    toast(error.message);
    await refresh();
  }
});

$("serviceForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const original = $("originalName").value;
  const payload = formPayload();
  const saveButton = $("saveServiceBtn");
  saveButton.disabled = true;
  showFormError();
  try {
    const response = original
      ? await api(`/api/services/${encodeURIComponent(original)}`, { method: "PATCH", body: payload })
      : await api("/api/services", { method: "POST", body: payload });

    const savedName = response.service?.name || payload.name || original;
    if (payload.enabled && $("startAfterSave").checked && savedName) {
      try {
        const action = original ? "restart" : "start";
        const started = await api(`/api/services/${encodeURIComponent(savedName)}/${action}`, { method: "POST" });
        toast(started.message || (original ? `已重新啟動 ${savedName}` : `已啟動 ${savedName}`));
      } catch (error) {
        showFormError(`設定已儲存，但啟動失敗：${error.message}`);
        await refresh();
        return;
      }
    } else {
      toast(response.message || "已儲存");
    }
    resetForm();
    await refresh();
  } catch (error) {
    showFormError(error.message);
    toast(error.message);
  } finally {
    saveButton.disabled = false;
  }
});

$("powerForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("savePowerBtn").disabled = true;
  try {
    const response = await api("/api/power-schedule", { method: "POST", body: powerPayload() });
    toast(response.message || "Saved");
    await refresh();
  } catch (error) {
    toast(error.message);
  } finally {
    $("savePowerBtn").disabled = false;
  }
});

$("refreshBtn").addEventListener("click", () => refresh().catch((error) => toast(error.message)));
$("refreshLogBtn").addEventListener("click", () => refreshLog().catch((error) => toast(error.message)));
$("powerMode").addEventListener("change", updatePowerMode);
$("resetFormBtn").addEventListener("click", resetForm);
$("cancelEditBtn").addEventListener("click", resetForm);
$("detectProcessBtn").addEventListener("click", (event) => detectService("process", event.currentTarget));
$("detectLaunchdBtn").addEventListener("click", (event) => detectService("launchd", event.currentTarget));
document.querySelectorAll('input[name="kind"]').forEach((input) => {
  input.addEventListener("change", updateKindUI);
});
$("enabled").addEventListener("change", () => {
  if (!$("enabled").checked) $("startAfterSave").checked = false;
  $("startAfterSave").disabled = !$("enabled").checked;
  updateSaveButton();
});
$("startAfterSave").addEventListener("change", updateSaveButton);
resetForm();
refresh().catch((error) => toast(error.message));
"""


class ServerManagerHandler(BaseHTTPRequestHandler):
    server_version = "ServerManager/1.0"

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} - {fmt % args}")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/":
                send_text(self, INDEX_HTML, "text/html; charset=utf-8")
            elif path == "/styles.css":
                send_text(self, STYLE_CSS, "text/css; charset=utf-8")
            elif path == "/app.js":
                send_text(self, APP_JS, "application/javascript; charset=utf-8")
            elif path == "/api/status":
                send_json(self, status_payload())
            elif path.startswith("/api/services/") and path.endswith("/logs"):
                self.handle_logs(path, parsed.query)
            else:
                send_json(self, {"error": "not found"}, 404)
        except Exception as exc:
            send_json(self, {"error": str(exc)}, 500)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/service-suggestions":
                self.handle_service_suggestion()
            elif path == "/api/services":
                self.handle_create_service()
            elif path == "/api/power-schedule":
                self.handle_power_schedule()
            elif path.startswith("/api/services/"):
                self.handle_action(path)
            else:
                send_json(self, {"error": "not found"}, 404)
        except ValueError as exc:
            send_json(self, {"error": str(exc)}, 400)
        except Exception as exc:
            send_json(self, {"error": str(exc)}, 500)

    def do_PATCH(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path.startswith("/api/services/"):
                self.handle_patch_service(parsed.path)
            else:
                send_json(self, {"error": "not found"}, 404)
        except ValueError as exc:
            send_json(self, {"error": str(exc)}, 400)
        except Exception as exc:
            send_json(self, {"error": str(exc)}, 500)

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path.startswith("/api/services/"):
                self.handle_delete_service(parsed.path)
            else:
                send_json(self, {"error": "not found"}, 404)
        except Exception as exc:
            send_json(self, {"error": str(exc)}, 500)

    def handle_power_schedule(self) -> None:
        payload = read_json_body(self)
        send_json(self, set_power_schedule(payload))

    def handle_service_suggestion(self) -> None:
        payload = read_json_body(self)
        send_json(self, service_suggestion(payload))

    def service_name_from_path(self, path: str, suffix: str = "") -> str:
        prefix = "/api/services/"
        if not path.startswith(prefix):
            raise ValueError("invalid service path")
        name = path[len(prefix):]
        if suffix and name.endswith(suffix):
            name = name[: -len(suffix)]
        if "/" in name:
            name = name.split("/", 1)[0]
        return unquote(name)

    def handle_create_service(self) -> None:
        payload = read_json_body(self)
        services = load_services()
        service = validate_service_payload(payload, services)
        services[service.name] = service
        save_services(services)
        record_event(service.name, "created", "created service config")
        send_json(self, {"ok": True, "message": f"created {service.name}", "service": service_payload(service, latest_service_events())}, 201)

    def handle_patch_service(self, path: str) -> None:
        original = self.service_name_from_path(path)
        payload = read_json_body(self)
        services = load_services()
        if original not in services:
            send_json(self, {"error": f"unknown service: {original}"}, 404)
            return
        old = services.pop(original)
        service = validate_service_payload(payload, services, old)
        services[service.name] = service
        if service.name != original and old.pid_file.exists():
            service.pid_file.parent.mkdir(parents=True, exist_ok=True)
            old.pid_file.rename(service.pid_file)
        save_services(services)
        record_event(service.name, "updated", f"updated service config from {original}")
        send_json(self, {"ok": True, "message": f"saved {service.name}", "service": service_payload(service, latest_service_events())})

    def handle_delete_service(self, path: str) -> None:
        name = self.service_name_from_path(path)
        services = load_services()
        if name not in services:
            send_json(self, {"error": f"unknown service: {name}"}, 404)
            return
        service = services[name]
        state, pid, _detail = service_state(service)
        stop_payload = None
        if service.kind == "process" and state in {"managed", "external"}:
            stop_payload = capture_operation(stop_service, service, True)
            if not stop_payload.get("ok"):
                send_json(self, {"error": f"could not stop {name}; config was not deleted", "stop": stop_payload}, 409)
                return
        services.pop(name)
        save_services(services)
        message = "deleted service config"
        if service.kind == "launchd":
            message = "deleted manager entry; launchd service unchanged"
        record_event(name, "deleted", message)
        send_json(self, {"ok": True, "message": f"{message} for {name}", "state": state, "pid": pid, "stop": stop_payload})

    def handle_action(self, path: str) -> None:
        parts = path[len("/api/services/"):].split("/")
        if len(parts) != 2:
            send_json(self, {"error": "invalid action path"}, 404)
            return
        name = unquote(parts[0])
        action = parts[1]
        services = load_services()
        if name not in services:
            send_json(self, {"error": f"unknown service: {name}"}, 404)
            return
        service = services[name]
        if action in {"start", "restart"} and not service.enabled:
            service = touch_service(services, name, enabled=True)
        operations = {
            "start": start_service,
            "stop": stop_service,
            "restart": restart_service,
            "check": check_service,
        }
        if action not in operations:
            send_json(self, {"error": f"unknown action: {action}"}, 404)
            return
        payload = capture_operation(operations[action], service)
        if service.kind == "launchd" and action == "stop" and payload.get("ok"):
            touch_service(load_services(), name, enabled=False)
        if service.kind == "launchd" and action in {"start", "restart"} and payload.get("ok"):
            touch_service(load_services(), name, enabled=True)
        payload["status"] = status_payload()
        send_json(self, payload, 200 if payload.get("ok") else 409)

    def handle_logs(self, path: str, query: str) -> None:
        name = self.service_name_from_path(path, suffix="/logs")
        services = load_services()
        if name not in services:
            send_json(self, {"error": f"unknown service: {name}"}, 404)
            return
        params = parse_qs(query)
        try:
            lines = int(params.get("lines", ["300"])[0])
        except ValueError:
            lines = 300
        lines = max(1, min(lines, 2000))
        service = services[name]
        send_json(self, {"ok": True, "service": name, "logPath": str(service.log_file), "text": tail_log_text(service, lines)})


def run_web(port: int, host: str = DEFAULT_WEB_HOST) -> None:
    ensure_dirs()
    server = ThreadingHTTPServer((host, port), ServerManagerHandler)
    shown_host = lan_ip_address() if host in {"", DEFAULT_WEB_HOST} else host
    print(f"web panel listening on http://{shown_host}:{port}")
    if host == DEFAULT_WEB_HOST:
        print(f"LAN access enabled on port {port}; use this only on a trusted network")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Small local server manager")
    subparsers = parser.add_subparsers(dest="command", required=True)

    for command in ("list", "status", "start", "check", "restart"):
        sub = subparsers.add_parser(command)
        sub.add_argument("names", nargs="*", help="service names, or all")

    supervise = subparsers.add_parser("supervise")
    supervise.add_argument("names", nargs="*", help="service names, or all")
    supervise.add_argument("--interval", type=int, default=DEFAULT_SUPERVISE_INTERVAL, help="poll interval in seconds")

    stop = subparsers.add_parser("stop")
    stop.add_argument("names", nargs="*", help="service names, or all")
    stop.add_argument("--by-port", action="store_true", help="also stop configured port listeners when no managed pid exists")

    logs = subparsers.add_parser("logs")
    logs.add_argument("name")
    logs.add_argument("-n", "--lines", type=int, default=80)

    web = subparsers.add_parser("web")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--host", default=DEFAULT_WEB_HOST, help="bind host; defaults to 0.0.0.0 for LAN access")

    subparsers.add_parser("install-launchd")
    subparsers.add_parser("uninstall-launchd")
    subparsers.add_parser("launchd-status")

    install_web = subparsers.add_parser("install-web-launchd")
    install_web.add_argument("--host", default=DEFAULT_WEB_HOST, help="bind host for the startup web panel")
    install_web.add_argument("--port", type=int, default=DEFAULT_WEB_PORT, help="bind port for the startup web panel")

    subparsers.add_parser("uninstall-web-launchd")
    subparsers.add_parser("web-launchd-status")
    subparsers.add_parser("power-interval-sync")

    args = parser.parse_args()

    if args.command == "install-launchd":
        install_launchd()
        return 0
    if args.command == "install-web-launchd":
        install_web_launchd(args.host, args.port)
        return 0
    if args.command == "uninstall-launchd":
        uninstall_launchd()
        return 0
    if args.command == "uninstall-web-launchd":
        uninstall_web_launchd()
        return 0
    if args.command == "launchd-status":
        launchd_status()
        return 0
    if args.command == "web-launchd-status":
        web_launchd_status()
        return 0
    if args.command == "power-interval-sync":
        sync_power_interval_schedule()
        return 0
    if args.command == "web":
        run_web(args.port, args.host)
        return 0

    services = load_services()
    if args.command == "logs":
        service = select_services(services, [args.name])[0]
        tail_log(service, args.lines)
        return 0

    selected = select_services(services, args.names)
    if args.command == "list":
        for service in selected:
            enabled = "enabled" if service.enabled else "disabled"
            print(f"{service.name} ({enabled}): {service.description}\n  cwd: {service.cwd}\n  command: {shlex.join(service.command)}")
        return 0
    if args.command == "status":
        print_status(selected)
        return 0
    if args.command == "start":
        for service in selected:
            start_service(service)
        return 0
    if args.command == "check":
        for service in selected:
            check_service(service)
        return 0
    if args.command == "stop":
        for service in selected:
            stop_service(service, by_port=args.by_port)
        return 0
    if args.command == "restart":
        for service in selected:
            restart_service(service)
        return 0
    if args.command == "supervise":
        supervise_services(args.names, args.interval)
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
