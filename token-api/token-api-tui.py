#!/usr/bin/env python3
"""
Token-API TUI: Terminal dashboard for Claude instance management.

Can run in two modes:
  - Full mode: Starts API server + TUI in one terminal
  - TUI-only mode (--tui-only): Just the dashboard, connects to existing server

Controls:
  arrow/jk  - Select instance
  r         - Rename selected instance
  d         - Delete selected instance
  c         - Clear all instances
  R         - Restart the API server
  q         - Quit
"""

import sys
import os
import argparse
import json
import sqlite3
import time
import threading
import signal
import urllib.request
import logging
from collections import deque
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

# API configuration
API_URL = "http://localhost:7777"
SERVER_PORT = 7777

# Configuration
DB_PATH = Path.home() / ".claude" / "agents.db"
REFRESH_INTERVAL = 2  # seconds

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
api_healthy = True
api_error_message = None
layout_mode = "full"  # "mobile", "vertical", "compact", or "full"
layout_mode_forced = False  # True if user used --mobile, --vertical, --compact, or --no-mobile
sort_mode = "status"  # "status", "recent_activity", "recent_stopped", "created"
console = Console()

# Server log buffer (circular buffer for last N log entries)
MAX_LOG_LINES = 50
server_log_buffer = deque(maxlen=MAX_LOG_LINES)
log_buffer_lock = threading.Lock()


class TUILogHandler(logging.Handler):
    """Custom log handler that captures logs to the TUI buffer."""

    def emit(self, record):
        try:
            msg = self.format(record)
            timestamp = datetime.now().strftime("%H:%M:%S")
            level = record.levelname[:4]
            with log_buffer_lock:
                server_log_buffer.append(f"[dim]{timestamp}[/dim] [{self._level_color(level)}]{level}[/{self._level_color(level)}] {msg}")
        except Exception:
            pass

    def _level_color(self, level: str) -> str:
        colors = {"INFO": "green", "WARN": "yellow", "ERRO": "red", "DEBU": "dim", "CRIT": "red bold"}
        return colors.get(level, "white")

# Server state
server_thread: Optional[threading.Thread] = None
server_should_stop = threading.Event()
server_restart_requested = threading.Event()


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


def setup_server_logging():
    """Configure logging to capture server logs to the TUI buffer only (no stdout)."""
    # Create our custom handler
    tui_handler = TUILogHandler()
    tui_handler.setLevel(logging.INFO)
    tui_handler.setFormatter(logging.Formatter("%(message)s"))

    # Also clear root logger handlers to prevent any stdout leakage
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(tui_handler)
    root_logger.setLevel(logging.WARNING)  # Only warnings+ from unknown loggers

    # Configure uvicorn loggers - remove default handlers and add only ours
    for logger_name in ["uvicorn", "uvicorn.access", "uvicorn.error", "fastapi", "asyncio"]:
        logger = logging.getLogger(logger_name)
        # Remove all existing handlers (prevents stdout output)
        logger.handlers.clear()
        logger.addHandler(tui_handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False  # Don't send to root logger

    # Also capture our app logs
    app_logger = logging.getLogger("token_api")
    app_logger.handlers.clear()
    app_logger.addHandler(tui_handler)
    app_logger.setLevel(logging.INFO)
    app_logger.propagate = False

    return tui_handler


def run_server():
    """Run the uvicorn server in a thread."""
    import uvicorn
    from main import app

    # Add startup message to log buffer
    with log_buffer_lock:
        server_log_buffer.append(f"[dim]{datetime.now().strftime('%H:%M:%S')}[/dim] [green]INFO[/green] Starting server on port {SERVER_PORT}")

    # Note: We don't redirect stdout/stderr here to allow startup errors to be visible.
    # Logging capture is set up separately for runtime logs.
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=SERVER_PORT,
        log_level="warning",  # Reduce noise - our app uses token_api logger
        access_log=False
    )
    server = uvicorn.Server(config)
    server.run()


