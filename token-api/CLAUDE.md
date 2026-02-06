# Token-API Project

Local FastAPI server for Claude instance management, notifications, and system coordination.

## Architecture

- **Server**: `main.py` - FastAPI app on port 7777
- **TUI**: `token-api-tui.py` - Rich-based dashboard for monitoring instances
- **Database**: `~/.claude/agents.db` (SQLite, shared with Claude Code)

## Key Files

| File | Purpose |
|------|---------|
| `main.py` | FastAPI server (~3000 lines) |
| `token-api-tui.py` | TUI dashboard (~1500 lines) |
| `init_db.py` | Database initialization |
| `DESIGN.md` | Original design doc (partially outdated) |

## Database Tool

Query the local agents.db with the `agents-db` CLI:

```bash
agents-db instances              # Show all instances
agents-db events --limit 10      # Recent events
agents-db tables                 # List tables
agents-db describe claude_instances
agents-db query "SELECT * FROM events WHERE event_type='instance_renamed'"
agents-db --json instances       # JSON output
```

## Key Tables

### claude_instances
Core instance registry. Key columns:
- `id` - Instance UUID
- `tab_name` - Display name (set via rename, or auto "Claude HH:MM")
- `working_dir` - Instance working directory
- `status` - 'active' or 'stopped'
- `is_processing` - 1 when actively processing a prompt
- `device_id` - 'desktop', 'Token-S24', etc.

### events
Event log for instance lifecycle, renames, TTS, notifications.

## Core API Endpoints

### Instance Management
```
POST   /api/instances/register          # Register new instance
DELETE /api/instances/{id}              # Stop instance
PATCH  /api/instances/{id}/rename       # Rename (sets tab_name)
POST   /api/instances/{id}/activity     # Update processing state
POST   /api/instances/{id}/unstick     # Nudge stuck instance (?level=1 SIGWINCH, ?level=2 SIGINT)
GET    /api/instances/{id}/diagnose    # Get detailed process diagnostics
GET    /api/instances                   # List all instances
GET    /api/instances/{id}/todos        # Get instance task list
```

### Notifications
```
POST   /api/notify                      # Send notification
POST   /api/notify/tts                  # TTS only
POST   /api/notify/sound                # Sound only
GET    /api/notify/queue/status         # TTS queue status
POST   /api/tts/skip                    # Skip current TTS (?clear_queue=true to clear queue)
```

### System
```
GET    /api/dashboard                   # Dashboard data
GET    /api/work-mode                   # Current work mode
POST   /api/headless                    # Toggle headless mode
GET    /health                          # Health check
```

## Instance Naming

The `tab_name` field stores the instance display name. The TUI displays:
1. Custom `tab_name` (if user renamed it)
2. `working_dir` path (if using default "Claude HH:MM" name)

Auto-generated names match pattern `Claude HH:MM`. Any other name is considered custom.

Rename via:
- TUI: Press `r` on selected instance
- CLI: `instance-name "my-name"`
- API: `PATCH /api/instances/{id}/rename` with `{"tab_name": "..."}`

## TUI Controls

```
↑↓ / jk  - Navigate instances (up/down)
h / l    - Switch info panel (Events ↔ Logs)
r        - Rename selected
s        - Stop selected
d        - Delete (with confirm)
y        - Copy resume command to clipboard (yank)
U        - Unstick frozen instance (SIGWINCH, gentle nudge)
I        - Interrupt frozen instance (SIGINT, cancel current op)
K        - Kill frozen instance (SIGKILL, auto-copies resume cmd)
c        - Clear all stopped
o        - Change sort order
x        - Skip current TTS
X        - Skip TTS and clear queue
R        - Restart server
q        - Quit
```

### Info Panel Pages

The TUI has a paginated info panel (toggled with H/L):
- **Page 0 (Events)**: Recent events from the database (registrations, stops, renames, TTS)
- **Page 1 (Logs)**: Server logs from the API

The current page is shown in the status bar.

## Common Debug Patterns

```bash
# Check if server is running
curl http://localhost:7777/health

# View active instances
agents-db instances

# Check recent events
agents-db events --limit 20

# Verify rename worked
agents-db query "SELECT id, tab_name FROM claude_instances WHERE id='...'"

# Watch server logs (if running via systemd)
journalctl -u token-api -f

# Test TTS (wait 10s after for user feedback)
curl -s http://localhost:7777/api/notify/test | jq .
# Then sleep 10 and ask user if they heard it
```

