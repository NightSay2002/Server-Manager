#!/usr/bin/env python3
"""Create and manage a daily Git heartbeat using macOS launchd."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / ".state"
HEARTBEAT_FILE = ROOT / "daily-push-test.txt"
LOCK_FILE = STATE_DIR / "daily-git-push.lock"
LOG_FILE = STATE_DIR / "daily-git-push.log"
ERROR_LOG_FILE = STATE_DIR / "daily-git-push.err.log"
LABEL = os.environ.get(
    "SERVER_MANAGER_DAILY_GIT_PUSH_LABEL",
    "com.local.server-manager.daily-git-push",
)
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
DEFAULT_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def run_command(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        args,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise RuntimeError(f"{args[0]} failed: {detail}")
    return result


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run_command(["git", *args], check=check)


def current_branch() -> str:
    branch = git("branch", "--show-current").stdout.strip()
    if not branch:
        raise RuntimeError("cannot push from a detached HEAD")
    return branch


def append_heartbeat(branch: str) -> str:
    now = dt.datetime.now().astimezone()
    line = f"{now.isoformat(timespec='seconds')} branch={branch}\n"
    with HEARTBEAT_FILE.open("a", encoding="utf-8") as handle:
        handle.write(line)
    return line.strip()


def run_daily_push() -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("daily git push is already running")
            return 0

        if shutil.which("git") is None:
            raise RuntimeError("git is not available in PATH")
        if git("rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
            raise RuntimeError(f"not a Git repository: {ROOT}")
        if not git("remote", "get-url", "origin", check=False).stdout.strip():
            raise RuntimeError("Git remote 'origin' is not configured")

        branch = current_branch()
        heartbeat = append_heartbeat(branch)
        git("add", "--", HEARTBEAT_FILE.name)

        staged = git("diff", "--cached", "--quiet", "--", HEARTBEAT_FILE.name, check=False)
        if staged.returncode == 0:
            print("heartbeat is unchanged; nothing to commit")
            return 0
        if staged.returncode != 1:
            raise RuntimeError(staged.stderr.strip() or "could not inspect staged heartbeat")

        date_text = dt.datetime.now().astimezone().date().isoformat()
        git("commit", "-m", f"Daily push {date_text}", "--", HEARTBEAT_FILE.name)
        git("push", "-u", "origin", branch)
        print(f"pushed daily heartbeat: {heartbeat}")
        return 0


def build_plist(hour: int, minute: int) -> dict:
    python = sys.executable or shutil.which("python3") or "/usr/bin/python3"
    return {
        "Label": LABEL,
        "ProgramArguments": [python, str(Path(__file__).resolve()), "run"],
        "WorkingDirectory": str(ROOT),
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "ProcessType": "Background",
        "StandardOutPath": str(LOG_FILE),
        "StandardErrorPath": str(ERROR_LOG_FILE),
        "EnvironmentVariables": {"PATH": DEFAULT_PATH},
    }


def install(hour: int, minute: int) -> int:
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("hour must be 0-23 and minute must be 0-59")

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with PLIST_PATH.open("wb") as handle:
        plistlib.dump(build_plist(hour, minute), handle)

    domain = f"gui/{os.getuid()}"
    subprocess.run(
        ["launchctl", "bootout", domain, str(PLIST_PATH)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    result = subprocess.run(
        ["launchctl", "bootstrap", domain, str(PLIST_PATH)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"launchctl bootstrap failed: {detail}")
    subprocess.run(["launchctl", "enable", f"{domain}/{LABEL}"], check=False)
    print(f"installed daily Git push for {hour:02d}:{minute:02d}")
    print(f"launchd agent: {PLIST_PATH}")
    return 0


def uninstall() -> int:
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(PLIST_PATH)], check=False)
    try:
        PLIST_PATH.unlink()
        print(f"removed launchd agent: {PLIST_PATH}")
    except FileNotFoundError:
        print(f"launchd agent is not installed: {PLIST_PATH}")
    return 0


def status() -> int:
    target = f"gui/{os.getuid()}/{LABEL}"
    result = subprocess.run(
        ["launchctl", "print", target],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    print(result.stdout if result.returncode == 0 else result.stderr.strip())
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily Git heartbeat and push")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("run", help="append, commit, and push one heartbeat")
    install_parser = subparsers.add_parser("install", help="install the daily launchd job")
    install_parser.add_argument("--hour", type=int, default=5)
    install_parser.add_argument("--minute", type=int, default=30)
    subparsers.add_parser("status", help="show the launchd job status")
    subparsers.add_parser("uninstall", help="remove the daily launchd job")
    args = parser.parse_args()

    try:
        if args.command == "run":
            return run_daily_push()
        if args.command == "install":
            return install(args.hour, args.minute)
        if args.command == "status":
            return status()
        if args.command == "uninstall":
            return uninstall()
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
