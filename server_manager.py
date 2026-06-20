#!/usr/bin/env python3
import argparse
import contextlib
import html
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
from datetime import datetime
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
        raise ValueError("port must be a number") from exc
    if port < 1 or port > 65535:
        raise ValueError("port must be between 1 and 65535")
    return port


def normalize_ports(value) -> list[int]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        raw_values = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, list):
        raw_values = value
    else:
        raise ValueError("extraPorts must be a comma-separated string or array")
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
        return list(existing)
    else:
        command = ""

    if isinstance(command, list):
        parsed = [str(part) for part in command]
    elif isinstance(command, str):
        try:
            parsed = shlex.split(command)
        except ValueError as exc:
            raise ValueError(f"command parse failed: {exc}") from exc
    else:
        raise ValueError("command must be a string or array")
    if not parsed:
        raise ValueError("command is required")
    return parsed


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
    if parsed.get("lastExitCode"):
        detail_parts.append(f"last exit {parsed['lastExitCode']}")
    if parsed.get("lastTerminatingSignal"):
        detail_parts.append(f"last signal {parsed['lastTerminatingSignal']}")
    port_details = open_port_details([port for port in [service.port, *(service.extra_ports or [])] if port])
    detail_parts.extend(port_details)

    if launchd_state == "running":
        return "running", pid, "; ".join(detail_parts)
    if launchd_state in {"spawn scheduled", "not running"} and parsed.get("runInterval"):
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
    print(f"web panel will run at login on http://{host if host != '0.0.0.0' else '0.0.0.0'}:{port}")


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
    match = re.search(r"restart\s+at\s+(.+?)\s+(every day|on\s+.+)$", line, re.IGNORECASE)
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

    raw_days = match.group(2).strip()
    if raw_days.lower() != "every day":
        compact = raw_days.lower().removeprefix("on").strip().upper()
        days = normalize_power_days(compact)
        parsed["days"] = list(days)
    return parsed