def start_server():
    """Start the server in a background thread."""
    global server_thread

    server_thread = threading.Thread(target=run_server, daemon=True, name="uvicorn-server")
    server_thread.start()

    # Wait for server to be ready
    max_wait = 10
    start = time.time()
    while time.time() - start < max_wait:
        try:
            req = urllib.request.Request(f"{API_URL}/health", method="GET")
            with urllib.request.urlopen(req, timeout=1) as response:
                if response.status == 200:
                    # Server is healthy - now set up log capture for TUI
                    setup_server_logging()
                    with log_buffer_lock:
                        server_log_buffer.append(f"[dim]{datetime.now().strftime('%H:%M:%S')}[/dim] [green]INFO[/green] Server ready and accepting connections")
                    return True
        except Exception:
            pass
        time.sleep(0.2)

    # Server failed to start - add error to log buffer
    with log_buffer_lock:
        server_log_buffer.append(f"[dim]{datetime.now().strftime('%H:%M:%S')}[/dim] [red]ERRO[/red] Server failed to start within {max_wait}s")
    return False


def restart_server():
    """Restart the server (requires full process restart for clean state)."""
    # For a clean restart, we need to restart the whole process
    # This is because uvicorn doesn't have a clean restart mechanism
    os.execv(sys.executable, [sys.executable] + sys.argv)


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