**Testing TTS:** After running a TTS test, sleep for ~10 seconds before continuing so the user can confirm whether they heard sound and/or speech.

## Profile System

4 static profiles assigned round-robin to new instances:
- profile_1: David voice, chimes.wav
- profile_2: Zira voice, notify.wav
- profile_3: David voice, ding.wav
- profile_4: Zira voice, tada.wav

## CLI Tools

### Token-API Specific

| Command | Purpose |
|---------|---------|
| `agents-db` | Query local agents.db database |
| `token-status` | Quick server status check |
| `token-restart` | Restart the server (systemd) |
| `notify-test` | Send test notifications |
| `tts-skip` | Skip current TTS (--all to clear queue) |
| `instance-name` | Rename current session |
| `instance-stop` | Stop/unstick/kill instance by name (fuzzy match) |
| `instances-clear` | Bulk clear stopped instances |

### General (also useful here)

| Command | Purpose |
|---------|---------|
| `deploy local` | Run local dev server with ngrok |
| `test` | Send test messages to local server |

### Examples

```bash
# Quick status check
token-status

# Restart the server
token-restart                    # Restart via systemd
token-restart --watch            # Restart and tail logs
token-restart --status           # Check status only

# Stop/unstick/kill instances
instance-stop "auth-refactor"    # Stop by name
instance-stop --unstick "auth"   # Nudge stuck instance (L1, SIGWINCH)
instance-stop --unstick=2 "auth" # Interrupt stuck instance (L2, SIGINT)
instance-stop --kill "auth"      # Kill frozen instance (SIGKILL, shows resume cmd)
instance-stop --diagnose "auth"  # Show process state, wchan, children, FDs
instance-stop --list             # List active instances
instances-clear                  # Preview stopped instances
instances-clear --confirm        # Delete stopped instances

# Test TTS
notify-test "Hello from Token-API"

# Test sound only
notify-test --sound-only

# Skip TTS
tts-skip                         # Skip current TTS
tts-skip --all                   # Skip and clear queue

# Query database
agents-db events --limit 5
```

## Known Issues & Fixes

### Display Name Priority (Fixed 2026-01-26)
The `format_instance_name()` function in the TUI now correctly prioritizes custom `tab_name` over `working_dir`. Previously, renamed instances still showed the directory path.

Location: `token-api-tui.py:280` - `is_custom_tab_name()` and `format_instance_name()`

### Backspace in TUI Rename
The TUI rename input captures raw terminal characters. Backspace (`\x7f`) may appear in names if terminal handling is imperfect. Workaround: use `instance-name` CLI instead.

### is_processing Flag Not Persisting (Fixed 2026-01-26)
Three bugs caused the green arrow (processing indicator) to not display properly:

1. **PostToolUse clearing flag**: The `handle_post_tool_use()` was setting `is_processing=0` on every tool use, immediately clearing the flag set by `prompt_submit`. Fixed to only update `last_activity` as a heartbeat.

2. **Timezone mismatch in stale worker**: The `clear_stale_processing_flags()` worker compared Python local timestamps against SQLite UTC time, causing a 7-hour offset. All flags appeared "stale" immediately. Fixed by adding `'localtime'` to the SQLite datetime comparison.

3. **Todos endpoint wrong path**: The `/api/instances/{id}/todos` endpoint looked in `~/.claude/todos/` (old format) instead of `~/.claude/tasks/{id}/` (new TaskCreate format). Fixed to read individual task JSON files from the correct location.

Location: `main.py` - `handle_post_tool_use()`, `clear_stale_processing_flags()`, `get_instance_todos()`

### TUI Todo Caching (Added 2026-01-26)
The TUI now caches todo data per instance. When `is_processing=0`, it displays cached data instead of empty values. This prevents progress/task columns from disappearing between prompts.

Location: `token-api-tui.py` - `todos_cache` global, `get_instance_todos()` with `use_cache` parameter

## Development Notes

- Server runs on port 7777 (hardcoded in `main.py`)
- TUI polls database directly, not via API (for speed)
- TUI refresh interval: 2 seconds
- Database changes from CLI/API are picked up on next TUI refresh

## Potential Future Tools/Skills

- **/token-debug skill**: Interactive debugging workflow
