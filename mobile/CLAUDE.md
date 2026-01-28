# Mobile Development Tools

Tools and configuration for phone automation via Termux and MacroDroid.

## Overview

This directory contains:
- Termux shell configuration templates
- Documentation for mobile automation tooling

The phone (Samsung S24) connects via Tailscale and runs Termux for SSH access.

## Connection

```bash
sshp                    # Interactive SSH to phone
sshp "command"          # Run command on phone
ssh phone               # Direct SSH (same as sshp)
```

Connection details are in the global CLAUDE.md under "Phone Access with `sshp`".

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
| `set_variable` | Set variable value | `name`, `value`, `type` |
| `wifi` | Enable/disable WiFi | `enable` |
| `torch` | Flashlight control | `state` (0=off, 1=on, 2=toggle) |
| `wait` | Pause execution | `seconds`, `milliseconds` |
| `shell` | Run shell command | `command`, `root`, `output_var` |
| `run_macro` | Trigger another macro | `macro_name`, `macro_guid` |
| `disable_app` | Enable/disable app | `apps`, `packages`, `disable` |
| `media_control` | Play/pause/next/prev | `option`, `app`, `package` |
| `launch_activity` | Launch app (reliable) | `app`, `package` |
| `if` | Start conditional block | `conditions`, `or_conditions` |
| `else` | Alternative branch | - |
| `end_if` | Close if/else block | - |

### Available Constraints

| Type | Description | Key Options |
|------|-------------|-------------|
| `geofence` | Inside/outside location | `geofence_id`, `option` |
| `day_of_week` | Specific days | `days` (array of 7 bools) |
| `time_of_day` | Time range | `start_hour`, `end_hour` |
| `wifi` | WiFi state | `state`, `ssids` |
| `battery` | Battery level | `level`, `greater_than` |

### File Locations

Macros are pushed to: `~/macros/` on the phone (Termux home)

To import in MacroDroid:
1. Settings → Export/Import → Import Macros
2. Use Termux file picker to browse to `~/macros/`
3. Select the .macro file

### HTTP Server Trigger Details

MacroDroid's HTTP Server runs on a configurable port (default 8080). Endpoints are:
```
http://<phone-ip>:<port>/<identifier>
http://<phone-ip>:<port>/<identifier>?param=value
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
sshpc           # SSH back to desktop
fetch-bashrc    # Pull latest bashrc from desktop
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
