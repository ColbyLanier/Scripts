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
GET    /api/instances                   # List all instances
GET    /api/instances/{id}/todos        # Get instance task list
```

### Notifications
```
POST   /api/notify                      # Send notification
POST   /api/notify/tts                  # TTS only
POST   /api/notify/sound                # Sound only
GET    /api/notify/queue/status         # TTS queue status
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
↑↓ / jk  - Navigate instances
r        - Rename selected
s        - Stop selected
d        - Delete (with confirm)
c        - Clear all stopped
o        - Change sort order
R        - Restart server
q        - Quit
```

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
```

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
| `notify-test` | Send test notifications |
| `instance-name` | Rename current session |

### General (also useful here)

| Command | Purpose |
|---------|---------|
| `deploy local` | Run local dev server with ngrok |
| `test` | Send test messages to local server |

### Examples

```bash
# Quick status check
token-status

# Continuous monitoring
token-status --watch

# Test TTS
notify-test "Hello from Token-API"

# Test sound only
notify-test --sound-only

# Query database
agents-db events --limit 5
```

## Known Issues & Fixes

### Display Name Priority (Fixed 2026-01-26)
The `format_instance_name()` function in the TUI now correctly prioritizes custom `tab_name` over `working_dir`. Previously, renamed instances still showed the directory path.

Location: `token-api-tui.py:280` - `is_custom_tab_name()` and `format_instance_name()`

### Backspace in TUI Rename
The TUI rename input captures raw terminal characters. Backspace (`\x7f`) may appear in names if terminal handling is imperfect. Workaround: use `instance-name` CLI instead.

## Development Notes

- Server runs on port 7777 (hardcoded in `main.py`)
- TUI polls database directly, not via API (for speed)
- TUI refresh interval: 2 seconds
- Database changes from CLI/API are picked up on next TUI refresh

## Potential Future Tools/Skills

- **instance-stop**: Stop instance by name (fuzzy match)
- **clear-stopped**: Bulk clear stopped instances
- **/token-debug skill**: Interactive debugging workflow
