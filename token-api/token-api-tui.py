#!/usr/bin/env python3
"""
Token-API TUI: Terminal dashboard for Claude instance management.

Connects to existing Token-API server running on port 7777.

Controls:
  arrow/jk  - Select instance (up/down)
  g/G       - Jump to first/last instance
  h/l       - Switch info panel (Events/Logs/Deploy)
  Enter     - Open selected instance in new terminal tab
  r         - Rename selected instance
  y         - Copy resume command to clipboard (yank)
  v         - Change voice for instance
  f         - Cycle filter (all/active/stopped)
  s         - Stop selected instance
  d         - Delete selected instance
  U         - Unstick frozen instance (SIGWINCH, gentle nudge)
  I         - Interrupt frozen instance (SIGINT, cancel op)
  K         - Kill deadlocked instance (SIGKILL, preserves terminal for /resume)
  R         - Restart Token-API server
  Ctrl+R    - Full refresh (restart server + reload TUI code)
  c         - Clear all instances
  o         - Change sort order
  q         - Quit
"""

import sys
import os
import re
import argparse
import json
import sqlite3
import subprocess
import time
import threading
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional

# Add the script directory to path for imports
SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.layout import Layout
from rich.live import Live
from rich.text import Text
from rich.prompt import Prompt
from rich.highlighter import JSONHighlighter

# API configuration
API_URL = "http://localhost:7777"
SERVER_PORT = 7777

# Configuration
DB_PATH = Path.home() / ".claude" / "agents.db"
REFRESH_INTERVAL = 2  # seconds
TIMER_STATE_PATH = Path("/mnt/c/Users/colby/Documents/Obsidian/Token-ENV/Scripts/timer-state.json")


# Resume copy feedback state
resume_feedback: Optional[tuple[float, str]] = None  # (timestamp, message)

# Unstick feedback state
unstick_feedback: Optional[tuple[float, str]] = None  # (timestamp, message)

# Restart feedback state
restart_feedback: Optional[tuple[float, str]] = None  # (timestamp, message)

# Timer/mode display cache
timer_display_cache = {
    "break_seconds": 0,
    "mode": "silence",
    "work_mode": "clocked_in",
    "last_fetch": 0
}

# Layout detection thresholds
MOBILE_TAILSCALE_IP = "100.102.92.24"
MOBILE_WIDTH_THRESHOLD = 60  # Below this = mobile mode
COMPACT_WIDTH_THRESHOLD = 100  # Below this (but above mobile) = compact mode
# Vertical threshold: character cells are ~2x taller than wide, so a "square" terminal
# in pixels has aspect ratio ~2.0 in characters. We favor vertical mode - only clearly
# wide terminals (aspect > 2.5) get full mode. Square-ish terminals stay vertical.
VERTICAL_ASPECT_RATIO_THRESHOLD = 2.5

# Global state
selected_index = 0
instances_cache = []
todos_cache = {}  # instance_id -> last known todos data (persists when not polling)
api_healthy = True
api_error_message = None
layout_mode = "full"  # "mobile", "vertical", "compact", or "full"
layout_mode_forced = False  # True if user used --mobile, --vertical, --compact, or --no-mobile
sort_mode = "recent_activity"  # "status", "recent_activity", "recent_stopped", "created"
filter_mode = "all"  # "all", "active", "stopped"
panel_page = 0  # 0 = events view, 1 = server logs view, 2 = deploy logs view
PANEL_PAGE_MAX = 2  # 0=Events, 1=Logs, 2=Deploy
deploy_active = False
deploy_log_path = None
deploy_metadata = {}
deploy_previous_page = 0
deploy_auto_switched = False
DEPLOY_SCAN_DIR = Path.home() / "ProcAgentDir"
console = Console()


def detect_layout_mode() -> str:
    """Detect layout mode: 'mobile', 'vertical', 'compact', or 'full'.

    Priority:
    1. Phone SSH always gets mobile
    2. Very narrow (<60) always gets mobile
    3. Vertical/square orientation (aspect < 2.5) gets vertical (stacked panels)
    4. Medium width (60-100) gets compact (no sidebar)
    5. Wide + normal aspect gets full

    Note: Character cells are ~2x taller than wide, so a "square" terminal in pixels
    has aspect ratio ~2.0 in character terms. We favor vertical mode (threshold 2.5)
    so only clearly wide terminals get full mode.
    """
    ssh_client = os.environ.get("SSH_CLIENT", "")

    # Phone always mobile
    if ssh_client.startswith(MOBILE_TAILSCALE_IP + " "):
        return "mobile"

    width = console.size.width
    height = console.size.height

    # Very narrow always mobile
    if width < MOBILE_WIDTH_THRESHOLD:
        return "mobile"

    # Check vertical orientation (tall terminal in character terms)
    is_vertical = height > 0 and (width / height) < VERTICAL_ASPECT_RATIO_THRESHOLD

    # Vertical monitor gets dedicated vertical mode (stacked panels)
    if is_vertical:
        return "vertical"

    # Medium width gets compact (no sidebar but horizontal header)
    if width < COMPACT_WIDTH_THRESHOLD:
        return "compact"

    return "full"


ANSI_ESCAPE_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')


def strip_ansi(text: str) -> str:
    """Strip ANSI escape codes from text."""
    return ANSI_ESCAPE_RE.sub('', text)


def check_deploy_status() -> tuple[bool, Path | None, dict]:
    """Check for active deployment by scanning for .claude-deploy-signal files."""
    try:
        if not DEPLOY_SCAN_DIR.exists():
            return False, None, {}
        for entry in DEPLOY_SCAN_DIR.iterdir():
            if entry.is_dir():
                signal = entry / ".claude-deploy-signal"
                if signal.exists():
                    log = entry / ".claude-deploy.log"
                    try:
                        metadata = json.loads(signal.read_text())
                    except Exception:
                        metadata = {}
                    return True, log, metadata
    except Exception:
        pass
    return False, None, {}


def check_api_health() -> tuple[bool, str | None]:
    """Check if the API server is reachable."""
    try:
        req = urllib.request.Request(f"{API_URL}/api/instances", method="GET")
        with urllib.request.urlopen(req, timeout=3) as response:
            if response.status == 200:
                return True, None
            return False, f"API returned status {response.status}"
    except urllib.error.URLError as e:
        if "Connection refused" in str(e):
            return False, f"API server not running (port 7777)"
        return False, f"Cannot reach API: {e.reason}"
    except Exception as e:
        return False, f"Health check failed: {str(e)}"


def get_db_connection():
    """Get a database connection."""
    return sqlite3.connect(DB_PATH)


def format_duration(start_time_str: str, end_time_str: str = None) -> str:
    """Format duration from start time to now or end time."""
    try:
        start = datetime.fromisoformat(start_time_str.replace("Z", "+00:00").replace("T", " ").split(".")[0])
        if end_time_str:
            end = datetime.fromisoformat(end_time_str.replace("Z", "+00:00").replace("T", " ").split(".")[0])
        else:
            end = datetime.now()

        delta = end - start
        total_seconds = int(delta.total_seconds())

        if total_seconds < 0:
            return "0m"

        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60

        if hours > 0:
            return f"{hours}h {minutes}m"
        else:
            return f"{minutes}m"
    except Exception:
        return "?"


def format_duration_colored(start_time_str: str, end_time_str: str = None) -> str:
    """Format duration with color based on age: green <30m, yellow 30m-2h, dim >2h."""
    duration = format_duration(start_time_str, end_time_str)
    try:
        start = datetime.fromisoformat(start_time_str.replace("Z", "+00:00").replace("T", " ").split(".")[0])
        end = datetime.fromisoformat(end_time_str.replace("Z", "+00:00").replace("T", " ").split(".")[0]) if end_time_str else datetime.now()
        total_minutes = int((end - start).total_seconds()) // 60
    except Exception:
        return duration
    if total_minutes < 30:
        return f"[green]{duration}[/green]"
    elif total_minutes < 120:
        return f"[yellow]{duration}[/yellow]"
    else:
        return f"[dim]{duration}[/dim]"


def filter_instances(instances: list) -> list:
    """Filter instances based on current filter_mode."""
    if filter_mode == "all":
        return instances
    elif filter_mode == "active":
        return [i for i in instances if i.get("status") in ("processing", "idle")]
    elif filter_mode == "stopped":
        return [i for i in instances if i.get("status") == "stopped"]
    return instances


def is_custom_tab_name(tab_name: str) -> bool:
    """Check if tab_name is a custom name (not auto-generated like 'Claude HH:MM')."""
    import re
    if not tab_name:
        return False
    # Auto-generated names match "Claude HH:MM" pattern
    if re.match(r'^Claude \d{2}:\d{2}$', tab_name):
        return False
    return True


def format_instance_name(instance: dict, max_len: int = 20) -> str:
    """Format instance name, prioritizing custom tab_name over working_dir."""
    tab_name = instance.get("tab_name", "")

    # If user has set a custom name, always use it
    if is_custom_tab_name(tab_name):
        if len(tab_name) > max_len:
            return tab_name[:max_len - 3] + "..."
        return tab_name

    # Otherwise derive from working_dir
    working_dir = instance.get("working_dir")
    if working_dir:
        # Extract the last 2-3 path components for a readable name
        parts = working_dir.rstrip("/").split("/")
        # Filter out empty parts and common prefixes like 'home', 'mnt', 'c', etc.
        parts = [p for p in parts if p and p not in ("home", "mnt", "c", "Users")]
        if len(parts) >= 2:
            name = "/".join(parts[-2:])  # Last two components
        elif parts:
            name = parts[-1]
        else:
            name = working_dir
        if len(name) > max_len:
            name = "..." + name[-(max_len - 3):]
        return name
    # Fallback to tab_name or id
    return tab_name or instance.get("id", "?")[:max_len]