def power_schedule_status() -> dict:
    pmset = shutil.which("pmset")
    if not pmset:
        return {"summary": "pmset not available", "raw": "", "lines": [], "enabled": False, "time": "05:00", "days": list(POWER_DAYS)}
    result = subprocess.run([pmset, "-g", "sched"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    raw = result.stdout or result.stderr
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    summary = "no repeating restart schedule found"
    structured = {"enabled": False, "time": "05:00", "timeWithSeconds": "05:00:00", "days": list(POWER_DAYS)}
    for line in lines:
        if "restart" in line.lower():
            summary = line
            structured = parse_pmset_restart_line(line)
            break
    structured["daySummary"] = display_power_days("".join(structured["days"]))
    return {"summary": summary, "raw": raw, "lines": lines, **structured}


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
        return {"ok": True, "message": "system restart schedule disabled", "powerSchedule": power_schedule_status()}

    time_value = normalize_power_time(str(payload.get("time", "")).strip())
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


def service_payload(service: Service, event_summary: dict[str, dict[str, str]]) -> dict:
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
        "url": service.url,
        "state": state,
        "pid": pid,
        "detail": detail,
        "logPath": str(service.log_file),
        "launchdLabel": service.launchd_label,
        "launchdDomain": service.launchd_domain,
        "launchdAutoStart": service.launchd_auto_start,
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
    return {
        "services": [service_payload(service, events) for service in services.values()],
        "powerSchedule": power_schedule_status(),
        "supervisor": parse_launchd_status(launchd_raw),
        "eventLog": str(EVENTS_FILE),
        "now": now_iso(),
    }


def validate_service_payload(payload: dict, services: dict[str, Service], existing: Service | None = None) -> Service:
    kind = payload.get("kind", existing.kind if existing else "process")
    if kind not in {"process", "launchd"}:
        raise ValueError("kind must be process or launchd")

    if kind == "launchd":
        label = payload.get("launchdLabel", existing.launchd_label if existing else "")
        if not label:
            raise ValueError("launchdLabel is required for launchd services")
        raw_name = payload.get("name") or (existing.name if existing else label)
        name = unique_service_name(str(raw_name), services, original=existing.name if existing else None)
        url = payload.get("url", existing.url if existing else None)
        if url == "":
            url = None
        cwd_raw = payload.get("cwd", str(existing.cwd) if existing and existing.cwd else "")
        cwd = Path(str(cwd_raw)).expanduser() if cwd_raw else None
        if cwd and (not cwd.is_absolute() or not cwd.exists()):
            raise ValueError(f"cwd does not exist or is not absolute: {cwd}")
        created_at = existing.created_at if existing and existing.created_at else now_iso()
        start_wait_seconds = int(payload.get("startWaitSeconds", existing.start_wait_seconds if existing else 2) or 0)
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
            launchd_domain=str(payload.get("launchdDomain", existing.launchd_domain if existing and existing.launchd_domain else "gui") or "gui"),
            launchd_auto_start=bool(payload.get("launchdAutoStart", existing.launchd_auto_start if existing else True)),
            launchd_plist=Path(payload["launchdPlist"]).expanduser() if payload.get("launchdPlist") else (existing.launchd_plist if existing else None),
            stdout_path=Path(payload["stdoutPath"]).expanduser() if payload.get("stdoutPath") else (existing.stdout_path if existing else None),
            stderr_path=Path(payload["stderrPath"]).expanduser() if payload.get("stderrPath") else (existing.stderr_path if existing else None),
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
        raise ValueError("cwd is required")
    cwd = Path(str(cwd_raw)).expanduser()
    if not cwd.is_absolute():
        raise ValueError("cwd must be an absolute path")
    if not cwd.exists():
        raise ValueError(f"cwd does not exist: {cwd}")
    if url == "":
        url = None
    return Service(
        name=name,
        description=str(description or ""),
        cwd=cwd,
        command=command,
        port=port,
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
        <div class="weekday-row" id="powerDays">
          <label class="checkline"><input type="checkbox" value="M"> Mon</label>
          <label class="checkline"><input type="checkbox" value="T"> Tue</label>
          <label class="checkline"><input type="checkbox" value="W"> Wed</label>
          <label class="checkline"><input type="checkbox" value="R"> Thu</label>
          <label class="checkline"><input type="checkbox" value="F"> Fri</label>
          <label class="checkline"><input type="checkbox" value="S"> Sat</label>
          <label class="checkline"><input type="checkbox" value="U"> Sun</label>
        </div>
        <div id="powerRaw" class="muted"></div>
      </form>
    </section>
    <section class="layout">
      <div>
        <section class="panel">
          <div class="panel-head">
            <h2 id="formTitle">新增 server</h2>
            <button id="resetFormBtn" type="button" class="ghost">Clear</button>
          </div>
          <form id="serviceForm" class="service-form">
            <input type="hidden" id="originalName">
            <label>Kind
              <select id="kind" name="kind">
                <option value="process">Process / Python watcher</option>
                <option value="launchd">Launchd service</option>
              </select>
            </label>
            <label>Name<input id="name" name="name" autocomplete="off" required></label>
            <label>CWD<input id="cwd" name="cwd" placeholder="/absolute/path" required></label>
            <label>Command<input id="commandText" name="commandText" placeholder="npm run start" required></label>
            <div class="form-row">
              <label>Port<input id="port" name="port" inputmode="numeric"></label>
              <label>URL<input id="url" name="url" placeholder="http://127.0.0.1:8000"></label>
            </div>
            <label>Launchd Label<input id="launchdLabel" name="launchdLabel" placeholder="com.example.service"></label>
            <div class="form-row">
              <label>Domain<input id="launchdDomain" name="launchdDomain" placeholder="gui or system"></label>
              <label>Extra ports<input id="extraPorts" name="extraPorts" placeholder="8080, 18110"></label>
            </div>
            <label>stdout log<input id="stdoutPath" name="stdoutPath" placeholder="/tmp/service.out.log"></label>
            <label>stderr log<input id="stderrPath" name="stderrPath" placeholder="/tmp/service.err.log"></label>
            <label>Description<input id="description" name="description"></label>
            <label class="checkline"><input id="launchdAutoStart" name="launchdAutoStart" type="checkbox" checked> Native launchd auto-start</label>
            <label class="checkline"><input id="enabled" name="enabled" type="checkbox" checked> Enabled</label>
            <button type="submit">Save</button>
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
  grid-template-columns: minmax(320px, 420px) minmax(0, 1fr);
  gap: 16px;
  align-items: start;
}
.panel { padding: 14px; margin-bottom: 16px; }
.service-form { display: grid; gap: 10px; }
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
.weekday-row {
  display: flex;
  flex-wrap: wrap;
  gap: 10px 12px;
  align-items: center;
}
.form-row { display: grid; grid-template-columns: 120px minmax(0, 1fr); gap: 10px; }
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
  .summary, .power-form, .form-row, .details { grid-template-columns: 1fr; }
  .topbar { align-items: flex-start; }
}
"""


APP_JS = r"""
const $ = (id) => document.getElementById(id);
let current = null;
let selectedLog = "";

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
  const days = new Set(power.days || ["M", "T", "W", "R", "F", "S", "U"]);
  document.querySelectorAll("#powerDays input").forEach((input) => {
    input.checked = days.has(input.value);
  });
  $("powerRaw").textContent = power.summary || "";
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
}

