q# Mobile Development Tools

Tools and configuration for phone automation via Termux and MacroDroid.

## Overview

This directory contains:
- Termux shell configuration templates
- MacroDroid macro specifications (YAML)
- Documentation for mobile automation tooling

The phone (Samsung S24) connects via Tailscale and runs Termux for SSH access.

## Focus Management System

A phone-server system for managing app usage. The phone reports events to the server, which decides enforcement actions.

### Architecture

```
Phone (MacroDroid)                    Desktop Server (Token-API)
──────────────────                    ────────────────────────────

[Telemetry Macros]
  Twitter/YouTube/Games    ─POST──>   /phone (logs app events)
  Open/Close events                   │
                                      ▼
  Geofence Home/Gym       ─POST──>   Server analyzes context
  Enter/Exit events                   (time, location, usage)
         │                                    │
         ├─ 200 OK ─────────────────>        │
         │                                    ▼
         └─ Error ──> Enable Local    /enforce (pushes commands)
                      Fallback               │
                           │                 │
                           ▼                 │
[Enforcement Macros]  <──────────────────────┘
  /enforce endpoint       Disable/enable specific apps
  Local Management        (disabled by default)
```

### Phone Endpoints (MacroDroid HTTP Server, port 7777)

| Endpoint | Purpose |
|----------|---------|
| `/enforce?action=disable&app=twitter` | Disable an app |
| `/enforce?action=enable&app=twitter` | Re-enable an app |
| `/enable-local` | Enable local fallback enforcement |
| `/disable-local` | Disable local fallback (server back) |
| `/notify?title=X&text=Y` | Show notification |
| `/list-exports` | List available macro exports |

### Server Endpoints (Token-API)

| Endpoint | Purpose |
|----------|---------|
| `POST /phone` | Receive app/geofence telemetry |

### Macro Categories

| Category | Count | Purpose |
|----------|-------|---------|
| Telemetry | 6 | Report app opens/closes to server |
| Geofence | 4 | Report location enter/exit |
| Enforcement | 6 | Enable/disable apps on command |
| Endpoints | 2 | HTTP API endpoints |
| Other | 4 | Misc (Spotify, YouTube toggle, etc.) |

See `macros/MACRODROID.md` for full macro inventory.

## Connection

All devices use `ssh-connect` (standardized SSH with redirect-on-exit).
Commands: `ssh-mac`, `ssh-wsl`, `ssh-phone` (each wraps `ssh-connect <target>`).

```bash
ssh-phone               # Interactive SSH to phone
ssh-phone "command"     # Run command on phone and exit
ssh-phone --proxy       # Nest SSH instead of redirecting (when already in SSH)
```

### Redirect-on-Exit Flow

When SSH'd into host A and you switch to host B, instead of nesting
(origin→A→B), the session redirects so you connect directly (origin→B).

```
  Phone                    Mac                      WSL
    │                       │                        │
    │──── ssh-mac ─────────>│                        │
    │     (wrapper loop)    │ (interactive session)  │
    │                       │                        │
    │                  user runs ssh-wsl             │
    │                       │                        │
    │                       │── reverse SSH ─────────│
    │<── "echo wsl > ──────"│   (not to WSL, back   │
    │     ~/.ssh-next"      │    to phone/origin)    │
    │                       │                        │
    │   (file written)      │── kill -HUP $PPID     │
    │                       ╳  (session closes)      │
    │                                                │
    │   wrapper loop wakes                           │
    │   reads ~/.ssh-next = "wsl"                    │
    │   rm ~/.ssh-next                               │
    │                                                │
    │──── ssh wsl (direct) ─────────────────────────>│
    │     (no nesting!)     │                        │
```