def get_instances():
    """Fetch all instances from the database with current sort order."""
    try:
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Build ORDER BY based on sort_mode
        order_clauses = {
            "status": "status ASC, last_activity DESC",
            "recent_activity": "last_activity DESC",
            "recent_stopped": "stopped_at DESC NULLS LAST, last_activity DESC",
            "created": "registered_at DESC"
        }
        order_by = order_clauses.get(sort_mode, "status ASC, last_activity DESC")

        cursor.execute(f"""
            SELECT * FROM claude_instances
            ORDER BY {order_by}
        """)
        rows = cursor.fetchall()
        conn.close()

        return [dict(row) for row in rows]
    except Exception:
        return []


def get_instance_todos(instance_id: str, use_cache: bool = False) -> dict:
    """Fetch todos for an instance from the API.

    If use_cache=True and data is cached, returns cached data without polling.
    If use_cache=True but no cached data exists, fetches fresh data to seed the cache.
    If use_cache=False, always fetches fresh data and updates the cache.
    """
    global todos_cache
    default = {"progress": 0, "current_task": None, "total": 0, "todos": []}

    if use_cache and instance_id in todos_cache:
        return todos_cache[instance_id]

    try:
        req = urllib.request.Request(f"{API_URL}/api/instances/{instance_id}/todos")
        with urllib.request.urlopen(req, timeout=2) as response:
            data = json.loads(response.read().decode())
            todos_cache[instance_id] = data  # Update cache with fresh data
            return data
    except Exception:
        return todos_cache.get(instance_id, default)  # On error, return cached or default


