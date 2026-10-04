# Server Manager

Small local server manager with a built-in web panel. It uses only the Python
standard library and is designed for personal machines that run several local
servers, watchers, or launchd jobs.

## Quick Start

```bash
git clone https://github.com/NightSay2002/Server-Manager.git
cd Server-Manager
./server-manager web --port 8765
```

Open:

```text
http://<LAN-IP>:8765
```

The web panel and detected web servers bind to `0.0.0.0` by default. The panel
shows local service links with the Mac's current LAN IP instead of the bind
address. Set `SERVER_MANAGER_LAN_IP` only when automatic LAN IP detection picks
the wrong interface. Use this only on a trusted network.

On first run, the launcher creates a local `servers.json` from
`servers.example.json`. `servers.json` is intentionally ignored by Git so your
machine-specific paths, commands, ports, and service labels stay private.

## Commands

```bash
./server-manager status
./server-manager check all
./server-manager start all
./server-manager stop all
./server-manager restart <service-name>
./server-manager logs <service-name>
./server-manager supervise all --interval 1800
./server-manager web --port 8765
```

To choose the bind address explicitly:

```bash
./server-manager web --host 0.0.0.0 --port 8765
```

Use `--host 127.0.0.1` instead when the panel must remain local-only.

## Web Panel

The web panel can:

- Add, edit, enable, disable, delete, start, stop, restart, and check services.
- Show service state, pid, port, URL, recent events, and logs.
- Tail logs from each service folder.
- View and edit fixed-weekday or every-N-days macOS restart schedules. Interval
  schedules use `pmset` for the next restart and a local LaunchAgent to advance
  the date after login.

Service logs are written to:

```text
<service cwd>/.server-manager/logs/<service-name>.log
```

Manager events and pid files are stored under `.state/`.

## Service Types

### Process

Use this for normal commands, Python watchers, Node apps, local APIs, and dev
servers.

Required fields:

- `name`
- `cwd`
- `command`

Optional fields:

- `description`
- `port`
- `url`
- `env`
- `enabled`
- `startWaitSeconds`

### Launchd

Use this for existing macOS LaunchAgent or LaunchDaemon jobs. Server Manager
uses `launchctl` and does not start a duplicate process.

Required fields:

- `name`
- `kind: "launchd"`
- `launchdLabel`
- `launchdDomain`: usually `gui` or `system`

Optional fields:

- `launchdPlist`
- `launchdAutoStart`
- `primaryPort`
- `stdoutPath`
- `stderrPath`
- `url`
- `startWaitSeconds`

Set `startWaitSeconds` higher than the service's real cold-start time. Services
that initialize embedded tools, such as BiliLive, may need about 180 seconds;
the manager waits for `primaryPort` before reporting the start as successful.

For `system` launchd jobs and `pmset repeat`, macOS may require admin
permission. Configure sudoers narrowly if you want the web panel to control
those without interactive password prompts.

## Auto Start

Install the background supervisor at login:

```bash
./server-manager install-launchd
```

Enabled process services start when the supervisor loads at login. The
supervisor also checks them at its configured interval and restarts stopped or
unhealthy services.

Install the web panel at login:

```bash
./server-manager install-web-launchd --host 0.0.0.0 --port 8765
```

For local-only access at login:

```bash
./server-manager install-web-launchd --host 127.0.0.1 --port 8765
```

Check or remove launchd jobs:

```bash
./server-manager launchd-status
./server-manager web-launchd-status
./server-manager uninstall-launchd
./server-manager uninstall-web-launchd
```

The default launchd labels are:

```text
com.local.server-manager
com.local.server-manager.web
```

They can be overridden with:

```bash
SERVER_MANAGER_LAUNCHD_LABEL=com.example.server-manager
SERVER_MANAGER_WEB_LAUNCHD_LABEL=com.example.server-manager.web
```

## Daily Git Push

The optional Python heartbeat job appends one timestamp to
`daily-push-test.txt`, commits only that file, and pushes the current branch to
`origin` once per day. Install it at the default time of 05:30:

```bash
python3 daily_git_push.py install
```

Choose another local time, run it immediately, inspect it, or remove it:

```bash
python3 daily_git_push.py install --hour 7 --minute 15
python3 daily_git_push.py run
python3 daily_git_push.py status
python3 daily_git_push.py uninstall
```

The job never stages other files. If a push is rejected because the remote has
new commits, it logs the error under `.state/` and leaves conflict resolution
to you.

## Local Config

`servers.json` is not committed. To publish or share this project safely, commit
`servers.example.json` only.

Example process service:

```json
{
  "services": [
    {
      "name": "example-api",
      "description": "Example local API",
      "kind": "process",
      "enabled": true,
      "cwd": "/absolute/path/to/project",
      "command": ["python3", "-m", "http.server", "8080"],
      "port": 8080,
      "url": "http://192.168.0.10:8080"
    }
  ]
}
```

For a service that should be opened by other devices on the same network, set
`url` to the Mac's LAN address, and make sure the service listens on more than
`127.0.0.1`.