def get_instance_todos(instance_id: str) -> dict:
    """Fetch todos for an instance from the API."""
    try:
        req = urllib.request.Request(f"{API_URL}/api/instances/{instance_id}/todos")
        with urllib.request.urlopen(req, timeout=2) as response:
            return json.loads(response.read().decode())
    except Exception:
        return {"progress": 0, "current_task": None, "total": 0, "todos": []}


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
    """Fetch recent events from the database."""
    try:
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("""
            SELECT * FROM events
            ORDER BY created_at DESC
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


def get_tts_queue_status():
    """Fetch TTS queue status from the API."""
    try:
        req = urllib.request.Request(f"{API_URL}/api/notify/queue/status")
        with urllib.request.urlopen(req, timeout=2) as response:
            return json.loads(response.read().decode())
    except Exception:
        return {"current": None, "queue": [], "queue_length": 0}


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
        title="Claude Instances  [dim](↑↓ r=rename s=stop d=delete c=clear o=sort R=restart q=quit)[/dim]",
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
    table.add_column("Time", style="green", width=6, justify="right")

    for i, instance in enumerate(instances):
        selector = "[yellow]>[/yellow]" if i == selected_idx else " "
        name = format_instance_name(instance, max_len=30)
        if i == selected_idx:
            name = f"[bold yellow]{name}[/bold yellow]"

        device = instance.get("device_id", "?")
        todos = {"progress": 0, "current_task": None, "total": 0}
        # Only poll for todos when instance is actively processing (not just "active" status)
        if instance.get("is_processing", 0):
            todos = get_instance_todos(instance.get("id", ""))

        is_processing = instance.get("is_processing", 0)
        has_active_subtask = todos.get("current_task") is not None

        if instance["status"] == "stopped":
            status_icon = "[dim]o[/dim]"
        elif is_processing or has_active_subtask:
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
        duration = format_duration(instance.get("registered_at", ""), end_time)

        table.add_row(selector, status_icon, name, device, progress_text, current_task, duration)

    if not instances:
        table.add_row(" ", "[dim]-[/dim]", "[dim]No instances[/dim]", "-", "-", "-", "-")

    return table


def create_mobile_instances_table(instances: list, selected_idx: int) -> Table:
    """Create a compact instances table for mobile."""
    table = Table(
        title="Instances [dim](jk r s d c o R q)[/dim]",
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
    table.add_column("T", style="green", width=4, justify="right")

    for i, instance in enumerate(instances):
        selector = "[yellow]>[/yellow]" if i == selected_idx else " "
        name = format_instance_name(instance, max_len=18)
        if i == selected_idx:
            name = f"[bold yellow]{name}[/bold yellow]"

        todos = {"progress": 0, "total": 0, "current_task": None}
        # Only poll for todos when instance is actively processing (not just "active" status)
        if instance.get("is_processing", 0):
            todos = get_instance_todos(instance.get("id", ""))

        is_processing = instance.get("is_processing", 0)
        has_active_subtask = todos.get("current_task") is not None

        if instance["status"] == "stopped":
            status_icon = "[dim]o[/dim]"
        elif is_processing or has_active_subtask:
            status_icon = "[green]>[/green]"
        else:
            status_icon = "[cyan]*[/cyan]"

        if todos.get("total", 0) > 0:
            progress = todos.get("progress", 0)
            progress_bar = make_progress_bar(progress, 5)
        else:
            progress_bar = "[dim]-----[/dim]"

        end_time = instance.get("stopped_at") if instance["status"] == "stopped" else None
        duration = format_duration(instance.get("registered_at", ""), end_time)
        if " " in duration:
            duration = duration.split()[0]

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
        title="Claude Instances  [dim](↑↓/jk r=rename s=stop d=delete c=clear o=sort R=restart q=quit)[/dim]",
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
    table.add_column("Time", style="green", width=6, justify="right")

    for i, instance in enumerate(instances):
        selector = "[yellow]>[/yellow]" if i == selected_idx else " "
        name = format_instance_name(instance, max_len=35)
        if i == selected_idx:
            name = f"[bold yellow]{name}[/bold yellow]"

        device = instance.get("device_id", "?")
        todos = {"progress": 0, "current_task": None, "total": 0}
        # Only poll for todos when instance is actively processing
        if instance.get("is_processing", 0):
            todos = get_instance_todos(instance.get("id", ""))

        is_processing = instance.get("is_processing", 0)
        has_active_subtask = todos.get("current_task") is not None

        if instance["status"] == "stopped":
            status_icon = "[dim]o[/dim]"
        elif is_processing or has_active_subtask:
            status_icon = "[green]>[/green]"
        else:
            status_icon = "[cyan]*[/cyan]"

        if todos.get("total", 0) > 0:
            progress = todos.get("progress", 0)
            progress_bar = make_progress_bar(progress, 8)
            progress_text = f"{progress_bar} {progress}%"
        else:
            progress_text = "[dim]-[/dim]"

        end_time = instance.get("stopped_at") if instance["status"] == "stopped" else None
        duration = format_duration(instance.get("registered_at", ""), end_time)

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
            instance_id = event.get("instance_id", "")
            details = event.get("details", {}) if isinstance(event.get("details"), dict) else {}

            tab_name = details.get("tab_name", "") or details.get("new_name", "") or instance_id or "system"
            color, icon, action = EVENT_STYLES.get(event_type, ("dim", ".", event_type))

            if event_type == "instance_registered":
                tab_name = details.get("tab_name", instance_id) or instance_id
                msg = f"[{color}]{icon}[/{color}] [bold]{tab_name}[/bold]: [green]registered[/green]"
            elif event_type == "instance_stopped":
                msg = f"[{color}]{icon}[/{color}] [bold]{instance_id}[/bold]: [red]stopped[/red]"
            elif event_type == "instance_renamed":
                old_name = details.get("old_name", "?")
                new_name = details.get("new_name", "?")
                msg = f"[{color}]{icon}[/{color}] [bold]{old_name}[/bold] -> [bold]{new_name}[/bold]"
            elif event_type in ("tts_queued", "tts_playing", "tts_completed"):
                voice = details.get("voice", "").replace("Microsoft ", "").replace(" Desktop", "")
                msg = f"[{color}]{icon}[/{color}] [bold]{tab_name}[/bold]: [{color}]{action}[/{color}]"
                if voice and event_type == "tts_playing":
                    msg += f" [dim]({voice})[/dim]"
            else:
                msg = f"[{color}]{icon}[/{color}] [bold]{tab_name}[/bold]: [{color}]{action}[/{color}]"

            lines.append(f"[dim]{time_str}[/dim]  {msg}")
        except Exception:
            continue

    if not lines:
        lines.append("[dim]No recent events[/dim]")

    content = "\n\n".join(lines[:6])
    return Panel(content, title="Recent Events", border_style="blue")


def create_mobile_events_panel(events: list) -> Panel:
    """Create a compact events panel for mobile."""
    lines = []

    EVENT_ICONS = {
        "instance_registered": "[green]+[/green]",
        "instance_stopped": "[red]-[/red]",
        "instance_renamed": "[yellow]~[/yellow]",
        "tts_playing": "[cyan]>[/cyan]",
        "notification_sent": "[magenta]*[/magenta]",
    }

    for event in events[:4]:
        try:
            created = event.get("created_at", "")
            time_str = created.split(" ")[1][:5] if " " in created else "??:??"

            event_type = event.get("event_type", "unknown")
            details = event.get("details", {}) if isinstance(event.get("details"), dict) else {}

            icon = EVENT_ICONS.get(event_type, "[dim].[/dim]")
            tab_name = details.get("tab_name", "") or details.get("new_name", "") or event.get("instance_id", "?")
            if len(tab_name) > 12:
                tab_name = tab_name[:10] + ".."

            lines.append(f"[dim]{time_str}[/dim] {icon} {tab_name}")
        except Exception:
            continue

    if not lines:
        lines.append("[dim]No events[/dim]")

    return Panel("\n".join(lines), title="Events", border_style="blue", padding=(0, 1))


def create_tts_queue_panel(queue_status: dict) -> Panel:
    """Create the TTS queue panel."""
    lines = []

    current = queue_status.get("current")
    if current:
        voice_short = current.get("voice", "").replace("Microsoft ", "").replace(" Desktop", "")
        lines.append(f"[yellow]> NOW:[/yellow] {current.get('tab_name', '?')}")
        lines.append(f"  [dim]{voice_short}: {current.get('message', '')[:40]}[/dim]")

    queue = queue_status.get("queue", [])
    if queue:
        lines.append("")
        lines.append(f"[cyan]Queued ({len(queue)}):[/cyan]")
        for i, item in enumerate(queue[:3]):
            voice_short = item.get("voice", "").replace("Microsoft ", "").replace(" Desktop", "")
            lines.append(f"  {i+1}. {item.get('tab_name', '?')} ({voice_short})")

        if len(queue) > 3:
            lines.append(f"  [dim]... and {len(queue) - 3} more[/dim]")
    elif not current:
        lines.append("[dim]No TTS playing or queued[/dim]")

    content = "\n".join(lines) if lines else "[dim]Queue empty[/dim]"
    return Panel(content, title="TTS Queue", border_style="yellow")


def create_server_logs_panel(max_lines: int = 8) -> Panel:
    """Create a panel showing recent server logs."""
    with log_buffer_lock:
        recent_logs = list(server_log_buffer)

    if not recent_logs:
        content = "[dim]No server logs yet...[/dim]"
    else:
        # Get the most recent logs that fit
        display_logs = recent_logs[-max_lines:]
        content = "\n".join(display_logs)

    return Panel(content, title="Server Logs", border_style="blue")


def create_instance_details_panel(instance: dict, todos_data: dict) -> Panel:
    """Create a panel showing details for the selected instance."""
    lines = []

    if not instance:
        return Panel("[dim]No instance selected[/dim]", title="Instance Details", border_style="magenta")

    name = format_instance_name(instance, max_len=25)
    status = instance.get("status", "unknown")
    device = instance.get("device_id", "?")
    status_icon = "[green]*[/green]" if status == "active" else "[dim]o[/dim]"

    lines.append(f"{status_icon} [bold]{name}[/bold]  [dim]({device})[/dim]")
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
    status_icon = "[green]*[/green]" if status == "active" else "[dim]o[/dim]"
    lines.append(f"{status_icon} [bold]{name}[/bold]")

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
        content = "[green]* Server running[/green] on port 7777\n[dim]Press R to restart[/dim]"
        border = "green"
    else:
        content = f"[red]! Server error[/red]\n[dim]{api_error_message or 'Unknown error'}[/dim]"
        border = "red"

    return Panel(content, title="API Server", border_style=border)


def create_status_bar(instances: list, selected_idx: int) -> Text:
    """Create the status bar."""
    active_count = sum(1 for i in instances if i.get("status") == "active")
    total_count = len(instances)
    productivity = "[green]ACTIVE[/green]" if active_count > 0 else "[dim]IDLE[/dim]"

    # Mode indicator with color
    mode_colors = {"mobile": "yellow", "vertical": "magenta", "compact": "blue", "full": "cyan"}
    mode_color = mode_colors.get(layout_mode, "white")

    text = Text()
    text.append(f"Instances: {active_count}/{total_count}  |  ", style="white")
    text.append_text(Text.from_markup(f"[{mode_color}]{layout_mode}[/{mode_color}]"))
    text.append(f"  |  {selected_idx + 1}/{total_count}  |  ", style="white")
    text.append("[dim]c=clear  R=restart  q=quit[/dim]")

    return text


def create_mobile_status_bar(instances: list, selected_idx: int) -> Text:
    """Create a compact status bar for mobile."""
    active_count = sum(1 for i in instances if i.get("status") == "active")
    total_count = len(instances)

    text = Text()
    text.append(f"{active_count}/{total_count} ", style="white")
    if active_count > 0:
        text.append("*", style="green")
    else:
        text.append("o", style="dim")
    text.append(f"  sel:{selected_idx + 1}", style="dim")

    return text


def generate_mobile_dashboard(instances: list, selected_idx: int) -> Layout:
    """Generate a compact dashboard layout for mobile."""
    global api_healthy, api_error_message

    events = get_recent_events(4)

    selected_instance = None
    selected_todos = {"progress": 0, "current_task": None, "total": 0, "todos": []}
    if instances and 0 <= selected_idx < len(instances):
        selected_instance = instances[selected_idx]
        # Only poll for todos when instance is actively processing (not just "active" status)
        if selected_instance.get("is_processing", 0):
            selected_todos = get_instance_todos(selected_instance.get("id", ""))

    layout = Layout()

    if not api_healthy:
        layout.split_column(
            Layout(name="error", size=2),
            Layout(name="instances"),
            Layout(name="details", size=5),
            Layout(name="server_logs", size=5),
            Layout(name="footer", size=1)
        )
        error_text = Text()
        error_text.append("! API down", style="bold red")
        layout["error"].update(Panel(error_text, border_style="red"))
    else:
        layout.split_column(
            Layout(name="instances"),
            Layout(name="details", size=5),
            Layout(name="server_logs", size=5),
            Layout(name="footer", size=1)
        )

    layout["instances"].update(create_mobile_instances_table(instances, selected_idx))
    layout["details"].update(create_mobile_instance_details_panel(selected_instance, selected_todos))
    layout["server_logs"].update(create_server_logs_panel(max_lines=3))
    layout["footer"].update(create_mobile_status_bar(instances, selected_idx))

    return layout


def generate_compact_dashboard(instances: list, selected_idx: int) -> Layout:
    """Generate compact dashboard without sidebar (for medium-width terminals)."""
    global api_healthy, api_error_message

    selected_instance = None
    selected_todos = {"progress": 0, "current_task": None, "total": 0, "todos": []}
    if instances and 0 <= selected_idx < len(instances):
        selected_instance = instances[selected_idx]
        if selected_instance.get("is_processing", 0):
            selected_todos = get_instance_todos(selected_instance.get("id", ""))

    layout = Layout()

    # Compact header + main content + footer
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="instances"),
        Layout(name="server_logs", size=4),
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

    header_layout["title"].update(Panel(
        Text("TOKEN-API TUI", style="bold white", justify="center"),
        border_style="cyan"
    ))
    header_layout["server_status"].update(Panel(
        Text(server_text, justify="center"),
        border_style="green" if api_healthy else "red"
    ))
    layout["header"].update(header_layout)

    layout["instances"].update(create_compact_instances_table(instances, selected_idx))
    layout["server_logs"].update(create_server_logs_panel(max_lines=3))
    layout["footer"].update(create_status_bar(instances, selected_idx))

    return layout


def generate_vertical_dashboard(instances: list, selected_idx: int) -> Layout:
    """Generate vertical dashboard with stacked panels (for vertical monitors).

    Dynamically sizes panels based on available height and content:
    - Instance table sized to fit content (with reasonable bounds)
    - Extra space distributed to details and logs
    """
    global api_healthy, api_error_message

    selected_instance = None
    selected_todos = {"progress": 0, "current_task": None, "total": 0, "todos": []}
    if instances and 0 <= selected_idx < len(instances):
        selected_instance = instances[selected_idx]
        if selected_instance.get("is_processing", 0):
            selected_todos = get_instance_todos(selected_instance.get("id", ""))

    # Calculate adaptive sizes based on terminal height and content
    height = console.size.height

    # Fixed elements
    header_size = 3
    footer_size = 1
    spacer_size = 1  # Breathing room between table and details

    # Instance table: needs rows for header + data + borders
    num_instances = max(len(instances), 1)
    # Table needs: title + header + separator + N data rows + borders = N + 6
    table_ideal = num_instances + 6
    # Clamp between reasonable bounds
    table_min = 8   # At least show a few rows with full borders
    table_max = 22  # Don't let table dominate

    # Available space for flexible content (minus spacer)
    available = height - header_size - footer_size - spacer_size

    # Minimum sizes for other panels
    details_min = 6
    logs_min = 4

    # Calculate instance table size
    instance_size = max(table_min, min(table_ideal, table_max))

    # Remaining space after fixed elements and table
    remaining = available - instance_size

    if remaining >= details_min + logs_min + 10:
        # Plenty of space - distribute extra to details and logs
        extra = remaining - details_min - logs_min
        details_size = details_min + (extra * 2 // 3)  # 2/3 to details
        logs_size = logs_min + (extra // 3)            # 1/3 to logs
    elif remaining >= details_min + logs_min:
        # Just enough - use minimums
        details_size = details_min
        logs_size = logs_min
    else:
        # Tight space - shrink proportionally
        details_size = max(4, remaining * 2 // 3)
        logs_size = max(3, remaining // 3)

    layout = Layout()

    # Vertical layout: everything stacked with calculated sizes + spacer for breathing room
    layout.split_column(
        Layout(name="header", size=header_size),
        Layout(name="instances", size=instance_size),
        Layout(name="spacer", size=spacer_size),
        Layout(name="details", size=details_size),
        Layout(name="server_logs", size=logs_size),
        Layout(name="footer", size=footer_size)
    )

    # Empty spacer
    layout["spacer"].update(Text(""))

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

    header_layout["title"].update(Panel(
        Text("TOKEN-API TUI", style="bold white", justify="center"),
        border_style="cyan"
    ))
    header_layout["server_status"].update(Panel(
        Text(server_text, justify="center"),
        border_style="green" if api_healthy else "red"
    ))
    layout["header"].update(header_layout)

    # Calculate how many log lines fit in the allocated space (panel has 2 border lines)
    log_lines = max(1, logs_size - 2)

    layout["instances"].update(create_compact_instances_table(instances, selected_idx))
    layout["details"].update(create_instance_details_panel(selected_instance, selected_todos))
    layout["server_logs"].update(create_server_logs_panel(max_lines=log_lines))
    layout["footer"].update(create_status_bar(instances, selected_idx))

    return layout


def generate_dashboard(instances: list, selected_idx: int) -> Layout:
    """Generate the full dashboard layout."""
    global api_healthy, api_error_message

    events = get_recent_events(6)
    tts_queue = get_tts_queue_status()

    selected_instance = None
    selected_todos = {"progress": 0, "current_task": None, "total": 0, "todos": []}
    if instances and 0 <= selected_idx < len(instances):
        selected_instance = instances[selected_idx]
        # Only poll for todos when instance is actively processing (not just "active" status)
        if selected_instance.get("is_processing", 0):
            selected_todos = get_instance_todos(selected_instance.get("id", ""))

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
    header_layout["title"].update(Panel(
        Text("TOKEN-API TUI  [dim]v1.1[/dim]", style="bold white", justify="center"),
        border_style="cyan"
    ))
    header_layout["server_status"].update(create_server_status_panel())
    layout["header"].update(header_layout)

    # Main content
    layout["main"].split_row(
        Layout(name="left_column", ratio=3),
        Layout(name="sidebar", ratio=2)
    )

    # Left column: instances table + server logs
    layout["left_column"].split_column(
        Layout(name="instances", ratio=3),
        Layout(name="server_logs", ratio=1)
    )

    layout["sidebar"].split_column(
        Layout(name="instance_details", ratio=2),
        Layout(name="events", ratio=1),
        Layout(name="tts_queue", ratio=1)
    )

    layout["instances"].update(create_instances_table(instances, selected_idx))
    layout["server_logs"].update(create_server_logs_panel(max_lines=6))
    layout["instance_details"].update(create_instance_details_panel(selected_instance, selected_todos))
    layout["events"].update(create_events_panel(events))
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
    global selected_index, instances_cache, api_healthy, api_error_message, layout_mode, layout_mode_forced, sort_mode

    parser = argparse.ArgumentParser(description="Token-API TUI Dashboard")
    parser.add_argument("--mobile", "-m", action="store_true",
                        help="Force mobile-friendly layout")
    parser.add_argument("--vertical", "-v", action="store_true",
                        help="Force vertical layout (stacked panels)")
    parser.add_argument("--compact", action="store_true",
                        help="Force compact layout (no sidebar)")
    parser.add_argument("--no-mobile", action="store_true",
                        help="Force full desktop layout even on narrow terminals")
    parser.add_argument("--tui-only", action="store_true",
                        help="Don't start server, just run TUI (connects to existing server)")
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

    if not args.tui_only:
        console.print(f"[cyan]Starting Token-API TUI + Server[/cyan] ({mode_indicator} mode)")
        console.print("[dim]Starting API server...[/dim]")

        if start_server():
            console.print("[green]* Server started on port 7777[/green]")
        else:
            console.print("[yellow]! Server may not have started properly[/yellow]")
    else:
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

    console.print("[dim]Controls: arrow/jk=select, r=rename, d=delete, c=clear all, R=restart, q=quit[/dim]\n")

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
                    elif key == 'R':  # Uppercase R for restart
                        with action_lock:
                            action_queue.append('restart')
                        update_flag.set()
                    elif key.lower() == 'r':
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

    try:
        with Live(get_dashboard(instances_cache, selected_index), console=console, refresh_per_second=10, screen=True) as live:
            last_refresh = time.time()

            while not quit_flag.is_set():
                actions_to_process = []
                with action_lock:
                    if action_queue:
                        actions_to_process = action_queue.copy()
                        action_queue.clear()

                for action in actions_to_process:
                    if action == 'up' and instances_cache:
                        selected_index = max(0, selected_index - 1)
                        live.update(get_dashboard(instances_cache, selected_index))
                        live.refresh()

                    elif action == 'down' and instances_cache:
                        selected_index = min(len(instances_cache) - 1, selected_index + 1)
                        live.update(get_dashboard(instances_cache, selected_index))
                        live.refresh()

                    elif action == 'restart':
                        input_mode.set()
                        time.sleep(0.1)
                        live.stop()

                        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, original_terminal_settings)

                        console.print("\n[yellow]Restarting server...[/yellow]")
                        time.sleep(0.5)
                        restart_server()

                    elif action == 'rename' and instances_cache:
                        if 0 <= selected_index < len(instances_cache):
                            instance = instances_cache[selected_index]
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
                            input_mode.clear()
                            instances_cache = get_instances()
                            live.start()
                            live.update(get_dashboard(instances_cache, selected_index))
                            live.refresh()

                    elif action == 'delete' and instances_cache:
                        if 0 <= selected_index < len(instances_cache):
                            instance = instances_cache[selected_index]
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
                                        if selected_index >= len(instances_cache) - 1:
                                            selected_index = max(0, selected_index - 1)
                                    else:
                                        console.print("[red]x[/red] Delete failed")
                                else:
                                    console.print("[dim]Cancelled[/dim]")
                            except (KeyboardInterrupt, EOFError):
                                console.print("[dim]Cancelled[/dim]")

                            time.sleep(0.3)
                            input_mode.clear()
                            instances_cache = get_instances()
                            if instances_cache:
                                selected_index = min(selected_index, len(instances_cache) - 1)
                            live.start()
                            live.update(get_dashboard(instances_cache, selected_index))
                            live.refresh()

                    elif action == 'delete_all':
                        total_count = len(instances_cache) if instances_cache else 0

                        if total_count == 0:
                            input_mode.set()
                            live.stop()
                            console.print("\n[dim]No instances to clear.[/dim]")
                            time.sleep(1)
                            input_mode.clear()
                            live.start()
                            live.update(get_dashboard(instances_cache, selected_index))
                            live.refresh()
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
                        input_mode.clear()
                        instances_cache = get_instances()
                        if instances_cache:
                            selected_index = min(selected_index, len(instances_cache) - 1)
                        live.start()
                        live.update(get_dashboard(instances_cache, selected_index))
                        live.refresh()

                    elif action == 'stop' and instances_cache:
                        if 0 <= selected_index < len(instances_cache):
                            instance = instances_cache[selected_index]
                            instance_id = instance.get("id")
                            instance_name = format_instance_name(instance)

                            # Stop without confirmation (it's non-destructive)
                            if delete_instance(instance_id):
                                instances_cache = get_instances()
                                live.update(get_dashboard(instances_cache, selected_index))
                                live.refresh()

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
                        input_mode.clear()
                        instances_cache = get_instances()
                        live.start()
                        live.update(get_dashboard(instances_cache, selected_index))
                        live.refresh()

                update_flag.clear()

                if time.time() - last_refresh >= REFRESH_INTERVAL:
                    instances_cache = get_instances()
                    api_healthy, api_error_message = check_api_health()
                    if instances_cache:
                        selected_index = min(selected_index, len(instances_cache) - 1)
                    live.update(get_dashboard(instances_cache, selected_index))
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