**Key mechanisms:**
- `$SSH_CLIENT` IP → `ip_to_host()` → identifies origin device
- Reverse SSH writes `~/.ssh-next` on origin (not `/tmp`, Termux can't write there)
- `kill -HUP $PPID` auto-closes the SSH session
- Origin's wrapper loop picks up the redirect file and connects directly
- `--proxy` skips all of this and nests normally

## MacroDroid Automation

MacroDroid is an Android automation app. We can programmatically generate macros and push them to the phone.

### CLI Tools

| Command | Description |
|---------|-------------|
| `macrodroid-gen` | Generate .macro files from YAML/JSON specs |
| `macrodroid-push` | Push .macro files to phone via SSH |
| `macrodroid-pull` | Pull files from phone via SSH |
| `macrodroid-read` | Parse and display .mdr backup files |
| `macrodroid-state` | Fetch and display current state from phone |

### Quick Start

```bash
# Show example spec
macrodroid-gen --example > my-macro.yaml

# Edit the spec, then generate and push
macrodroid-gen my-macro.yaml > my-macro.macro
macrodroid-push my-macro.macro

# Or pipeline in one command
macrodroid-gen my-macro.yaml | macrodroid-push - my-macro.macro
```

### Reading Current State

```bash
# Get current state (pulls latest .mdr from phone)
macrodroid-state                    # Summary view
macrodroid-state --detail           # Detailed view with action params
macrodroid-state --json             # JSON output
macrodroid-state --list             # List exports on phone

# Read a local .mdr file
macrodroid-read backup.mdr          # Summary
macrodroid-read backup.mdr --list   # Just macro names
macrodroid-read backup.mdr --detail # Full details
macrodroid-read backup.mdr --macro "Twitter Management"  # Specific macro
macrodroid-read backup.mdr --geofences   # Show geofences
macrodroid-read backup.mdr --http-config # Show HTTP server config

# Pull files from phone
macrodroid-pull --list              # List ~/macros/ on phone
macrodroid-pull --latest            # Pull most recent export
macrodroid-pull ~/macros/file.mdr   # Pull specific file
```

### Workflow for Updating Macros

1. **Get current state:**
   ```bash
   macrodroid-state --detail
   ```

2. **Create/modify a macro spec:**
   ```bash
   macrodroid-gen --example > new-macro.yaml
   # Edit new-macro.yaml
   ```

3. **Generate and push:**
   ```bash
   macrodroid-gen new-macro.yaml | macrodroid-push - new-macro.macro
   ```

4. **Import on phone:**
   - MacroDroid → Settings → Import/Export → Import
   - Select from ~/macros/

### Macro Spec Format (YAML)

```yaml
name: "My Macro"
category: "Automation"
description: "What this macro does"
enabled: true

# Local variables (optional)
variables:
  - name: response
    type: string
    value: ""

# Global variables (optional, for cross-macro state)
global_variables:
  - name: yt_bg
    type: boolean
    value: false

# Triggers - what starts the macro
triggers:
  - type: http_server
    identifier: "my-webhook"

# Actions - what the macro does
actions:
  - type: notification
    title: "Hello"
    text: "World"

# Constraints - conditions that must be met (optional)
constraints:
  - type: time_of_day
    start_hour: 9
    end_hour: 17
```

### Available Triggers

| Type | Description | Key Options |
|------|-------------|-------------|
| `http_server` | HTTP endpoint listener | `identifier`, `send_response` |
| `webhook` | External URL trigger | `identifier` |
| `geofence` | Location entry/exit | `geofence_id`, `enter` |
| `app_launched` | App open/close | `apps`, `packages`, `launched` |
| `time` | Scheduled time | `hour`, `minute`, `days` |
| `device_boot` | Device startup | - |
| `battery_level` | Battery threshold | `level`, `option` |
| `notification` | Notification received | `app_name`, `text_contains` |
| `shake` | Device shake | `sensitivity` |
| `screen_on` | Screen on/off | `screen_on` |
| `wifi_state` | WiFi changes | `state`, `ssids` |
| `music_playing` | Music starts/stops | `started` (bool) |
| `regular_interval` | Periodic timer | `interval`, `unit` |
| `swipe` | Screen swipe gesture | `area`, `motion` |
| `media_button` | Media button press | `option` |
| `bluetooth` | BT device event | `device`, `state` |

### Available Actions

| Type | Description | Key Options |
|------|-------------|-------------|
| `notification` | Show notification | `title`, `text`, `channel_type` |
| `http_request` | Make HTTP request | `url`, `method`, `body`, `headers` |
| `http_response` | Respond to HTTP trigger | `code`, `text` |
| `vibrate` | Vibrate device | `pattern` |
| `toast` | Show toast message | `text`, `duration` |
| `speak` | Text-to-speech | `text`, `speed`, `pitch` |
| `launch_app` | Launch application | `app`, `package` |
| `set_variable` | Set variable value | `name`, `value`, `var_type`, `local` |
| `wifi` | Enable/disable WiFi | `enable` |
| `torch` | Flashlight control | `state` (0=off, 1=on, 2=toggle) |
| `wait` | Pause execution | `seconds`, `milliseconds` |
| `shell` | Run shell command | `command`, `root`, `output_var` |
| `run_macro` | Trigger another macro | `macro_name`, `macro_guid` |
| `disable_app` | Enable/disable app | `apps`, `packages`, `disable` |
| `media_control` | Play/pause/next/prev | `option`, `app`, `package` |
| `launch_activity` | Launch app (reliable) | `app`, `package` |
| `if` | Start conditional block | `conditions`, `or_conditions` |
| `else_if` | Alternative conditional | `conditions` (same as `if`) |
| `else` | Alternative branch | - |
| `end_if` | Close if/else block | - |
| `wait_until` | Wait for trigger | `triggers` (embedded), `timeout` |
| `export_macros` | Export .mdr file | `filename`, `file_path` |
| `locale_plugin` | Plugin action | `package`, `blurb` |

### Available Constraints

| Type | Description | Key Options |
|------|-------------|-------------|
| `geofence` | Inside/outside location | `geofence_id`, `option` |
| `day_of_week` | Specific days | `days` (array of 7 bools) |
| `time_of_day` | Time range | `start_hour`, `end_hour` |
| `wifi` | WiFi state | `state`, `ssids` |
| `battery` | Battery level | `level`, `greater_than` |
| `variable` | Check variable value | `variable`, `var_type`, `comparison`, `value`, `local_var` |
| `bluetooth` | BT device state | `device`, `state` |
| `device_locked` | Screen lock state | `locked` (bool) |
| `music_active` | Music playing check | `playing` (bool) |

### File Locations

Macros are pushed to: `~/macros/` on the phone (Termux home)

To import in MacroDroid:
1. Settings → Export/Import → Import Macros
2. Use Termux file picker to browse to `~/macros/`
3. Select the .macro file

### .macro File Lifecycle

**Important:** `.macro` files are temporary staging files. After import into MacroDroid, they should be deleted because the macro is now stored in the `.mdr` export.

**Workflow:**
1. Generate: `macrodroid-gen spec.yaml > name.macro`
2. Push: `macrodroid-push name.macro`
3. Import in MacroDroid app
4. Verify with `macrodroid-state --list`
5. Delete: `rm name.macro` (local) and `ssh-phone "rm ~/macros/name.macro"` (phone)

**After importing**, delete the `.macro` file from the phone — it's just a staging file.

**The `.mdr` file is the source of truth** - it contains all imported macros. Use `macrodroid-read --refresh` to see current state.

### HTTP Server Trigger Details

MacroDroid's HTTP Server runs on port 7777. Endpoints are:
```
http://<phone-ip>:7777/<identifier>
http://<phone-ip>:7777/<identifier>?param=value
```

Available magic variables in actions:
- `{http_query_string}` - Full query string
- `{http_request_body}` - POST body content
- `{http_param=name}` - Specific query parameter

### Example: Webhook with Response

```yaml
name: "API Endpoint"
triggers:
  - type: http_server
    identifier: "status"
    send_response: true

actions:
  - type: set_variable
    name: "status"
    type: string
    value: '{"ok": true, "time": "{system_time}"}'

  - type: http_response
    code: "OK"
    text: "{lv=status}"
```

### Example: Notification Forwarder

```yaml
name: "Forward Notifications"
triggers:
  - type: notification
    app_name: "Gmail"
    text_contains: "urgent"

actions:
  - type: http_request
    url: "http://100.66.10.74:7777/api/notify"
    method: POST
    body: '{"title": "Gmail Alert", "message": "{notification_text}"}'
    content_type: "application/json"

constraints:
  - type: time_of_day
    start_hour: 9
    end_hour: 22
```

### Example: Conditional Logic (If/Else)

```yaml
name: "Enforcement Handler"
variables:
  - name: action
    type: string
  - name: app
    type: string

triggers:
  - type: http_server
    identifier: "enforce"
    send_response: true

actions:
  # Check if action is "disable"
  - type: if
    conditions:
      - type: variable
        variable: "action"
        var_type: string
        comparison: equals
        value: "disable"
        local_var: true

  # Nested check for which app
  - type: if
    conditions:
      - type: variable
        variable: "app"
        var_type: string
        comparison: equals
        value: "twitter"

  - type: disable_app
    apps: ["X"]
    packages: ["com.twitter.android"]
    disable: true

  - type: end_if

  - type: end_if  # end action == disable

  - type: http_response
    code: "OK"
    text: '{"status": "ok"}'
```

**Condition types:**
- `variable` - Check variable value (`comparison`: equals, not_equals, greater_than, less_than)
- `http_response` - Check HTTP response code

## Termux Configuration

### Templates

- `termux-bashrc-template` - Bash configuration with aliases and shortcuts
- `termux-properties-template` - Termux appearance/behavior settings

### Key Aliases (from bashrc)

```bash
ssh-mac         # SSH to Mac Mini (with redirect-on-exit)
ssh-wsl         # SSH to WSL (with redirect-on-exit)
fetch-bashrc    # Pull latest bashrc from Mac
```

### Storage Access

If MacroDroid can't access Termux private storage, run on phone:
```bash
termux-setup-storage
```

This creates `~/storage/` with symlinks to Downloads, DCIM, etc.
Then push to `~/storage/downloads/` for broader app access.

## Adding New Trigger/Action Types

The generator is extensible. To add new types, edit:
`~/Scripts/cli-tools/bin/macrodroid-gen`

Use existing builders as templates. The format is derived from MacroDroid's export format - export a macro with the desired action to see the JSON structure.