def rename_instance(instance_id: str, new_name: str) -> bool:
    """Rename an instance via the API."""
    try:
        data = json.dumps({"tab_name": new_name}).encode()
        req = urllib.request.Request(
            f"{API_URL}/api/instances/{instance_id}/rename",
            data=data,
            headers={"Content-Type": "application/json"},
            method="PATCH"
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            result = json.loads(response.read().decode())
            return result.get("status") == "renamed"
    except Exception:
        return False


def delete_instance(instance_id: str) -> bool:
    """Delete/stop an instance via the API."""
    try:
        req = urllib.request.Request(
            f"{API_URL}/api/instances/{instance_id}",
            method="DELETE"
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            result = json.loads(response.read().decode())
            return result.get("status") == "stopped"
    except Exception:
        return False


def kill_instance(instance_id: str) -> dict:
    """Kill a frozen instance via the API. Returns result dict or None on failure."""
    try:
        req = urllib.request.Request(
            f"{API_URL}/api/instances/{instance_id}/kill",
            method="POST",
            headers={"Content-Type": "application/json"},
            data=b"{}"
        )
        resp = urllib.request.urlopen(req, timeout=20)  # longer timeout for SIGINT×2 sequence
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
            return {"status": "error", "detail": body.get("detail", str(e))}
        except Exception:
            return {"status": "error", "detail": str(e)}
    except Exception:
        return None


def unstick_instance(instance_id: str, level: int = 1) -> dict:
    """Nudge a stuck instance. Level 1=SIGWINCH (gentle), Level 2=SIGINT (cancel op). Returns result dict or None on failure."""
    try:
        req = urllib.request.Request(
            f"{API_URL}/api/instances/{instance_id}/unstick?level={level}",
            method="POST",
            headers={"Content-Type": "application/json"},
            data=b"{}"
        )
        resp = urllib.request.urlopen(req, timeout=10)  # 4s server wait + margin
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
            return {"status": "error", "detail": body.get("detail", str(e))}
        except Exception:
            return {"status": "error", "detail": str(e)}
    except Exception:
        return None


def copy_to_clipboard(text: str) -> tuple[bool, str]:
    """Copy text to clipboard. Returns (success, message)."""
    # Try clip.exe first (WSL)
    try:
        subprocess.run(["clip.exe"], input=text, text=True, check=True, timeout=2)
        return (True, "Copied to clipboard")
    except FileNotFoundError:
        pass
    except Exception as e:
        pass

    # Try xclip
    try:
        subprocess.run(["xclip", "-selection", "clipboard"], input=text, text=True, check=True, timeout=2)
        return (True, "Copied to clipboard")
    except FileNotFoundError:
        pass
    except Exception as e:
        pass

    # Try xsel
    try:
        subprocess.run(["xsel", "--clipboard", "--input"], input=text, text=True, check=True, timeout=2)
        return (True, "Copied to clipboard")
    except FileNotFoundError:
        pass
    except Exception as e:
        return (False, f"Copy failed: {str(e)[:25]}")

    return (False, "No clipboard tool (need clip.exe/xclip/xsel)")


def get_available_voices() -> list:
    """Get list of available voices from the API."""
    try:
        req = urllib.request.Request(f"{API_URL}/api/voices")
        with urllib.request.urlopen(req, timeout=5) as response:
            result = json.loads(response.read().decode())
            return result.get("voices", [])
    except Exception:
        return []


def change_instance_voice(instance_id: str, voice: str) -> dict:
    """Change an instance's TTS voice via the API.

    Returns dict with 'success', 'changes' (list of bumps), or None on error.
    """
    try:
        data = json.dumps({"voice": voice}).encode()
        req = urllib.request.Request(
            f"{API_URL}/api/instances/{instance_id}/voice",
            data=data,
            headers={"Content-Type": "application/json"},
            method="PATCH"
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            result = json.loads(response.read().decode())
            if result.get("status") in ("voice_changed", "no_change"):
                return {
                    "success": True,
                    "changes": result.get("changes", []),
                    "status": result.get("status")
                }
            return {"success": False}
    except Exception:
        return {"success": False}


def delete_all_instances() -> tuple[bool, int]:
    """Delete all instances via the API. Returns (success, count)."""
    try:
        req = urllib.request.Request(
            f"{API_URL}/api/instances/all",
            method="DELETE"
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            result = json.loads(response.read().decode())
            if result.get("status") in ("deleted_all", "no_instances"):
                return True, result.get("deleted_count", 0)
            return False, 0
    except Exception:
        return False, 0


def get_recent_events(limit: int = 5):
    """Fetch recent events from the database with instance names."""
    try:
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("""
            SELECT e.*, ci.tab_name as instance_tab_name, ci.working_dir as instance_working_dir
            FROM events e
            LEFT JOIN claude_instances ci ON e.instance_id = ci.id
            ORDER BY e.created_at DESC
            LIMIT ?
        """, (limit,))
        rows = cursor.fetchall()
        conn.close()

        events = []
        for row in rows:
            event = dict(row)
            if event.get("details"):
                try:
                    event["details"] = json.loads(event["details"])
                except:
                    pass
            events.append(event)
        return events
    except Exception:
        return []


def format_event_instance_name(event: dict, max_len: int = 15) -> str:
    """Format instance name for event display using joined instance data or fallbacks."""
    instance_id = event.get("instance_id", "")
    details = event.get("details", {}) if isinstance(event.get("details"), dict) else {}

    # First check joined instance data (from LEFT JOIN)
    tab_name = event.get("instance_tab_name")
    working_dir = event.get("instance_working_dir")

    # If instance still exists and has a custom name, use it
    if is_custom_tab_name(tab_name):
        if len(tab_name) > max_len:
            return tab_name[:max_len - 2] + ".."
        return tab_name

    # Check details for name (some events store it there)
    details_name = details.get("tab_name") or details.get("new_name")
    if is_custom_tab_name(details_name):
        if len(details_name) > max_len:
            return details_name[:max_len - 2] + ".."
        return details_name

    # Derive from working_dir if available
    if working_dir:
        parts = working_dir.rstrip("/").split("/")
        parts = [p for p in parts if p and p not in ("home", "mnt", "c", "Users")]
        if parts:
            name = parts[-1]
            if len(name) > max_len:
                name = name[:max_len - 2] + ".."
            return name

    # Fallback to truncated ID
    if instance_id:
        return instance_id[:8] + ".." if len(instance_id) > 10 else instance_id
    return "system"


def get_tts_queue_status():
    """Fetch TTS queue status from the API."""
    try:
        req = urllib.request.Request(f"{API_URL}/api/notify/queue/status")
        with urllib.request.urlopen(req, timeout=2) as response:
            return json.loads(response.read().decode())
    except Exception:
        return {"current": None, "queue": [], "queue_length": 0}


def get_timer_state() -> dict:
    """Read Obsidian timer-state.json for break availability."""
    try:
        if TIMER_STATE_PATH.exists():
            with open(TIMER_STATE_PATH, 'r') as f:
                return json.load(f)
    except Exception:
        pass
    return {"breakAvailableSeconds": 0, "currentMode": "work_silence"}


def get_desktop_mode() -> dict:
    """Fetch current desktop/work mode from API."""
    try:
        req = urllib.request.Request(f"{API_URL}/api/work-mode")
        with urllib.request.urlopen(req, timeout=1) as response:
            return json.loads(response.read().decode())
    except Exception:
        return {"work_mode": "clocked_in", "current_timer_mode": "silence"}


def format_break_time(seconds: int) -> str:
    """Format break time as HH:MM:SS or MM:SS."""
    if seconds <= 0:
        return "00:00"
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def get_timer_header_text() -> Text:
    """Generate timer/mode display for header."""
    global timer_display_cache

    # Fetch fresh data
    timer_state = get_timer_state()
    desktop_mode = get_desktop_mode()

    break_secs = timer_state.get("breakAvailableSeconds", 0)
    obsidian_mode = timer_state.get("currentMode", "work_silence")
    work_mode = desktop_mode.get("work_mode", "clocked_in")

    # Mode icons
    mode_icons = {
        "work_silence": "🔇",
        "work_music": "🎵",
        "work_video": "📺",
        "work_gaming": "🎮",
        "gym": "🏋️",
    }

    # Parse obsidian mode for display
    icon = mode_icons.get(obsidian_mode, "❓")
    mode_name = obsidian_mode.replace("work_", "").replace("_", " ").title()

    # Break time color based on amount
    if break_secs > 1800:  # > 30 min
        break_color = "green"
    elif break_secs > 300:  # > 5 min
        break_color = "yellow"
    else:
        break_color = "red"

    break_str = format_break_time(break_secs)

    # Work mode indicator
    if work_mode == "clocked_out":
        work_indicator = "[dim]OFF[/dim]"
    elif work_mode == "gym":
        work_indicator = "[magenta]GYM[/magenta]"
    else:
        work_indicator = ""

    # Build display text
    text = Text()
    text.append(f"{icon} ", style="bold")
    text.append(f"{mode_name}", style="bold white")
    text.append("  ", style="dim")
    text.append("⏱ ", style="dim")
    text.append(break_str, style=f"bold {break_color}")
    if work_indicator:
        text.append(f"  {work_indicator}")

    return text


def make_progress_bar(progress: int, width: int = 10) -> str:
    """Create a text-based progress bar."""
    if progress == 0:
        return "[dim]" + "─" * width + "[/dim]"

    filled = int(width * progress / 100)
    empty = width - filled

    if progress == 100:
        return f"[green]{'█' * filled}[/green]"
    else:
        return f"[cyan]{'█' * filled}[/cyan][dim]{'─' * empty}[/dim]"


def create_instances_table(instances: list, selected_idx: int) -> Table:
    """Create the instances table with selection and todo progress."""
    max_name_len = 15
    for inst in instances:
        name = format_instance_name(inst, max_len=30)
        max_name_len = max(max_name_len, len(name) + 2)

    table = Table(
        show_header=True,
        header_style="bold cyan",
        border_style="blue",
        expand=False
    )

    table.add_column("", width=2, justify="center")
    table.add_column("●", style="dim", width=1, justify="center")
    table.add_column("Name", style="white", width=max_name_len)
    table.add_column("Device", style="yellow", width=10)
    table.add_column("Progress", width=14)
    table.add_column("Task", style="dim", min_width=20, max_width=30)
    table.add_column("Time", width=6, justify="right")

    for i, instance in enumerate(instances):
        selector = "[yellow]>[/yellow]" if i == selected_idx else " "
        name = format_instance_name(instance, max_len=30)
        if i == selected_idx:
            name = f"[bold yellow]{name}[/bold yellow]"

        device = instance.get("device_id", "?")
        instance_id = instance.get("id", "")
        status = instance.get("status", "idle")
        # Poll for fresh todos when processing, otherwise use cached data
        if status == "processing":
            todos = get_instance_todos(instance_id, use_cache=False)
        else:
            todos = get_instance_todos(instance_id, use_cache=True)

        has_active_subtask = todos.get("current_task") is not None

        if status == "stopped":
            status_icon = "[dim]o[/dim]"
        elif status == "processing" or has_active_subtask:
            status_icon = "[green]>[/green]"
        else:
            status_icon = "[cyan]*[/cyan]"

        if todos.get("total", 0) > 0:
            progress = todos.get("progress", 0)
            progress_bar = make_progress_bar(progress, 8)
            progress_text = f"{progress_bar} {progress}%"
        else:
            progress_text = "[dim]-[/dim]"

        current_task = todos.get("current_task", "")
        if current_task:
            if len(current_task) > 28:
                current_task = current_task[:25] + "..."
            current_task = f"[italic]{current_task}[/italic]"
        else:
            current_task = "[dim]-[/dim]"

        end_time = instance.get("stopped_at") if instance["status"] == "stopped" else None
        duration = format_duration_colored(instance.get("registered_at", ""), end_time)

        table.add_row(selector, status_icon, name, device, progress_text, current_task, duration)

    if not instances:
        table.add_row(" ", "[dim]-[/dim]", "[dim]No instances[/dim]", "-", "-", "-", "-")

    return table


def create_mobile_instances_table(instances: list, selected_idx: int) -> Table:
    """Create a compact instances table for mobile."""
    table = Table(
        title="Instances [dim](jk r s d c o q)[/dim]",
        show_header=True,
        header_style="bold cyan",
        border_style="blue",
        expand=True,
        padding=(0, 0)
    )

    table.add_column("", width=1, justify="center")
    table.add_column("*", width=1, justify="center")
    table.add_column("Name", style="white", no_wrap=True, max_width=20)
    table.add_column("Prog", width=6)
    table.add_column("T", width=4, justify="right")

    for i, instance in enumerate(instances):
        selector = "[yellow]>[/yellow]" if i == selected_idx else " "
        name = format_instance_name(instance, max_len=18)
        if i == selected_idx:
            name = f"[bold yellow]{name}[/bold yellow]"

        instance_id = instance.get("id", "")
        status = instance.get("status", "idle")
        # Poll for fresh todos when processing, otherwise use cached data
        if status == "processing":
            todos = get_instance_todos(instance_id, use_cache=False)
        else:
            todos = get_instance_todos(instance_id, use_cache=True)

        has_active_subtask = todos.get("current_task") is not None

        if status == "stopped":
            status_icon = "[dim]o[/dim]"
        elif status == "processing" or has_active_subtask:
            status_icon = "[green]>[/green]"
        else:
            status_icon = "[cyan]*[/cyan]"

        if todos.get("total", 0) > 0:
            progress = todos.get("progress", 0)
            progress_bar = make_progress_bar(progress, 5)
        else:
            progress_bar = "[dim]-----[/dim]"

        end_time = instance.get("stopped_at") if status == "stopped" else None
        duration = format_duration_colored(instance.get("registered_at", ""), end_time)

        table.add_row(selector, status_icon, name, progress_bar, duration)

    if not instances:
        table.add_row(" ", "o", "[dim]None[/dim]", "-----", "-")

    return table


def create_compact_instances_table(instances: list, selected_idx: int) -> Table:
    """Create a compact instances table without Task column (for compact mode)."""
    max_name_len = 25
    for inst in instances:
        name = format_instance_name(inst, max_len=40)
        max_name_len = max(max_name_len, len(name) + 2)

    table = Table(
        show_header=True,
        header_style="bold cyan",
        border_style="blue",
        expand=True
    )

    table.add_column("", width=2, justify="center")
    table.add_column("●", style="dim", width=1, justify="center")
    table.add_column("Name", style="white")  # Dynamic width - fills available space
    table.add_column("Device", style="yellow", width=10)
    table.add_column("Progress", width=14)
    table.add_column("Time", width=6, justify="right")

    for i, instance in enumerate(instances):
        selector = "[yellow]>[/yellow]" if i == selected_idx else " "
        name = format_instance_name(instance, max_len=35)
        if i == selected_idx:
            name = f"[bold yellow]{name}[/bold yellow]"

        device = instance.get("device_id", "?")
        instance_id = instance.get("id", "")
        status = instance.get("status", "idle")
        # Poll for fresh todos when processing, otherwise use cached data
        if status == "processing":
            todos = get_instance_todos(instance_id, use_cache=False)
        else:
            todos = get_instance_todos(instance_id, use_cache=True)

        has_active_subtask = todos.get("current_task") is not None

        if status == "stopped":
            status_icon = "[dim]o[/dim]"
        elif status == "processing" or has_active_subtask:
            status_icon = "[green]>[/green]"
        else:
            status_icon = "[cyan]*[/cyan]"

        if todos.get("total", 0) > 0:
            progress = todos.get("progress", 0)
            progress_bar = make_progress_bar(progress, 8)
            progress_text = f"{progress_bar} {progress}%"
        else:
            progress_text = "[dim]-[/dim]"

        end_time = instance.get("stopped_at") if status == "stopped" else None
        duration = format_duration_colored(instance.get("registered_at", ""), end_time)

        table.add_row(selector, status_icon, name, device, progress_text, duration)

    if not instances:
        table.add_row(" ", "[dim]-[/dim]", "[dim]No instances[/dim]", "-", "-", "-")

    return table


def create_events_panel(events: list) -> Panel:
    """Create the events panel."""
    lines = []

    EVENT_STYLES = {
        "instance_registered": ("green", "+", "registered"),
        "instance_stopped": ("red", "-", "stopped"),
        "instance_killed": ("red", "x", "killed"),
        "instance_unstick": ("cyan", "!", "nudged"),
        "instance_renamed": ("yellow", "~", "renamed"),
        "tts_queued": ("yellow", "o", "queued TTS"),
        "tts_playing": ("cyan", ">", "speaking"),
        "tts_completed": ("blue", "v", "TTS done"),
        "notification_sent": ("magenta", "*", "notified"),
        "sound_played": ("yellow", "~", "sound"),
    }

    for event in events:
        try:
            created = event.get("created_at", "")
            if created:
                time_str = created.split(" ")[1][:5] if " " in created else created[:5]
            else:
                time_str = "??:??"

            event_type = event.get("event_type", "unknown")
            details = event.get("details", {}) if isinstance(event.get("details"), dict) else {}

            # Get human-readable name using the helper function
            display_name = format_event_instance_name(event, max_len=18)
            color, icon, action = EVENT_STYLES.get(event_type, ("dim", ".", event_type))

            if event_type == "instance_registered":
                msg = f"[{color}]{icon}[/{color}] [bold]{display_name}[/bold]: [green]registered[/green]"
            elif event_type == "instance_stopped":
                msg = f"[{color}]{icon}[/{color}] [bold]{display_name}[/bold]: [red]stopped[/red]"
            elif event_type == "instance_renamed":
                old_name = details.get("old_name", "?")
                new_name = details.get("new_name", "?")
                msg = f"[{color}]{icon}[/{color}] [bold]{old_name}[/bold] -> [bold]{new_name}[/bold]"
            elif event_type in ("tts_queued", "tts_playing", "tts_completed"):
                voice = details.get("voice", "").replace("Microsoft ", "").replace(" Desktop", "")
                msg = f"[{color}]{icon}[/{color}] [bold]{display_name}[/bold]: [{color}]{action}[/{color}]"
                if voice and event_type == "tts_playing":
                    msg += f" [dim]({voice})[/dim]"
            else:
                msg = f"[{color}]{icon}[/{color}] [bold]{display_name}[/bold]: [{color}]{action}[/{color}]"

            lines.append(f"[dim]{time_str}[/dim]  {msg}")
        except Exception:
            continue

    if not lines:
        lines.append("[dim]No recent events[/dim]")

    content = "\n".join(lines[:6])
    return Panel(content, title="Recent Events", border_style="blue")


def create_mobile_events_panel(events: list) -> Panel:
    """Create a compact events panel for mobile."""
    lines = []

    EVENT_ICONS = {
        "instance_registered": "[green]+[/green]",
        "instance_stopped": "[red]-[/red]",
        "instance_killed": "[red]x[/red]",
        "instance_unstick": "[cyan]![/cyan]",
        "instance_renamed": "[yellow]~[/yellow]",
        "tts_playing": "[cyan]>[/cyan]",
        "notification_sent": "[magenta]*[/magenta]",
    }

    for event in events[:4]:
        try:
            created = event.get("created_at", "")
            time_str = created.split(" ")[1][:5] if " " in created else "??:??"

            event_type = event.get("event_type", "unknown")
            icon = EVENT_ICONS.get(event_type, "[dim].[/dim]")

            # Get human-readable name using the helper function
            display_name = format_event_instance_name(event, max_len=12)

            lines.append(f"[dim]{time_str}[/dim] {icon} {display_name}")
        except Exception:
            continue

    if not lines:
        lines.append("[dim]No events[/dim]")

    return Panel("\n".join(lines), title="Events", border_style="blue", padding=(0, 1))


def create_tts_queue_panel(queue_status: dict) -> Panel:
    """Create a compact one-row TTS queue panel showing instance names in order."""
    current = queue_status.get("current")
    queue = queue_status.get("queue", [])

    # Build compact queue string
    queue_items = []

    if current:
        current_name = current.get('tab_name', '?')
        if len(current_name) > 12:
            current_name = current_name[:10] + ".."
        queue_items.append(f"[yellow]{current_name}[/yellow]")

    for item in queue[:5]:  # Show max 5 queued items
        name = item.get('tab_name', '?')
        if len(name) > 12:
            name = name[:10] + ".."
        queue_items.append(name)

    if len(queue) > 5:
        queue_items.append(f"[dim]+{len(queue) - 5} more[/dim]")

    if queue_items:
        content = "Queue: " + " → ".join(queue_items)
    else:
        content = "[dim]Queue: (empty)[/dim]"

    return Panel(content, title="TTS Queue", border_style="yellow")


def create_server_logs_panel(max_lines: int = 8) -> Panel:
    """Create a panel showing recent server logs fetched from API."""
    json_highlighter = JSONHighlighter()

    try:
        req = urllib.request.Request(f"{API_URL}/api/logs/recent?limit={max_lines}")
        with urllib.request.urlopen(req, timeout=2) as response:
            data = json.loads(response.read().decode())
            logs = data.get("logs", [])

            if not logs:
                content = Text("No server logs available", style="dim")
            else:
                # Format logs with timestamp and level colors
                # Build a Text object to support JSON highlighting
                content = Text()
                level_colors = {
                    "INFO": "green",
                    "WARN": "yellow",
                    "ERRO": "red",
                    "DEBU": "dim",
                    "CRIT": "red bold"
                }

                for i, log in enumerate(logs):
                    if i > 0:
                        content.append("\n")

                    timestamp = log.get("timestamp", "??:??:??")
                    level = log.get("level", "INFO")[:4]
                    message = log.get("message", "")
                    level_color = level_colors.get(level, "white")

                    # Add timestamp and level prefix
                    content.append(f"{timestamp} ", style="dim")
                    content.append(f"{level} ", style=level_color)

                    # Apply JSON highlighting to message if it might contain JSON
                    if '{' in message or '[' in message:
                        message_text = json_highlighter(Text(message))
                        content.append_text(message_text)
                    else:
                        content.append(message)

    except Exception:
        content = Text("Server logs unavailable", style="dim")

    return Panel(content, title="Server Logs", border_style="blue")


def create_deploy_logs_panel(max_lines: int = 8) -> Panel:
    """Create a panel showing deploy logs from .claude-deploy.log."""
    is_active, log_path, metadata = check_deploy_status()

    if not is_active or not log_path or not log_path.exists():
        content = Text("No deployment in progress", style="dim")
        return Panel(content, title="Deploy", border_style="blue")

    # Build title from metadata
    env = metadata.get("environment", "?")
    repo = metadata.get("repo", "")
    status_label = "RUNNING" if is_active else "COMPLETED"
    title_parts = ["Deploy"]
    if env:
        title_parts.append(f"[yellow]{env}[/yellow]")
    if repo:
        title_parts.append(f"[dim]{repo}[/dim]")
    title_parts.append(f"[bold green]{status_label}[/bold green]")
    title = " | ".join(title_parts)

    try:
        raw_lines = log_path.read_text().splitlines()
        # Tail: take the last N lines
        tail_lines = raw_lines[-max_lines:] if len(raw_lines) > max_lines else raw_lines

        if not tail_lines:
            content = Text("Deploy log is empty", style="dim")
            return Panel(content, title=title, border_style="yellow")

        content = Text()
        for i, raw_line in enumerate(tail_lines):
            if i > 0:
                content.append("\n")
            line = strip_ansi(raw_line)

            # Color lines based on content
            lower = line.lower()
            if "error" in lower or "fail" in lower or "fatal" in lower:
                content.append(line, style="red")
            elif "success" in lower or "deployed" in lower or "complete" in lower:
                content.append(line, style="green")
            elif "build" in lower or "step" in lower:
                content.append(line, style="cyan")
            elif "warn" in lower:
                content.append(line, style="yellow")
            else:
                content.append(line)

    except Exception:
        content = Text("Could not read deploy log", style="dim red")

    return Panel(content, title=title, border_style="yellow")


def create_instance_details_panel(instance: dict, todos_data: dict, compact: bool = False) -> Panel:
    """Create a panel showing details for the selected instance.

    If compact=True, shows a single-line summary suitable for bottom of vertical layout.
    """
    lines = []

    if not instance:
        return Panel("[dim]No instance selected[/dim]", title="Instance Details", border_style="magenta")

    name = format_instance_name(instance, max_len=25)
    status = instance.get("status", "unknown")
    device = instance.get("device_id", "?")
    if status == "stopped":
        status_icon = "[dim]o[/dim]"
    elif status == "processing":
        status_icon = "[green]>[/green]"
    else:
        status_icon = "[cyan]*[/cyan]"

    # Get TTS voice profile info
    tts_voice = instance.get("tts_voice", "")
    # Clean up voice name: "Microsoft David Desktop" -> "David"
    if tts_voice:
        voice_short = tts_voice.replace("Microsoft ", "").replace(" Desktop", "")
    else:
        voice_short = "?"

    profile_name = instance.get("profile_name", "")
    # Extract profile number: "profile_1" -> "1"
    profile_num = profile_name.replace("profile_", "") if profile_name else "?"

    working_dir = instance.get("working_dir", "")
    if working_dir:
        # Shorten home prefix for display
        working_dir_short = working_dir.replace(str(Path.home()), "~")
    else:
        working_dir_short = "?"

    if compact:
        # Compact single-line format for vertical layout bottom
        todos = todos_data.get("todos", [])
        total = todos_data.get("total", 0)
        progress = todos_data.get("progress", 0)
        current_task = todos_data.get("current_task", "")

        # Build compact line: status icon, name, device, voice, dir, progress, current task
        parts = [f"{status_icon} [bold]{name}[/bold]"]
        parts.append(f"[dim]({device})[/dim]")
        parts.append(f"[cyan]Voice:[/cyan] {voice_short}")
        parts.append(f"[dim]{working_dir_short}[/dim]")

        if total > 0:
            parts.append(f"[yellow]{progress}%[/yellow]")

        if current_task:
            if len(current_task) > 30:
                current_task = current_task[:27] + "..."
            parts.append(f"[italic]{current_task}[/italic]")

        content = "  ".join(parts)
        return Panel(content, title="Instance Details", border_style="magenta")

    lines.append(f"{status_icon} [bold]{name}[/bold]  [dim]({device})[/dim]")
    lines.append(f"[cyan]Voice:[/cyan] {voice_short}  [dim](profile {profile_num})[/dim]")
    lines.append(f"[cyan]Dir:[/cyan]   [dim]{working_dir_short}[/dim]")
    lines.append("")

    todos = todos_data.get("todos", [])
    completed = todos_data.get("completed", 0)
    total = todos_data.get("total", 0)
    progress = todos_data.get("progress", 0)

    if total > 0:
        progress_bar = make_progress_bar(progress, 15)
        lines.append(f"Progress: {progress_bar} {completed}/{total}")
        lines.append("")

        lines.append("[bold cyan]Subtasks:[/bold cyan]")
        for todo in todos:
            status_char = todo.get("status", "pending")
            content = todo.get("content", "")

            if len(content) > 45:
                content = content[:42] + "..."

            if status_char == "completed":
                lines.append(f"  [green]v[/green] [dim]{content}[/dim]")
            elif status_char == "in_progress":
                lines.append(f"  [yellow]>[/yellow] [bold]{content}[/bold]")
            else:
                lines.append(f"  [dim]o[/dim] {content}")
    else:
        lines.append("[dim]No active tasks[/dim]")

    content = "\n".join(lines)
    return Panel(content, title="Instance Details", border_style="magenta")


def create_mobile_instance_details_panel(instance: dict, todos_data: dict) -> Panel:
    """Create a compact panel showing the active subtask for mobile."""
    if not instance:
        return Panel("[dim]No selection[/dim]", title="Details", border_style="magenta", padding=(0, 1))

    lines = []

    name = format_instance_name(instance, max_len=15)
    status = instance.get("status", "unknown")
    if status == "stopped":
        status_icon = "[dim]o[/dim]"
    elif status == "processing":
        status_icon = "[green]>[/green]"
    else:
        status_icon = "[cyan]*[/cyan]"

    # Get TTS voice profile info
    tts_voice = instance.get("tts_voice", "")
    voice_short = tts_voice.replace("Microsoft ", "").replace(" Desktop", "") if tts_voice else "?"

    lines.append(f"{status_icon} [bold]{name}[/bold]  [dim]{voice_short}[/dim]")

    current_task = todos_data.get("current_task")
    progress = todos_data.get("progress", 0)
    total = todos_data.get("total", 0)

    if current_task:
        if len(current_task) > 35:
            current_task = current_task[:32] + "..."
        lines.append(f"[yellow]>[/yellow] {current_task}")
        if total > 0:
            lines.append(f"[dim]{progress}% ({todos_data.get('completed', 0)}/{total})[/dim]")
    elif total > 0:
        lines.append(f"[dim]{progress}% complete[/dim]")
    else:
        lines.append("[dim]No active task[/dim]")

    content = "\n".join(lines)
    return Panel(content, title="Details", border_style="magenta", padding=(0, 1))


def create_server_status_panel() -> Panel:
    """Create a panel showing server status."""
    if api_healthy:
        content = "[green]* Server running[/green] on port 7777"
        border = "green"
    else:
        content = f"[red]! Server error[/red]\n[dim]{api_error_message or 'Unknown error'}[/dim]"
        border = "red"

    return Panel(content, title="API Server", border_style=border)


def create_info_panel(max_lines: int = 8) -> Panel:
    """Create the info panel - events, server logs, or deploy logs based on panel_page."""
    if panel_page == 0:
        events = get_recent_events(max_lines)
        return create_events_panel(events)
    elif panel_page == 1:
        return create_server_logs_panel(max_lines=max_lines)
    else:
        return create_deploy_logs_panel(max_lines=max_lines)


def create_mobile_info_panel(max_lines: int = 4) -> Panel:
    """Create a compact info panel for mobile - events, server logs, or deploy logs based on panel_page."""
    if panel_page == 0:
        events = get_recent_events(max_lines)
        return create_mobile_events_panel(events)
    elif panel_page == 1:
        return create_server_logs_panel(max_lines=max_lines)
    else:
        return create_deploy_logs_panel(max_lines=max_lines)


def create_status_bar(instances: list, selected_idx: int) -> Text:
    """Create the status bar."""
    global unstick_feedback, resume_feedback, restart_feedback

    active_count = sum(1 for i in instances if i.get("status") in ("processing", "idle"))
    total_count = len(instances)

    # Mode indicator with color
    mode_colors = {"mobile": "yellow", "vertical": "magenta", "compact": "blue", "full": "cyan"}
    mode_color = mode_colors.get(layout_mode, "white")

    # Page indicator
    page_names = ["Events", "Logs", "Deploy"]
    page_name = page_names[panel_page] if panel_page < len(page_names) else "?"

    # Filter indicator
    filter_indicator = ""
    if filter_mode != "all":
        filter_indicator = f"  [magenta]F:{filter_mode}[/magenta]"

    text = Text()
    text.append(f"Instances: {active_count}/{total_count}  |  ", style="white")
    text.append_text(Text.from_markup(f"[{mode_color}]{layout_mode}[/{mode_color}]"))
    text.append(f"  |  {selected_idx + 1}/{total_count}  |  ", style="white")
    text.append_text(Text.from_markup(f"[cyan]{page_name}[/cyan] [dim](h/l)[/dim]"))
    if filter_indicator:
        text.append_text(Text.from_markup(filter_indicator))
    text.append("  |  ", style="white")

    # Check for feedback messages (show for 3 seconds)
    feedback_msg = None
    if restart_feedback:
        fb_time, fb_text = restart_feedback
        if time.time() - fb_time < 3.0:
            feedback_msg = fb_text
        else:
            restart_feedback = None
    if not feedback_msg and unstick_feedback:
        fb_time, fb_text = unstick_feedback
        if time.time() - fb_time < 3.0:
            feedback_msg = fb_text
        else:
            unstick_feedback = None
    if not feedback_msg and resume_feedback:
        fb_time, fb_text = resume_feedback
        if time.time() - fb_time < 3.0:
            feedback_msg = fb_text
        else:
            resume_feedback = None

    if feedback_msg:
        # Use green for success messages, yellow for warnings
        if "Copied" in feedback_msg or "Skipped" in feedback_msg or "Restarted" in feedback_msg:
            text.append_text(Text.from_markup(f"[green bold]✓ {feedback_msg}[/green bold]"))
        else:
            text.append_text(Text.from_markup(f"[yellow bold]{feedback_msg}[/yellow bold]"))
    else:
        text.append_text(Text.from_markup("[dim]jk=nav r=rename s=stop d=del y=copy f=filter R=restart q=quit[/dim]"))

    return text


def create_mobile_status_bar(instances: list, selected_idx: int) -> Text:
    """Create a compact status bar for mobile."""
    active_count = sum(1 for i in instances if i.get("status") in ("processing", "idle"))
    total_count = len(instances)

    # Page indicator
    page_indicators = {0: "E", 1: "L", 2: "D"}
    page_indicator = page_indicators.get(panel_page, "?")

    text = Text()
    text.append(f"{active_count}/{total_count} ", style="white")
    if active_count > 0:
        text.append("*", style="green")
    else:
        text.append("o", style="dim")
    text.append(f"  sel:{selected_idx + 1}", style="dim")
    text.append(f"  [{page_indicator}]", style="cyan")

    return text


def generate_mobile_dashboard(instances: list, selected_idx: int) -> Layout:
    """Generate a compact dashboard layout for mobile."""
    global api_healthy, api_error_message

    selected_instance = None
    selected_todos = {"progress": 0, "current_task": None, "total": 0, "todos": []}
    if instances and 0 <= selected_idx < len(instances):
        selected_instance = instances[selected_idx]
        instance_id = selected_instance.get("id", "")
        # Poll for fresh todos when processing, otherwise use cached data
        if selected_instance.get("status") == "processing":
            selected_todos = get_instance_todos(instance_id, use_cache=False)
        else:
            selected_todos = get_instance_todos(instance_id, use_cache=True)

    layout = Layout()

    if not api_healthy:
        layout.split_column(
            Layout(name="error", size=2),
            Layout(name="instances"),
            Layout(name="details", size=5),
            Layout(name="info_panel", size=5),
            Layout(name="footer", size=1)
        )
        error_text = Text()
        error_text.append("! API down", style="bold red")
        layout["error"].update(Panel(error_text, border_style="red"))
    else:
        layout.split_column(
            Layout(name="instances"),
            Layout(name="details", size=5),
            Layout(name="info_panel", size=5),
            Layout(name="footer", size=1)
        )

    layout["instances"].update(create_mobile_instances_table(instances, selected_idx))
    layout["details"].update(create_mobile_instance_details_panel(selected_instance, selected_todos))
    layout["info_panel"].update(create_mobile_info_panel(max_lines=3))
    layout["footer"].update(create_mobile_status_bar(instances, selected_idx))

    return layout


def generate_compact_dashboard(instances: list, selected_idx: int) -> Layout:
    """Generate compact dashboard without sidebar (for medium-width terminals)."""
    global api_healthy, api_error_message

    selected_instance = None
    selected_todos = {"progress": 0, "current_task": None, "total": 0, "todos": []}
    if instances and 0 <= selected_idx < len(instances):
        selected_instance = instances[selected_idx]
        instance_id = selected_instance.get("id", "")
        # Poll for fresh todos when processing, otherwise use cached data
        if selected_instance.get("status") == "processing":
            selected_todos = get_instance_todos(instance_id, use_cache=False)
        else:
            selected_todos = get_instance_todos(instance_id, use_cache=True)

    layout = Layout()

    # Compact header + main content + footer
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="instances"),
        Layout(name="info_panel", size=4),
        Layout(name="footer", size=1)
    )

    # Compact header: title + server status
    header_layout = Layout()
    header_layout.split_row(
        Layout(name="title", ratio=2),
        Layout(name="server_status", ratio=1)
    )

    if api_healthy:
        server_text = "[green]●[/green] Server OK"
    else:
        server_text = f"[red]●[/red] {api_error_message or 'Error'}"

    timer_text = get_timer_header_text()
    timer_text.justify = "center"
    header_layout["title"].update(Panel(
        timer_text,
        border_style="cyan"
    ))
    header_layout["server_status"].update(Panel(
        Text.from_markup(server_text, justify="center"),
        border_style="green" if api_healthy else "red"
    ))
    layout["header"].update(header_layout)

    layout["instances"].update(create_compact_instances_table(instances, selected_idx))
    layout["info_panel"].update(create_info_panel(max_lines=3))
    layout["footer"].update(create_status_bar(instances, selected_idx))

    return layout


def generate_vertical_dashboard(instances: list, selected_idx: int) -> Layout:
    """Generate vertical dashboard with stacked panels (for vertical monitors).

    Layout (top to bottom):
    - Header (timer + server status)
    - Instance table (sized to fit content, primary element)
    - Recent events (fills remaining space)
    - Instance details (compact, bottom-aligned)
    - Footer (status bar)
    """
    global api_healthy, api_error_message

    selected_instance = None
    selected_todos = {"progress": 0, "current_task": None, "total": 0, "todos": []}
    if instances and 0 <= selected_idx < len(instances):
        selected_instance = instances[selected_idx]
        instance_id = selected_instance.get("id", "")
        # Poll for fresh todos when processing, otherwise use cached data
        if selected_instance.get("status") == "processing":
            selected_todos = get_instance_todos(instance_id, use_cache=False)
        else:
            selected_todos = get_instance_todos(instance_id, use_cache=True)

    # Calculate adaptive sizes based on terminal height and content
    height = console.size.height

    # Fixed elements
    header_size = 3
    footer_size = 1
    details_size = 3  # Compact instance details at bottom (single line + borders)

    # Instance table: sized to fit content (primary element)
    num_instances = max(len(instances), 1)
    # Table needs: title + header + separator + N data rows + borders = N + 6
    table_ideal = num_instances + 6
    # Reasonable bounds - table is primary, but don't let it dominate
    table_min = 6   # Minimum to show a couple rows
    table_max = 20  # Allow more room for table as primary element

    # Calculate instance table size
    instance_size = max(table_min, min(table_ideal, table_max))

    # Events panel gets all remaining space
    # Available = total - header - footer - details - table
    events_size = height - header_size - footer_size - details_size - instance_size
    events_min = 6  # Minimum readable events panel
    events_size = max(events_min, events_size)

    layout = Layout()

    # Vertical layout: Table → Events → Details (bottom)
    layout.split_column(
        Layout(name="header", size=header_size),
        Layout(name="instances", size=instance_size),
        Layout(name="info_panel"),  # Events - takes remaining space (no size = flex)
        Layout(name="details", size=details_size),
        Layout(name="footer", size=footer_size)
    )

    # Compact header: title + server status side by side
    header_layout = Layout()
    header_layout.split_row(
        Layout(name="title", ratio=2),
        Layout(name="server_status", ratio=1)
    )

    if api_healthy:
        server_text = "[green]●[/green] Server OK"
    else:
        server_text = f"[red]●[/red] {api_error_message or 'Error'}"

    timer_text = get_timer_header_text()
    timer_text.justify = "center"
    header_layout["title"].update(Panel(
        timer_text,
        border_style="cyan"
    ))
    header_layout["server_status"].update(Panel(
        Text.from_markup(server_text, justify="center"),
        border_style="green" if api_healthy else "red"
    ))
    layout["header"].update(header_layout)

    # Calculate how many lines fit in the info panel (panel has 2 border lines)
    info_lines = max(1, events_size - 2)

    layout["instances"].update(create_compact_instances_table(instances, selected_idx))
    layout["info_panel"].update(create_info_panel(max_lines=info_lines))
    layout["details"].update(create_instance_details_panel(selected_instance, selected_todos, compact=True))
    layout["footer"].update(create_status_bar(instances, selected_idx))

    return layout


def generate_dashboard(instances: list, selected_idx: int) -> Layout:
    """Generate the full dashboard layout."""
    global api_healthy, api_error_message

    tts_queue = get_tts_queue_status()

    selected_instance = None
    selected_todos = {"progress": 0, "current_task": None, "total": 0, "todos": []}
    if instances and 0 <= selected_idx < len(instances):
        selected_instance = instances[selected_idx]
        instance_id = selected_instance.get("id", "")
        # Poll for fresh todos when processing, otherwise use cached data
        if selected_instance.get("status") == "processing":
            selected_todos = get_instance_todos(instance_id, use_cache=False)
        else:
            selected_todos = get_instance_todos(instance_id, use_cache=True)

    layout = Layout()

    # Include server status in header area
    layout.split_column(
        Layout(name="header", size=5),
        Layout(name="main"),
        Layout(name="footer", size=1)
    )

    # Header with server status
    header_layout = Layout()
    header_layout.split_row(
        Layout(name="title", ratio=2),
        Layout(name="server_status", ratio=1)
    )
    timer_text = get_timer_header_text()
    timer_text.justify = "center"
    header_layout["title"].update(Panel(
        timer_text,
        border_style="cyan"
    ))
    header_layout["server_status"].update(create_server_status_panel())
    layout["header"].update(header_layout)

    # Main content
    layout["main"].split_row(
        Layout(name="left_column", ratio=3),
        Layout(name="sidebar", ratio=2)
    )

    # Left column: instances table + details section (instance_details + tts_queue)
    layout["left_column"].split_column(
        Layout(name="instances", ratio=3),
        Layout(name="details_section", ratio=1)
    )

    # Details section: instance details (3/4) + TTS queue (1/4)
    layout["details_section"].split_column(
        Layout(name="instance_details", ratio=3),
        Layout(name="tts_queue", ratio=1)
    )

    # Sidebar shows events or server logs based on panel_page
    layout["sidebar"].update(create_info_panel(max_lines=20))

    layout["instances"].update(create_instances_table(instances, selected_idx))
    layout["instance_details"].update(create_instance_details_panel(selected_instance, selected_todos))
    layout["tts_queue"].update(create_tts_queue_panel(tts_queue))

    layout["footer"].update(create_status_bar(instances, selected_idx))

    return layout


def get_dashboard(instances: list, selected_idx: int) -> Layout:
    """Get appropriate dashboard based on layout_mode (dynamic if not forced)."""
    global layout_mode
    # Dynamically detect layout mode on each render if not forced by CLI
    if not layout_mode_forced:
        layout_mode = detect_layout_mode()

    if layout_mode == "mobile":
        return generate_mobile_dashboard(instances, selected_idx)
    if layout_mode == "vertical":
        return generate_vertical_dashboard(instances, selected_idx)
    if layout_mode == "compact":
        return generate_compact_dashboard(instances, selected_idx)
    return generate_dashboard(instances, selected_idx)


def main():
    """Main entry point."""
    global selected_index, instances_cache, api_healthy, api_error_message, layout_mode, layout_mode_forced, sort_mode, filter_mode, panel_page
    global deploy_active, deploy_log_path, deploy_metadata, deploy_previous_page, deploy_auto_switched

    parser = argparse.ArgumentParser(description="Token-API TUI Dashboard")
    parser.add_argument("--mobile", "-m", action="store_true",
                        help="Force mobile-friendly layout")
    parser.add_argument("--vertical", "-v", action="store_true",
                        help="Force vertical layout (stacked panels)")
    parser.add_argument("--compact", action="store_true",
                        help="Force compact layout (no sidebar)")
    parser.add_argument("--no-mobile", action="store_true",
                        help="Force full desktop layout even on narrow terminals")
    args = parser.parse_args()

    if args.mobile:
        layout_mode = "mobile"
        layout_mode_forced = True
    elif args.vertical:
        layout_mode = "vertical"
        layout_mode_forced = True
    elif args.compact:
        layout_mode = "compact"
        layout_mode_forced = True
    elif args.no_mobile:
        layout_mode = "full"
        layout_mode_forced = True
    else:
        layout_mode = detect_layout_mode()
        layout_mode_forced = False

    mode_colors = {"mobile": "yellow", "vertical": "magenta", "compact": "blue", "full": "cyan"}
    mode_indicator = f"[{mode_colors.get(layout_mode, 'white')}]{layout_mode}[/{mode_colors.get(layout_mode, 'white')}]"

    console.print(f"[cyan]Starting Token-API TUI[/cyan] ({mode_indicator} mode)")

    # Health check
    api_healthy, api_error_message = check_api_health()
    if not api_healthy:
        console.print(f"[yellow]Warning:[/yellow] {api_error_message}")

    # Check database
    if not DB_PATH.exists():
        console.print(f"[red]Error:[/red] Database not found at {DB_PATH}")
        console.print("Run the Token-API server first to initialize the database.")
        sys.exit(1)

    console.print("[dim]Controls: jk=nav, gG=top/btm, h/l=page, Enter=open, r=rename, f=filter, s=stop, d=del, R=restart, q=quit[/dim]\n")

    quit_flag = threading.Event()
    input_mode = threading.Event()
    update_flag = threading.Event()
    action_queue = []
    action_lock = threading.Lock()

    # Store terminal settings at main scope for cleanup on Ctrl+C
    import tty
    import termios
    original_terminal_settings = termios.tcgetattr(sys.stdin)

    def key_listener():
        """Listen for keypresses."""
        import select as sel

        try:
            tty.setcbreak(sys.stdin.fileno())
            while not quit_flag.is_set():
                if input_mode.is_set():
                    time.sleep(0.05)
                    continue

                if sel.select([sys.stdin], [], [], 0.02)[0]:
                    if input_mode.is_set():
                        continue

                    key = sys.stdin.read(1)

                    if key.lower() == 'q':
                        quit_flag.set()
                        break
                    elif key == '\x1b':
                        if sel.select([sys.stdin], [], [], 0.05)[0]:
                            seq = sys.stdin.read(2)
                            with action_lock:
                                if seq == '[A':
                                    action_queue.append('up')
                                elif seq == '[B':
                                    action_queue.append('down')
                            update_flag.set()
                    elif key == '\x12':  # Ctrl+R: full refresh (restart server + re-exec TUI)
                        with action_lock:
                            action_queue.append('full_refresh')
                        update_flag.set()
                    elif key == 'r':
                        with action_lock:
                            action_queue.append('rename')
                        update_flag.set()
                    elif key.lower() == 'd':
                        with action_lock:
                            action_queue.append('delete')
                        update_flag.set()
                    elif key.lower() == 'c':
                        with action_lock:
                            action_queue.append('delete_all')
                        update_flag.set()
                    elif key.lower() == 's':
                        with action_lock:
                            action_queue.append('stop')
                        update_flag.set()
                    elif key.lower() == 'o':
                        with action_lock:
                            action_queue.append('sort')
                        update_flag.set()
                    elif key == 'j':
                        with action_lock:
                            action_queue.append('down')
                        update_flag.set()
                    elif key == 'k':
                        with action_lock:
                            action_queue.append('up')
                        update_flag.set()
                    elif key == 'h':
                        with action_lock:
                            action_queue.append('page_prev')
                        update_flag.set()
                    elif key == 'l':
                        with action_lock:
                            action_queue.append('page_next')
                        update_flag.set()
                    elif key == 'y':
                        with action_lock:
                            action_queue.append('resume')
                        update_flag.set()
                    elif key == 'v':
                        with action_lock:
                            action_queue.append('voice')
                        update_flag.set()
                    elif key == 'U':
                        with action_lock:
                            action_queue.append('unstick')
                        update_flag.set()
                    elif key == 'I':
                        with action_lock:
                            action_queue.append('unstick2')
                        update_flag.set()
                    elif key == 'K':
                        with action_lock:
                            action_queue.append('kill')
                        update_flag.set()
                    elif key == 'f':
                        with action_lock:
                            action_queue.append('filter')
                        update_flag.set()
                    elif key == 'R':
                        with action_lock:
                            action_queue.append('restart')
                        update_flag.set()
                    elif key == '\r' or key == '\n':
                        with action_lock:
                            action_queue.append('open_terminal')
                        update_flag.set()
                    elif key == 'g':
                        with action_lock:
                            action_queue.append('go_top')
                        update_flag.set()
                    elif key == 'G':
                        with action_lock:
                            action_queue.append('go_bottom')
                        update_flag.set()
        except Exception:
            pass
        finally:
            try:
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, original_terminal_settings)
            except:
                pass

    listener_thread = threading.Thread(target=key_listener, daemon=True)
    listener_thread.start()

    instances_cache = get_instances()
    prev_instance_ids = set(i.get("id") for i in instances_cache)

    def _get_displayed():
        """Get filtered instances for display."""
        return filter_instances(instances_cache)

    def _refresh(live_ref):
        """Refresh dashboard with filtered instances."""
        displayed = _get_displayed()
        live_ref.update(get_dashboard(displayed, selected_index))
        live_ref.refresh()

    def _clamp_selection():
        """Clamp selected_index to filtered list bounds."""
        global selected_index
        displayed = _get_displayed()
        if displayed:
            selected_index = min(selected_index, len(displayed) - 1)
        else:
            selected_index = 0

    try:
        with Live(get_dashboard(_get_displayed(), selected_index), console=console, refresh_per_second=10, screen=True) as live:
            last_refresh = time.time()

            while not quit_flag.is_set():
                actions_to_process = []
                with action_lock:
                    if action_queue:
                        actions_to_process = action_queue.copy()
                        action_queue.clear()

                displayed = _get_displayed()

                for action in actions_to_process:
                    if action == 'up' and displayed:
                        selected_index = max(0, selected_index - 1)
                        _refresh(live)

                    elif action == 'down' and displayed:
                        selected_index = min(len(displayed) - 1, selected_index + 1)
                        _refresh(live)

                    elif action == 'go_top' and displayed:
                        selected_index = 0
                        _refresh(live)

                    elif action == 'go_bottom' and displayed:
                        selected_index = len(displayed) - 1
                        _refresh(live)

                    if action == 'rename' and displayed:
                        if 0 <= selected_index < len(displayed):
                            instance = displayed[selected_index]
                            instance_id = instance.get("id")
                            current_name = format_instance_name(instance)

                            input_mode.set()
                            time.sleep(0.1)
                            live.stop()

                            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, original_terminal_settings)

                            console.print(f"\n[yellow]Rename instance:[/yellow] {current_name}")
                            try:
                                new_name = Prompt.ask("New name", default=current_name)
                                if new_name and new_name != current_name:
                                    if rename_instance(instance_id, new_name):
                                        console.print(f"[green]v[/green] Renamed to: {new_name}")
                                    else:
                                        console.print("[red]x[/red] Rename failed")
                                else:
                                    console.print("[dim]Cancelled[/dim]")
                            except (KeyboardInterrupt, EOFError):
                                console.print("[dim]Cancelled[/dim]")

                            time.sleep(0.3)
                            tty.setcbreak(sys.stdin.fileno())
                            input_mode.clear()
                            instances_cache = get_instances()
                            _clamp_selection()
                            live.start()
                            _refresh(live)

                    elif action == 'delete' and displayed:
                        if 0 <= selected_index < len(displayed):
                            instance = displayed[selected_index]
                            instance_id = instance.get("id")
                            instance_name = format_instance_name(instance)

                            input_mode.set()
                            time.sleep(0.1)
                            live.stop()

                            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, original_terminal_settings)

                            console.print(f"\n[red]Delete instance:[/red] {instance_name}")
                            try:
                                confirm = Prompt.ask("Type 'yes' to confirm delete", default="no")
                                if confirm.lower() == 'yes':
                                    if delete_instance(instance_id):
                                        console.print(f"[green]v[/green] Deleted: {instance_name}")
                                    else:
                                        console.print("[red]x[/red] Delete failed")
                                else:
                                    console.print("[dim]Cancelled[/dim]")
                            except (KeyboardInterrupt, EOFError):
                                console.print("[dim]Cancelled[/dim]")

                            time.sleep(0.3)
                            tty.setcbreak(sys.stdin.fileno())
                            input_mode.clear()
                            instances_cache = get_instances()
                            _clamp_selection()
                            live.start()
                            _refresh(live)

                    elif action == 'voice' and displayed:
                        if 0 <= selected_index < len(displayed):
                            instance = displayed[selected_index]
                            instance_id = instance.get("id")
                            instance_name = format_instance_name(instance)
                            current_voice = instance.get("tts_voice", "")

                            input_mode.set()
                            time.sleep(0.1)
                            live.stop()

                            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, original_terminal_settings)

                            voices = get_available_voices()
                            if not voices:
                                console.print("[red]Could not fetch voices from API[/red]")
                            else:
                                console.print(f"\n[cyan]Change voice for:[/cyan] {instance_name}")
                                console.print(f"[dim]Current: {current_voice}[/dim]\n")

                                # Display numbered list
                                for i, v in enumerate(voices, 1):
                                    marker = "[green]*[/green]" if v["voice"] == current_voice else " "
                                    console.print(f"  {marker} {i}. {v['short_name']}")

                                console.print()
                                try:
                                    choice = Prompt.ask("Select voice number", default="")
                                    if choice.isdigit():
                                        idx = int(choice) - 1
                                        if 0 <= idx < len(voices):
                                            new_voice = voices[idx]["voice"]
                                            result = change_instance_voice(instance_id, new_voice)
                                            if result.get("success"):
                                                if result.get("status") == "no_change":
                                                    console.print("[dim]Already using that voice[/dim]")
                                                else:
                                                    changes = result.get("changes", [])
                                                    console.print(f"[green]v[/green] Voice changed to: {voices[idx]['short_name']}")
                                                    # Show bump chain if any
                                                    if len(changes) > 1:
                                                        console.print("[yellow]Bump chain:[/yellow]")
                                                        for c in changes:
                                                            old_short = c['old'].replace('Microsoft ', '') if c['old'] else '?'
                                                            new_short = c['new'].replace('Microsoft ', '')
                                                            console.print(f"  {c['name']}: {old_short} -> {new_short}")
                                            else:
                                                console.print("[red]x[/red] Voice change failed")
                                        else:
                                            console.print("[red]Invalid selection[/red]")
                                    else:
                                        console.print("[dim]Cancelled[/dim]")
                                except (KeyboardInterrupt, EOFError):
                                    console.print("[dim]Cancelled[/dim]")

                            time.sleep(0.3)
                            tty.setcbreak(sys.stdin.fileno())
                            input_mode.clear()
                            instances_cache = get_instances()
                            live.start()
                            _refresh(live)

                    elif action == 'delete_all':
                        total_count = len(instances_cache) if instances_cache else 0

                        if total_count == 0:
                            input_mode.set()
                            live.stop()
                            console.print("\n[dim]No instances to clear.[/dim]")
                            time.sleep(1)
                            tty.setcbreak(sys.stdin.fileno())
                            input_mode.clear()
                            live.start()
                            _refresh(live)
                            continue

                        input_mode.set()
                        time.sleep(0.1)
                        live.stop()

                        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, original_terminal_settings)

                        console.print(f"\n[red bold]Clear all {total_count} instance(s)?[/red bold]")
                        console.print("[dim]This will remove all instances from the database.[/dim]")
                        try:
                            confirm = Prompt.ask("Type 'yes' to confirm", default="no")
                            if confirm.lower() == 'yes':
                                success, count = delete_all_instances()
                                if success:
                                    console.print(f"[green]v[/green] Cleared {count} instance(s)")
                                    selected_index = 0
                                else:
                                    console.print("[red]x[/red] Clear all failed")
                            else:
                                console.print("[dim]Cancelled[/dim]")
                        except (KeyboardInterrupt, EOFError):
                            console.print("[dim]Cancelled[/dim]")

                        time.sleep(0.3)
                        tty.setcbreak(sys.stdin.fileno())
                        input_mode.clear()
                        instances_cache = get_instances()
                        _clamp_selection()
                        live.start()
                        _refresh(live)

                    elif action == 'stop' and displayed:
                        if 0 <= selected_index < len(displayed):
                            instance = displayed[selected_index]
                            instance_id = instance.get("id")

                            # Stop without confirmation (it's non-destructive)
                            if delete_instance(instance_id):
                                instances_cache = get_instances()
                                _clamp_selection()
                                _refresh(live)

                    elif action in ('unstick', 'unstick2') and displayed:
                        if 0 <= selected_index < len(displayed):
                            instance = displayed[selected_index]
                            instance_id = instance.get("id")
                            instance_name = format_instance_name(instance)
                            level = 2 if action == 'unstick2' else 1
                            level_desc = "Interrupting" if level == 2 else "Nudging"

                            # Non-destructive: no confirmation needed, run in background
                            global unstick_feedback
                            unstick_feedback = (time.time(), f"{level_desc} {instance_name}...")
                            _refresh(live)

                            def _do_unstick(iid, iname, lvl):
                                global unstick_feedback
                                result = unstick_instance(iid, level=lvl)
                                sig = result.get("signal", "?") if result else "?"
                                if result and result.get("status") == "nudged":
                                    unstick_feedback = (time.time(), f"{sig}: {iname} - activity detected")
                                elif result and result.get("status") == "no_change":
                                    unstick_feedback = (time.time(), f"{sig}: {iname} - no change")
                                elif result and result.get("detail"):
                                    unstick_feedback = (time.time(), f"Failed: {result['detail'][:30]}")
                                else:
                                    unstick_feedback = (time.time(), f"Unstick failed for {iname}")
                                update_flag.set()

                            threading.Thread(target=_do_unstick, args=(instance_id, instance_name, level), daemon=True).start()

                    elif action == 'kill' and displayed:
                        # Kill uses unstick level 3 (SIGKILL) - no confirmation needed
                        # since terminal is preserved and instance can be resumed
                        if 0 <= selected_index < len(displayed):
                            instance = displayed[selected_index]
                            instance_id = instance.get("id")
                            instance_name = format_instance_name(instance)
                            working_dir = instance.get("working_dir", "")

                            # Show immediate feedback, run in background
                            unstick_feedback = (time.time(), f"Killing {instance_name}...")
                            _refresh(live)

                            def _do_kill(iid, iname, wdir):
                                global unstick_feedback
                                result = unstick_instance(iid, level=3)
                                if result and result.get("status") in ("nudged", "no_change"):
                                    # SIGKILL always "works" - process is dead
                                    # Auto-copy resume command to clipboard
                                    if wdir:
                                        resume_cmd = f"cd {wdir} && claude --resume {iid}"
                                        copied, _ = copy_to_clipboard(resume_cmd)
                                        if copied:
                                            unstick_feedback = (time.time(), f"Killed {iname} - resume cmd copied!")
                                        else:
                                            unstick_feedback = (time.time(), f"Killed {iname} (use y to copy resume)")
                                    else:
                                        unstick_feedback = (time.time(), f"Killed {iname}")
                                elif result and result.get("detail"):
                                    unstick_feedback = (time.time(), f"Kill failed: {result['detail'][:30]}")
                                else:
                                    unstick_feedback = (time.time(), f"Kill failed for {iname}")
                                update_flag.set()

                            threading.Thread(target=_do_kill, args=(instance_id, instance_name, working_dir), daemon=True).start()

                    elif action == 'filter':
                        # Cycle filter: all -> active -> stopped -> all
                        filter_cycle = {"all": "active", "active": "stopped", "stopped": "all"}
                        filter_mode = filter_cycle.get(filter_mode, "all")
                        _clamp_selection()
                        _refresh(live)

                    elif action == 'restart':
                        # Restart the Token-API server
                        global restart_feedback
                        restart_feedback = (time.time(), "Restarting server...")
                        _refresh(live)

                        def _do_restart():
                            global restart_feedback, api_healthy, api_error_message
                            try:
                                result = subprocess.run(
                                    ["token-restart"],
                                    capture_output=True, text=True, timeout=15
                                )
                                if result.returncode == 0:
                                    restart_feedback = (time.time(), "Restarted server!")
                                    # Give server a moment to come back up
                                    time.sleep(2)
                                    api_healthy, api_error_message = check_api_health()
                                else:
                                    restart_feedback = (time.time(), f"Restart failed: {result.stderr[:30]}")
                            except FileNotFoundError:
                                restart_feedback = (time.time(), "token-restart not found")
                            except subprocess.TimeoutExpired:
                                restart_feedback = (time.time(), "Restart timed out")
                            except Exception as e:
                                restart_feedback = (time.time(), f"Restart error: {str(e)[:25]}")
                            update_flag.set()

                        threading.Thread(target=_do_restart, daemon=True).start()

                    elif action == 'full_refresh':
                        # Ctrl+R: restart server + re-exec TUI to pick up code changes
                        live.stop()
                        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, original_terminal_settings)
                        console.print("\n[cyan bold]Full refresh: restarting server and TUI...[/cyan bold]")
                        try:
                            subprocess.run(["token-restart"], capture_output=True, text=True, timeout=15)
                            console.print("[green]Server restarted.[/green] Re-launching TUI...")
                            time.sleep(1)
                        except Exception as e:
                            console.print(f"[yellow]Server restart issue: {e}[/yellow] Re-launching TUI anyway...")
                            time.sleep(0.5)
                        # Re-exec this process to pick up code changes
                        quit_flag.set()
                        listener_thread.join(timeout=0.5)
                        os.execv(sys.executable, [sys.executable] + sys.argv)

                    elif action == 'open_terminal' and displayed:
                        # Open a new terminal tab with resume command for selected instance
                        global resume_feedback
                        if 0 <= selected_index < len(displayed):
                            instance = displayed[selected_index]
                            instance_id = instance.get("id", "")
                            working_dir = instance.get("working_dir", "")
                            instance_name = format_instance_name(instance)

                            if not instance_id or not working_dir:
                                resume_feedback = (time.time(), "Missing instance data")
                            else:
                                resume_cmd = f"cd {working_dir} && claude --resume {instance_id}"
                                # Try to open in a new Windows Terminal tab
                                try:
                                    subprocess.Popen(
                                        ["cmd.exe", "/c", "start", "wt.exe", "-w", "0", "nt",
                                         "wsl.exe", "-e", "bash", "-ic", resume_cmd],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                                    )
                                    resume_feedback = (time.time(), f"Opened terminal for {instance_name}")
                                except FileNotFoundError:
                                    # Fallback: copy to clipboard
                                    copied, msg = copy_to_clipboard(resume_cmd)
                                    if copied:
                                        resume_feedback = (time.time(), f"Copied resume cmd (no wt.exe)")
                                    else:
                                        resume_feedback = (time.time(), msg)
                                except Exception as e:
                                    resume_feedback = (time.time(), f"Open failed: {str(e)[:25]}")
                        _refresh(live)

                    elif action == 'sort':
                        input_mode.set()
                        time.sleep(0.1)
                        live.stop()

                        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, original_terminal_settings)

                        console.print("\n[cyan bold]Sort instances by:[/cyan bold]")
                        console.print("  [yellow]1[/yellow] Status then recent activity (default)")
                        console.print("  [yellow]2[/yellow] Most recent activity")
                        console.print("  [yellow]3[/yellow] Most recently stopped")
                        console.print("  [yellow]4[/yellow] Instance creation time")
                        try:
                            choice = Prompt.ask("Choice", choices=["1", "2", "3", "4"], default="1")
                            sort_options = {
                                "1": "status",
                                "2": "recent_activity",
                                "3": "recent_stopped",
                                "4": "created"
                            }
                            sort_mode = sort_options.get(choice, "status")
                            console.print(f"[green]v[/green] Sorting by: {sort_mode.replace('_', ' ')}")
                        except (KeyboardInterrupt, EOFError):
                            console.print("[dim]Cancelled[/dim]")

                        time.sleep(0.3)
                        tty.setcbreak(sys.stdin.fileno())
                        input_mode.clear()
                        instances_cache = get_instances()
                        live.start()
                        _refresh(live)

                    elif action == 'page_prev':
                        panel_page = max(0, panel_page - 1)
                        # If user manually navigates away from Deploy during active deploy, disable auto-switch-back
                        if deploy_active and deploy_auto_switched and panel_page != 2:
                            deploy_auto_switched = False
                        _refresh(live)

                    elif action == 'page_next':
                        panel_page = min(PANEL_PAGE_MAX, panel_page + 1)
                        # If user manually navigates away from Deploy during active deploy, disable auto-switch-back
                        if deploy_active and deploy_auto_switched and panel_page != 2:
                            deploy_auto_switched = False
                        _refresh(live)

                    elif action == 'resume':
                        # Copy resume command to clipboard (y key)
                        if not displayed:
                            resume_feedback = (time.time(), "No instances")
                        elif not (0 <= selected_index < len(displayed)):
                            resume_feedback = (time.time(), "No instance selected")
                        else:
                            instance = displayed[selected_index]
                            instance_id = instance.get("id", "")
                            working_dir = instance.get("working_dir", "")
                            instance_name = format_instance_name(instance)

                            if not instance_id or not working_dir:
                                resume_feedback = (time.time(), "Missing instance data")
                            else:
                                resume_cmd = f"cd {working_dir} && claude --resume {instance_id}"
                                copied, msg = copy_to_clipboard(resume_cmd)
                                if copied:
                                    resume_feedback = (time.time(), f"Copied resume cmd for {instance_name}")
                                else:
                                    resume_feedback = (time.time(), msg)
                        _refresh(live)

                update_flag.clear()

                if time.time() - last_refresh >= REFRESH_INTERVAL:
                    old_count = len(instances_cache)
                    instances_cache = get_instances()
                    api_healthy, api_error_message = check_api_health()

                    # Auto-scroll to newest instance when new one appears
                    current_ids = set(i.get("id") for i in instances_cache)
                    new_ids = current_ids - prev_instance_ids
                    if new_ids and len(instances_cache) > old_count:
                        # Find the newest instance in the displayed (filtered) list
                        displayed = _get_displayed()
                        for idx, inst in enumerate(displayed):
                            if inst.get("id") in new_ids:
                                selected_index = idx
                                break
                    prev_instance_ids = current_ids

                    _clamp_selection()

                    # Deploy auto-switch logic
                    now_active, now_log, now_meta = check_deploy_status()
                    if now_active and not deploy_active:
                        # Deploy just started: save current page and switch to Deploy
                        deploy_previous_page = panel_page
                        panel_page = 2
                        deploy_auto_switched = True
                        deploy_log_path = now_log
                        deploy_metadata = now_meta
                    elif not now_active and deploy_active:
                        # Deploy just ended: switch back if we auto-switched
                        if deploy_auto_switched:
                            panel_page = deploy_previous_page
                            deploy_auto_switched = False
                        deploy_log_path = None
                        deploy_metadata = {}
                    deploy_active = now_active

                    _refresh(live)
                    last_refresh = time.time()

                update_flag.wait(timeout=0.02)

    except KeyboardInterrupt:
        pass
    finally:
        quit_flag.set()
        # Wait for listener thread to exit cleanly
        listener_thread.join(timeout=0.5)
        # Restore terminal settings (critical for Ctrl+C cleanup)
        try:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, original_terminal_settings)
        except:
            pass
        console.print("\n[dim]Goodbye![/dim]")


if __name__ == "__main__":
    main()
