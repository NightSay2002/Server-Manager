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
http://127.0.0.1:8765
```

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

To allow access from another device on the same trusted LAN:

```bash
./server-manager web --host 0.0.0.0 --port 8765
```

Then open the machine's LAN IP on port `8765`.

## Web Panel

The web panel can:

- Add, edit, enable, disable, delete, start, stop, restart, and check services.
- Show service state, pid, port, URL, recent events, and logs.
- Tail logs from each service folder.
- View and edit the macOS repeating restart schedule through `pmset`.

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

For `system` launchd jobs and `pmset repeat`, macOS may require admin
permission. Configure sudoers narrowly if you want the web panel to control
those without interactive password prompts.

## Auto Start

Install the background supervisor at login:

```bash
./server-manager install-launchd
```

Install the web panel at login:

```bash
./server-manager install-web-launchd --host 127.0.0.1 --port 8765
```

For LAN access at login:

```bash
./server-manager install-web-launchd --host 0.0.0.0 --port 8765
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
      "url": "http://127.0.0.1:8080"
    }
  ]
}
```