function powerPayload() {
  const days = Array.from(document.querySelectorAll("#powerDays input:checked")).map((input) => input.value);
  if ($("powerEnabled").checked && days.length === 0) {
    throw new Error("Choose at least one restart day.");
  }
  return {
    enabled: $("powerEnabled").checked,
    time: $("powerTime").value,
    days,
  };
}

function formPayload() {
  return {
    kind: $("kind").value,
    name: $("name").value.trim(),
    cwd: $("cwd").value.trim(),
    commandText: $("commandText").value.trim(),
    port: $("port").value.trim(),
    launchdLabel: $("launchdLabel").value.trim(),
    launchdDomain: $("launchdDomain").value.trim(),
    launchdAutoStart: $("launchdAutoStart").checked,
    extraPorts: $("extraPorts").value.trim(),
    stdoutPath: $("stdoutPath").value.trim(),
    stderrPath: $("stderrPath").value.trim(),
    url: $("url").value.trim(),
    description: $("description").value.trim(),
    enabled: $("enabled").checked,
  };
}

function updateKindRequirements() {
  const isLaunchd = $("kind").value === "launchd";
  $("cwd").required = !isLaunchd;
  $("commandText").required = !isLaunchd;
  $("launchdLabel").required = isLaunchd;
}

function resetForm() {
  $("originalName").value = "";
  $("formTitle").textContent = "新增 server";
  $("serviceForm").reset();
  $("kind").value = "process";
  updateKindRequirements();
  $("launchdAutoStart").checked = true;
  $("enabled").checked = true;
}

function editService(service) {
  $("originalName").value = service.name;
  $("formTitle").textContent = `編輯 ${service.name}`;
  $("kind").value = service.kind || "process";
  updateKindRequirements();
  $("name").value = service.name;
  $("cwd").value = service.cwd;
  $("commandText").value = service.commandText;
  $("port").value = service.port || "";
  $("launchdLabel").value = service.launchdLabel || "";
  $("launchdDomain").value = service.launchdDomain || "";
  $("launchdAutoStart").checked = service.launchdAutoStart !== false;
  $("extraPorts").value = (service.extraPorts || []).join(", ");
  $("stdoutPath").value = service.stdoutPath || "";
  $("stderrPath").value = service.stderrPath || "";
  $("url").value = service.url || "";
  $("description").value = service.description || "";
  $("enabled").checked = !!service.enabled;
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
  try {
    const response = original
      ? await api(`/api/services/${encodeURIComponent(original)}`, { method: "PATCH", body: payload })
      : await api("/api/services", { method: "POST", body: payload });
    toast(response.message || "Saved");
    resetForm();
    await refresh();
  } catch (error) {
    toast(error.message);
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
$("resetFormBtn").addEventListener("click", resetForm);
$("kind").addEventListener("change", updateKindRequirements);
updateKindRequirements();
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
            if path == "/api/services":
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


def run_web(port: int, host: str = "127.0.0.1") -> None:
    ensure_dirs()
    server = ThreadingHTTPServer((host, port), ServerManagerHandler)
    shown_host = "127.0.0.1" if host in {"", "0.0.0.0"} else host
    print(f"web panel listening on http://{shown_host}:{port}")
    if host == "0.0.0.0":
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
    web.add_argument("--host", default="127.0.0.1", help="bind host; use 0.0.0.0 for LAN access")

    subparsers.add_parser("install-launchd")
    subparsers.add_parser("uninstall-launchd")
    subparsers.add_parser("launchd-status")

    install_web = subparsers.add_parser("install-web-launchd")
    install_web.add_argument("--host", default=DEFAULT_WEB_HOST, help="bind host for the startup web panel")
    install_web.add_argument("--port", type=int, default=DEFAULT_WEB_PORT, help="bind port for the startup web panel")

    subparsers.add_parser("uninstall-web-launchd")
    subparsers.add_parser("web-launchd-status")

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
