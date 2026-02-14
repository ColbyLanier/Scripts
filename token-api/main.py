"""
Token-API: FastAPI Local Server for Claude Instance Management

This server provides:
- Claude instance registration and tracking
- Device identification (desktop vs SSH from phone)
- Notification routing
- Productivity gating
"""

import os
import re
import uuid
import json
import time
import signal
import random
import asyncio
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List
from contextlib import asynccontextmanager

import aiosqlite
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import subprocess
import requests
from pydantic import BaseModel, Field
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

# Configure logging for TUI capture
logger = logging.getLogger("token_api")
logger.setLevel(logging.INFO)

# ============ Server-side Log Buffer ============
from collections import deque
from typing import Deque

# Circular buffer to store recent log entries (max 100)
log_buffer: Deque[dict] = deque(maxlen=100)


class LogBufferHandler(logging.Handler):
    """Custom logging handler that captures logs to circular buffer."""

    def emit(self, record: logging.LogRecord):
        """Capture log record to buffer with timestamp, level, and message."""
        try:
            log_entry = {
                "timestamp": datetime.fromtimestamp(record.created).strftime("%H:%M:%S"),
                "level": record.levelname,
                "message": self.format(record)
            }
            log_buffer.append(log_entry)
        except Exception:
            # Silently fail to avoid logging errors in logging system
            pass


# Add buffer handler to logger
buffer_handler = LogBufferHandler()
buffer_handler.setLevel(logging.DEBUG)
buffer_handler.setFormatter(logging.Formatter('%(message)s'))
logger.addHandler(buffer_handler)

# Also capture uvicorn and fastapi logs
uvicorn_logger = logging.getLogger("uvicorn")
uvicorn_logger.addHandler(buffer_handler)

fastapi_logger = logging.getLogger("fastapi")
fastapi_logger.addHandler(buffer_handler)


# Configuration
DB_PATH = Path.home() / ".claude" / "agents.db"
SERVER_PORT = 7777  # Authoritative port for Token API
CRASH_LOG_PATH = Path.home() / ".claude" / "token-api-crash.log"


# ============ Crash Logging ============
import sys
import traceback


def log_crash(exc_type, exc_value, exc_tb, context: str = "unhandled"):
    """Write crash info to persistent file for post-mortem debugging."""
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tb_lines = traceback.format_exception(exc_type, exc_value, exc_tb)
        tb_str = "".join(tb_lines)

        with open(CRASH_LOG_PATH, "a") as f:
            f.write(f"\n{'='*60}\n")
            f.write(f"CRASH [{context}] at {timestamp}\n")
            f.write(f"{'='*60}\n")
            f.write(tb_str)
            f.write("\n")

        # Also print to stderr so journald captures it
        print(f"CRASH [{context}]: {exc_type.__name__}: {exc_value}", file=sys.stderr)
    except Exception:
        pass  # Don't crash while logging a crash


def _global_exception_handler(exc_type, exc_value, exc_tb):
    """Global exception handler for uncaught sync exceptions."""
    log_crash(exc_type, exc_value, exc_tb, context="sync")
    # Call the default handler to preserve normal behavior
    sys.__excepthook__(exc_type, exc_value, exc_tb)


def _asyncio_exception_handler(loop, context):
    """Handler for uncaught exceptions in asyncio tasks."""
    exception = context.get("exception")
    if exception:
        log_crash(type(exception), exception, exception.__traceback__, context="asyncio")
    else:
        # Log context message if no exception object
        try:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(CRASH_LOG_PATH, "a") as f:
                f.write(f"\n{'='*60}\n")
                f.write(f"ASYNCIO ERROR at {timestamp}\n")
                f.write(f"{'='*60}\n")
                f.write(f"{context}\n\n")
        except Exception:
            pass

    # Call the default handler
    loop.default_exception_handler(context)


# Install global exception handlers
sys.excepthook = _global_exception_handler

# Device IP mapping for SSH detection
DEVICE_IPS = {
    "100.102.92.24": "Token-S24",    # Phone
    "100.69.198.87": "TokenPC",      # Windows PC
    "100.66.10.74": "TokenPC",       # WSL (same physical machine)
    "100.95.109.23": "Mac-Mini",     # Mac Mini (Tailscale)
    "127.0.0.1": "Mac-Mini",         # Mac Mini (localhost)
}

# Profile pool for voice/sound assignment
# macOS voices: Daniel (British), Karen (Australian), Moira (Irish), Rishi (Indian)
PROFILES = [
    {"name": "profile_1", "tts_voice": "Daniel", "notification_sound": "chimes.wav", "color": "#0099ff"},      # British
    {"name": "profile_2", "tts_voice": "Karen", "notification_sound": "notify.wav", "color": "#00cc66"},       # Australian
    {"name": "profile_3", "tts_voice": "Moira", "notification_sound": "ding.wav", "color": "#ff9900"},         # Irish
    {"name": "profile_4", "tts_voice": "Rishi", "notification_sound": "tada.wav", "color": "#cc66ff"},         # Indian
]

# Scheduler instance
scheduler = AsyncIOScheduler()


# Pydantic Models
class InstanceRegisterRequest(BaseModel):
    instance_id: str
    origin_type: str = "local"  # 'local' or 'ssh'
    source_ip: Optional[str] = None
    device_id: Optional[str] = None
    pid: Optional[int] = None
    tab_name: Optional[str] = None
    working_dir: Optional[str] = None


class InstanceResponse(BaseModel):
    id: str
    session_id: str
    tab_name: Optional[str]
    working_dir: Optional[str]
    origin_type: str
    source_ip: Optional[str]
    device_id: str
    profile_name: str
    tts_voice: str
    notification_sound: str
    pid: Optional[int]
    status: str
    registered_at: str
    last_activity: str
    stopped_at: Optional[str]


class ActivityRequest(BaseModel):
    action: str  # "prompt_submit" or "stop"


class ProfileResponse(BaseModel):
    session_id: str
    profile: dict


class DashboardResponse(BaseModel):
    instances: List[dict]
    productivity_active: bool
    recent_events: List[dict]
    tts_queue: Optional[dict] = None  # TTS queue status


class TaskResponse(BaseModel):
    id: str
    name: str
    description: Optional[str]
    task_type: str
    schedule: str
    enabled: bool
    max_retries: int
    last_run: Optional[dict] = None
    next_run: Optional[str] = None


class TaskUpdateRequest(BaseModel):
    schedule: Optional[str] = None
    enabled: Optional[bool] = None
    max_retries: Optional[int] = None


class TaskExecutionResponse(BaseModel):
    id: int
    task_id: str
    status: str
    started_at: str
    completed_at: Optional[str]
    duration_ms: Optional[int]
    result: Optional[dict]
    retry_count: int


class NotifyRequest(BaseModel):
    message: str
    device_id: Optional[str] = None  # If None, notify based on active instances
    instance_id: Optional[str] = None  # Notify specific instance's device
    voice: Optional[str] = None  # Override TTS voice
    sound: Optional[str] = None  # Override sound file


class TTSRequest(BaseModel):
    message: str
    voice: Optional[str] = None
    rate: int = 0  # -10 to 10, 0 is normal speed
    instance_id: Optional[str] = None  # Track which instance triggered TTS


class SoundRequest(BaseModel):
    sound_file: Optional[str] = None  # Path to sound file


class WindowCheckRequest(BaseModel):
    """Request to check if a window should be allowed or closed."""
    window_title: Optional[str] = None  # e.g., "YouTube - Brave"
    exe_name: Optional[str] = None  # e.g., "brave.exe"
    source: str = "ahk"  # Source of the request


# ============ Audio Proxy Models ============

class AudioProxyState(BaseModel):
    """Current state of the audio proxy system."""
    phone_connected: bool = False
    receiver_running: bool = False
    receiver_pid: Optional[int] = None
    last_connect_time: Optional[str] = None
    last_disconnect_time: Optional[str] = None


class AudioProxyConnectRequest(BaseModel):
    """Request when phone connects to PC Bluetooth."""
    phone_device_id: str = "Token-S24"
    bluetooth_device_name: Optional[str] = None
    source: str = "macrodroid"


class AudioProxyConnectResponse(BaseModel):
    """Response after processing connect request."""
    success: bool
    action: str  # "connected", "already_connected", "error"
    receiver_started: bool
    receiver_pid: Optional[int] = None
    message: str


class AudioProxyDisconnectRequest(BaseModel):
    """Request when phone disconnects from PC Bluetooth."""
    phone_device_id: str = "Token-S24"
    source: str = "macrodroid"


class AudioProxyStatusResponse(BaseModel):
    """Response for status query."""
    phone_connected: bool
    receiver_running: bool
    receiver_pid: Optional[int] = None
    last_connect_time: Optional[str] = None
    last_disconnect_time: Optional[str] = None


class WindowEnforceResponse(BaseModel):
    """Response for window enforcement decision."""
    productivity_active: bool
    active_instance_count: int
    should_close_distractions: bool
    distraction_apps: List[str]  # Apps that should be closed if should_close_distractions is True
    reason: str


class DesktopDetectionRequest(BaseModel):
    """Request from AHK desktop detection."""
    detected_mode: str  # "video" | "music" | "gaming" | "silence"
    window_title: Optional[str] = None
    source: str = "ahk"


class DesktopDetectionResponse(BaseModel):
    """Response for desktop detection."""
    action: str  # "mode_changed" | "blocked" | "none"
    detected_mode: str
    old_mode: Optional[str] = None
    new_mode: Optional[str] = None
    reason: str
    obsidian_triggered: bool = False
    productivity_active: bool
    active_instance_count: int


# ============ Phone Activity Models ============

class PhoneActivityRequest(BaseModel):
    """Request from MacroDroid for phone app activity."""
    app: str  # App name: "twitter", "youtube", "game", or app package name
    action: str = "open"  # "open" | "close"
    package: Optional[str] = None  # Optional package name for games


class PhoneActivityResponse(BaseModel):
    """Response for phone activity detection."""
    allowed: bool
    reason: str  # "break_time_available", "productivity_active", "blocked", "closed"
    break_seconds: int = 0
    message: Optional[str] = None


# ============ Headless Mode Models ============

class HeadlessStatusResponse(BaseModel):
    """Response for headless mode status."""
    enabled: bool
    last_changed: Optional[str] = None
    hostname: Optional[str] = None
    error: Optional[str] = None


class HeadlessControlRequest(BaseModel):
    """Request to control headless mode."""
    action: str = "toggle"  # "toggle" | "enable" | "disable"


class HeadlessControlResponse(BaseModel):
    """Response after controlling headless mode."""
    success: bool
    action: str
    before: HeadlessStatusResponse
    after: Optional[HeadlessStatusResponse] = None
    message: str


# ============ System Control Models ============

class ShutdownRequest(BaseModel):
    """Request to shutdown/restart the system."""
    action: str = "shutdown"  # "shutdown" | "restart"
    delay_seconds: int = 0  # Delay before shutdown (0 = immediate)
    force: bool = False  # Force close applications


class ShutdownResponse(BaseModel):
    """Response after initiating shutdown."""
    success: bool
    action: str
    delay_seconds: int
    message: str


# ============ Claude Code Hook Models ============

class HookResponse(BaseModel):
    """Standard response for hook handlers."""
    success: bool = True
    action: str
    details: Optional[dict] = None


class PreToolUseResponse(BaseModel):
    """Response for PreToolUse hooks that can block operations."""
    permissionDecision: Optional[str] = None  # "allow" or "deny"
    permissionDecisionReason: Optional[str] = None


# ============ Hook Handler State ============
# Debouncing for PostToolUse to avoid excessive API calls
_post_tool_debounce: dict = {}  # session_id -> last_call_time


# Database helper: connect with busy_timeout to prevent indefinite blocking
async def get_db():
    """Get a database connection with busy_timeout configured."""
    db = await aiosqlite.connect(DB_PATH)
    await db.execute("PRAGMA busy_timeout=5000")
    return db


# Database initialization
async def init_db():
    """Initialize SQLite database with required tables."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    async with aiosqlite.connect(DB_PATH) as db:
        # Set busy_timeout to prevent blocking on lock contention
        await db.execute("PRAGMA busy_timeout=5000")
        # Create claude_instances table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS claude_instances (
                id TEXT PRIMARY KEY,
                session_id TEXT UNIQUE NOT NULL,
                tab_name TEXT,
                working_dir TEXT,
                origin_type TEXT NOT NULL,
                source_ip TEXT,
                device_id TEXT NOT NULL,
                profile_name TEXT,
                tts_voice TEXT,
                notification_sound TEXT,
                pid INTEGER,
                status TEXT DEFAULT 'idle',
                is_processing INTEGER DEFAULT 0,
                registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                stopped_at TIMESTAMP
            )
        """)

        # Migration: add is_processing column if it doesn't exist
        cursor = await db.execute("PRAGMA table_info(claude_instances)")
        columns = [col[1] for col in await cursor.fetchall()]
        if 'is_processing' not in columns:
            await db.execute("ALTER TABLE claude_instances ADD COLUMN is_processing INTEGER DEFAULT 0")
        if 'working_dir' not in columns:
            await db.execute("ALTER TABLE claude_instances ADD COLUMN working_dir TEXT")

        # Migration: Convert two-field status (status + is_processing) to single enum
        # Old: status='active' + is_processing=0/1 → New: status='processing'/'idle'/'stopped'
        cursor = await db.execute("SELECT COUNT(*) FROM claude_instances WHERE status = 'active'")
        if (await cursor.fetchone())[0] > 0:
            await db.execute("""
                UPDATE claude_instances SET status = CASE
                    WHEN status = 'active' AND is_processing = 1 THEN 'processing'
                    WHEN status = 'active' AND is_processing = 0 THEN 'idle'
                    ELSE status
                END
            """)
            await db.commit()

        await db.execute("CREATE INDEX IF NOT EXISTS idx_instances_status ON claude_instances(status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_instances_device ON claude_instances(device_id)")

        # Create devices table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS devices (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                type TEXT NOT NULL,
                tailscale_ip TEXT UNIQUE,
                notification_method TEXT,
                webhook_url TEXT,
                tts_engine TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Create events table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                instance_id TEXT,
                device_id TEXT,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("CREATE INDEX IF NOT EXISTS idx_events_time ON events(created_at DESC)")

        # Create scheduled_tasks table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS scheduled_tasks (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT,
                task_type TEXT NOT NULL,
                schedule TEXT NOT NULL,
                enabled INTEGER DEFAULT 1,
                max_retries INTEGER DEFAULT 0,
                retry_delay_seconds INTEGER DEFAULT 60,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Create task_executions table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS task_executions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TIMESTAMP NOT NULL,
                completed_at TIMESTAMP,
                duration_ms INTEGER,
                result TEXT,
                retry_count INTEGER DEFAULT 0,
                FOREIGN KEY (task_id) REFERENCES scheduled_tasks(id)
            )
        """)

        await db.execute("CREATE INDEX IF NOT EXISTS idx_task_executions_task_id ON task_executions(task_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_task_executions_started_at ON task_executions(started_at)")

        # Create task_locks table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS task_locks (
                task_id TEXT PRIMARY KEY,
                locked_at TIMESTAMP NOT NULL,
                locked_by TEXT,
                FOREIGN KEY (task_id) REFERENCES scheduled_tasks(id)
            )
        """)

        # Create audio_proxy_state table (for phone audio routing through PC)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS audio_proxy_state (
                id INTEGER PRIMARY KEY DEFAULT 1,
                phone_connected INTEGER DEFAULT 0,
                receiver_running INTEGER DEFAULT 0,
                receiver_pid INTEGER,
                last_connect_time TEXT,
                last_disconnect_time TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                CHECK (id = 1)
            )
        """)

        # Seed devices if not exist
        await db.execute("""
            INSERT OR IGNORE INTO devices (id, name, type, tailscale_ip, notification_method, tts_engine)
            VALUES ('desktop', 'Desktop', 'local', '100.66.10.74', 'tts_sound', 'windows_sapi')
        """)

        await db.execute("""
            INSERT OR IGNORE INTO devices (id, name, type, tailscale_ip, notification_method, webhook_url)
            VALUES ('Token-S24', 'Pixel Phone', 'mobile', '100.102.92.24', 'webhook', 'http://100.102.92.24:7777/notify')
        """)

        # Seed scheduled tasks
        await db.execute("""
            INSERT OR IGNORE INTO scheduled_tasks (id, name, description, task_type, schedule, max_retries)
            VALUES ('cleanup_stale_instances', 'Cleanup Stale Instances',
                    'Mark instances with no activity for 3+ hours as stopped',
                    'interval', '30m', 2)
        """)

        await db.execute("""
            INSERT OR IGNORE INTO scheduled_tasks (id, name, description, task_type, schedule, max_retries)
            VALUES ('purge_old_events', 'Purge Old Events',
                    'Delete events older than 30 days',
                    'cron', '0 3 * * *', 1)
        """)

        await db.commit()
        print(f"Database initialized at {DB_PATH}")


async def log_event(event_type: str, instance_id: str = None, device_id: str = None, details: dict = None):
    """Log an event to the events table."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO events (event_type, instance_id, device_id, details)
               VALUES (?, ?, ?, ?)""",
            (event_type, instance_id, device_id, json.dumps(details) if details else None)
        )
        await db.commit()


def resolve_device_from_ip(ip: str) -> str:
    """Map Tailscale IPs to known devices."""
    return DEVICE_IPS.get(ip, "unknown")


# Devices where we can inspect local PIDs, send signals, etc.
LOCAL_DEVICES = {"desktop", "Mac-Mini", "TokenPC"}


def is_local_device(device_id: str) -> bool:
    """Check if device_id refers to a machine where we can manage processes locally."""
    return device_id in LOCAL_DEVICES


def get_next_available_profile(used_voices: set) -> dict:
    """Get a random available profile from the pool.

    Args:
        used_voices: Set of voice names currently in use by registered instances.

    Returns:
        A random profile whose voice is not in use, or a random profile if all are used.
    """
    available = [p for p in PROFILES if p["tts_voice"] not in used_voices]
    if available:
        return random.choice(available)
    # If all voices used, return random profile anyway
    return random.choice(PROFILES)


# ============ Scheduled Task System ============

def parse_interval_schedule(schedule: str) -> dict:
    """Parse interval schedule string like '30m', '1h', '5s' into trigger kwargs."""
    match = re.match(r'^(\d+)(s|m|h|d)$', schedule.strip().lower())
    if not match:
        raise ValueError(f"Invalid interval format: {schedule}. Use format like '30m', '1h', '5s'")

    value = int(match.group(1))
    unit = match.group(2)

    unit_map = {'s': 'seconds', 'm': 'minutes', 'h': 'hours', 'd': 'days'}
    return {unit_map[unit]: value}


async def acquire_task_lock(task_id: str) -> bool:
    """Try to acquire a lock for a task. Returns True if lock acquired."""
    now = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute(
                "INSERT INTO task_locks (task_id, locked_at, locked_by) VALUES (?, ?, ?)",
                (task_id, now, "main")
            )
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            # Lock already exists - check if it's stale (> 1 hour old)
            cursor = await db.execute(
                "SELECT locked_at FROM task_locks WHERE task_id = ?",
                (task_id,)
            )
            row = await cursor.fetchone()
            if row:
                locked_at = datetime.fromisoformat(row[0])
                if datetime.now() - locked_at > timedelta(hours=1):
                    # Stale lock, force acquire
                    await db.execute(
                        "UPDATE task_locks SET locked_at = ?, locked_by = ? WHERE task_id = ?",
                        (now, "main", task_id)
                    )
                    await db.commit()
                    return True
            return False


async def release_task_lock(task_id: str):
    """Release a task lock."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM task_locks WHERE task_id = ?", (task_id,))
        await db.commit()


async def log_task_start(task_id: str) -> int:
    """Log task execution start and return execution_id."""
    now = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO task_executions (task_id, status, started_at)
               VALUES (?, 'running', ?)""",
            (task_id, now)
        )
        await db.commit()
        return cursor.lastrowid


async def log_task_complete(execution_id: int, duration_ms: int, result: dict):
    """Log successful task completion."""
    now = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE task_executions
               SET status = 'completed', completed_at = ?, duration_ms = ?, result = ?
               WHERE id = ?""",
            (now, duration_ms, json.dumps(result), execution_id)
        )
        await db.commit()


async def log_task_failed(execution_id: int, error: str):
    """Log task failure."""
    now = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE task_executions
               SET status = 'failed', completed_at = ?, result = ?
               WHERE id = ?""",
            (now, json.dumps({"error": error}), execution_id)
        )
        await db.commit()


# ============ Task Implementations ============

async def cleanup_stale_instances() -> dict:
    """Mark instances with no activity for 3+ hours as stopped."""
    cutoff = (datetime.now() - timedelta(hours=3)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            UPDATE claude_instances
            SET status = 'stopped', stopped_at = CURRENT_TIMESTAMP
            WHERE status IN ('processing', 'idle')
              AND last_activity < ?
        """, (cutoff,))
        affected = cursor.rowcount
        await db.commit()

    if affected > 0:
        await log_event("task_cleanup", details={"cleaned_up": affected})

    return {"cleaned_up": affected}


async def purge_old_events() -> dict:
    """Delete events older than 30 days."""
    cutoff = (datetime.now() - timedelta(days=30)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM events WHERE created_at < ?",
            (cutoff,)
        )
        deleted = cursor.rowcount
        await db.commit()

    return {"deleted": deleted}


# Task registry mapping task IDs to their implementation functions
TASK_REGISTRY = {
    "cleanup_stale_instances": cleanup_stale_instances,
    "purge_old_events": purge_old_events,
}


async def execute_task(task_id: str):
    """Execute a scheduled task with locking and logging."""
    # Try to acquire lock
    if not await acquire_task_lock(task_id):
        print(f"Task {task_id} is already running, skipping")
        return

    # Log start
    execution_id = await log_task_start(task_id)

    try:
        start_time = time.time()

        # Execute the task
        task_func = TASK_REGISTRY.get(task_id)
        if not task_func:
            raise ValueError(f"Unknown task: {task_id}")

        result = await task_func()

        duration_ms = int((time.time() - start_time) * 1000)
        await log_task_complete(execution_id, duration_ms, result)
        print(f"Task {task_id} completed in {duration_ms}ms: {result}")

    except Exception as e:
        await log_task_failed(execution_id, str(e))
        print(f"Task {task_id} failed: {e}")

    finally:
        await release_task_lock(task_id)


async def load_tasks_from_db():
    """Load enabled tasks from database and register with scheduler."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, task_type, schedule FROM scheduled_tasks WHERE enabled = 1"
        )
        tasks = await cursor.fetchall()

    for task in tasks:
        task_id = task["id"]
        task_type = task["task_type"]
        schedule = task["schedule"]

        if task_id not in TASK_REGISTRY:
            print(f"Warning: Task {task_id} has no implementation, skipping")
            continue

        try:
            if task_type == "interval":
                trigger_kwargs = parse_interval_schedule(schedule)
                trigger = IntervalTrigger(**trigger_kwargs)
            elif task_type == "cron":
                # Parse cron expression (minute hour day month day_of_week)
                parts = schedule.split()
                if len(parts) == 5:
                    trigger = CronTrigger(
                        minute=parts[0],
                        hour=parts[1],
                        day=parts[2],
                        month=parts[3],
                        day_of_week=parts[4]
                    )
                else:
                    raise ValueError(f"Invalid cron expression: {schedule}")
            else:
                print(f"Unknown task type: {task_type}")
                continue

            scheduler.add_job(
                execute_task,
                trigger=trigger,
                args=[task_id],
                id=task_id,
                replace_existing=True
            )
            print(f"Registered task: {task_id} ({task_type}: {schedule})")

        except Exception as e:
            print(f"Failed to register task {task_id}: {e}")


async def run_overdue_tasks():
    """Check for tasks that haven't run recently and execute them on startup."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Get all enabled tasks
        cursor = await db.execute(
            "SELECT id, task_type, schedule FROM scheduled_tasks WHERE enabled = 1"
        )
        tasks = await cursor.fetchall()

        for task in tasks:
            task_id = task["id"]
            task_type = task["task_type"]
            schedule = task["schedule"]

            if task_id not in TASK_REGISTRY:
                continue

            # Determine the expected run interval for this task
            if task_type == "interval":
                try:
                    trigger_kwargs = parse_interval_schedule(schedule)
                    # Convert to timedelta
                    if "seconds" in trigger_kwargs:
                        expected_interval = timedelta(seconds=trigger_kwargs["seconds"])
                    elif "minutes" in trigger_kwargs:
                        expected_interval = timedelta(minutes=trigger_kwargs["minutes"])
                    elif "hours" in trigger_kwargs:
                        expected_interval = timedelta(hours=trigger_kwargs["hours"])
                    elif "days" in trigger_kwargs:
                        expected_interval = timedelta(days=trigger_kwargs["days"])
                    else:
                        expected_interval = timedelta(hours=24)
                except:
                    expected_interval = timedelta(hours=24)
            else:
                # For cron tasks, assume they should run at least once per day
                expected_interval = timedelta(hours=24)

            # Check last execution time
            cursor = await db.execute(
                """SELECT MAX(started_at) as last_run
                   FROM task_executions WHERE task_id = ?""",
                (task_id,)
            )
            row = await cursor.fetchone()

            should_run = False
            reason = ""

            if row["last_run"] is None:
                # Never run before
                should_run = True
                reason = "never run before"
            else:
                last_run = datetime.fromisoformat(row["last_run"])
                time_since_last = datetime.now() - last_run

                # Run if it's been more than 2x the expected interval
                # (gives some buffer for normal scheduling variance)
                if time_since_last > (expected_interval * 2):
                    should_run = True
                    hours_overdue = time_since_last.total_seconds() / 3600
                    reason = f"overdue by {hours_overdue:.1f} hours"

            if should_run:
                print(f"Startup check: Running {task_id} ({reason})")
                # Run asynchronously so we don't block startup
                asyncio.create_task(execute_task(task_id))


# Lifespan context manager
@asynccontextmanager
async def lifespan(app: FastAPI):
    global tts_worker_task, stale_flag_cleaner_task

    # Install asyncio exception handler for this loop
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(_asyncio_exception_handler)

    # Log startup to crash log for context
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(CRASH_LOG_PATH, "a") as f:
            f.write(f"\n--- SERVER STARTED at {timestamp} ---\n")
    except Exception:
        pass

    # Startup
    await init_db()
    await load_tasks_from_db()
    scheduler.start()
    print("Scheduler started")
    # Start TTS queue worker
    tts_worker_task = asyncio.create_task(tts_queue_worker())
    print("TTS queue worker started")
    # Start stale flag cleaner
    stale_flag_cleaner_task = asyncio.create_task(clear_stale_processing_flags())
    print("Stale flag cleaner started")
    # Start stuck instance detector
    stuck_detector_task = asyncio.create_task(detect_stuck_instances())
    print("Stuck instance detector started")
    await run_overdue_tasks()
    yield

    # Log shutdown to crash log
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(CRASH_LOG_PATH, "a") as f:
            f.write(f"--- SERVER STOPPING at {timestamp} ---\n")
    except Exception:
        pass

    # Shutdown
    if tts_worker_task:
        tts_worker_task.cancel()
        try:
            await tts_worker_task
        except asyncio.CancelledError:
            pass
    if stale_flag_cleaner_task:
        stale_flag_cleaner_task.cancel()
        try:
            await stale_flag_cleaner_task
        except asyncio.CancelledError:
            pass
    scheduler.shutdown(wait=True)
    print("Scheduler stopped")


# FastAPI App
app = FastAPI(
    title="Token-API",
    description="Local FastAPI server for Claude instance management",
    version="0.1.0",
    lifespan=lifespan
)

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Instance Registration Endpoints
@app.post("/api/instances/register", response_model=ProfileResponse)
async def register_instance(request: InstanceRegisterRequest):
    """Register a new Claude instance."""
    logger.info(f"Registering instance: {request.working_dir or request.tab_name or request.instance_id[:8]}")
    session_id = str(uuid.uuid4())

    # Resolve device_id from source_ip if not provided
    device_id = request.device_id
    if not device_id and request.source_ip:
        device_id = resolve_device_from_ip(request.source_ip)
    if not device_id:
        device_id = "Mac-Mini"  # Default for local sessions on Mac Mini

    async with aiosqlite.connect(DB_PATH) as db:
        # Get currently used voices (from all registered instances, not just active)
        # Voices are locked for the duration of a session until deleted
        cursor = await db.execute(
            "SELECT tts_voice FROM claude_instances"
        )
        rows = await cursor.fetchall()
        used_voices = {row[0] for row in rows if row[0]}

        # Assign random available profile
        profile = get_next_available_profile(used_voices)

        # Insert instance
        now = datetime.now().isoformat()
        await db.execute(
            """INSERT INTO claude_instances
               (id, session_id, tab_name, working_dir, origin_type, source_ip, device_id,
                profile_name, tts_voice, notification_sound, pid, status,
                registered_at, last_activity)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'idle', ?, ?)""",
            (
                request.instance_id,
                session_id,
                request.tab_name,
                request.working_dir,
                request.origin_type,
                request.source_ip,
                device_id,
                profile["name"],
                profile["tts_voice"],
                profile["notification_sound"],
                request.pid,
                now,
                now
            )
        )
        await db.commit()

    # Log event
    await log_event(
        "instance_registered",
        instance_id=request.instance_id,
        device_id=device_id,
        details={"tab_name": request.tab_name, "origin_type": request.origin_type}
    )

    return ProfileResponse(
        session_id=session_id,
        profile={
            "name": profile["name"],
            "tts_voice": profile["tts_voice"],
            "notification_sound": profile["notification_sound"],
            "color": profile.get("color", "#0099ff")
        }
    )


@app.delete("/api/instances/all")
async def delete_all_instances():
    """Delete all instances from the database (clear all)."""
    now = datetime.now().isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        # Get all instances before deleting
        cursor = await db.execute(
            "SELECT id, device_id, status FROM claude_instances"
        )
        all_instances = await cursor.fetchall()

        if not all_instances:
            return {"status": "no_instances", "deleted_count": 0}

        # Count active instances for enforcement check
        active_count = sum(1 for _, _, status in all_instances if status in ('processing', 'idle'))

        # Delete all instances from the database
        await db.execute("DELETE FROM claude_instances")
        await db.commit()

    # Log bulk deletion event
    await log_event(
        "bulk_delete_all",
        details={"count": len(all_instances), "timestamp": now}
    )

    # Check enforcement if there were active instances
    if active_count > 0 and DESKTOP_STATE.get("current_mode") == "video":
        enforce_result = close_distraction_windows()
        await log_event(
            "enforcement_triggered",
            details={"trigger": "all_instances_deleted", "result": enforce_result}
        )
        return {
            "status": "deleted_all",
            "deleted_count": len(all_instances),
            "enforcement_triggered": True,
            "enforcement_result": enforce_result
        }

    return {"status": "deleted_all", "deleted_count": len(all_instances)}


@app.delete("/api/instances/{instance_id}")
async def stop_instance(instance_id: str):
    """Mark an instance as stopped."""
    logger.info(f"Stopping instance: {instance_id[:12]}...")
    now = datetime.now().isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id, device_id FROM claude_instances WHERE id = ?",
            (instance_id,)
        )
        row = await cursor.fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="Instance not found")

        await db.execute(
            """UPDATE claude_instances
               SET status = 'stopped', stopped_at = ?
               WHERE id = ?""",
            (now, instance_id)
        )
        await db.commit()

        # Check if this was the last active instance
        cursor = await db.execute(
            "SELECT COUNT(*) FROM claude_instances WHERE status IN ('processing', 'idle')"
        )
        count_row = await cursor.fetchone()
        remaining_active = count_row[0] if count_row else 0

    # Log event
    await log_event(
        "instance_stopped",
        instance_id=instance_id,
        device_id=row[1]
    )

    # If no more active instances and video mode was active, enforce
    if remaining_active == 0 and DESKTOP_STATE.get("current_mode") == "video":
        print(f"ENFORCE: Last instance stopped while in video mode, closing distractions")
        enforce_result = close_distraction_windows()
        await log_event(
            "enforcement_triggered",
            details={
                "trigger": "last_instance_stopped",
                "result": enforce_result
            }
        )
        return {
            "status": "stopped",
            "instance_id": instance_id,
            "enforcement_triggered": True,
            "enforcement_result": enforce_result
        }

    return {"status": "stopped", "instance_id": instance_id}


async def find_claude_pid_by_workdir(working_dir: str) -> Optional[int]:
    """Scan /proc for claude processes matching the working directory.

    Returns the PID if exactly one match is found, None otherwise.
    """
    if not working_dir:
        return None

    matches = []
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            try:
                comm_path = f"/proc/{pid}/comm"
                with open(comm_path, "r") as f:
                    comm = f.read().strip()
                if comm != "claude":
                    continue
                cwd_path = f"/proc/{pid}/cwd"
                cwd = os.readlink(cwd_path)
                if cwd.rstrip("/") == working_dir.rstrip("/"):
                    matches.append(pid)
            except (OSError, PermissionError):
                continue
    except OSError:
        return None

    if len(matches) == 1:
        return matches[0]
    return None


def is_pid_claude(pid: int) -> bool:
    """Check if the given PID belongs to a claude process."""
    try:
        with open(f"/proc/{pid}/comm", "r") as f:
            return f.read().strip() == "claude"
    except (OSError, PermissionError):
        return False


@app.post("/api/instances/{instance_id}/kill")
async def kill_instance(instance_id: str):
    """Kill a frozen Claude instance process and mark it stopped.

    Sends SIGINT twice (mimics double Ctrl+C for graceful exit),
    then SIGKILL if needed. Supports both desktop (direct kill)
    and phone (SSH kill) instances.
    """
    logger.info(f"Kill request for instance: {instance_id[:12]}...")
    now = datetime.now().isoformat()

    # Look up instance
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM claude_instances WHERE id = ?",
            (instance_id,)
        )
        row = await cursor.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Instance not found")

    instance = dict(row)
    pid = instance.get("pid")
    device_id = instance.get("device_id", "Mac-Mini")
    working_dir = instance.get("working_dir", "")
    kill_signal = None

    # If no PID stored, attempt process discovery fallback
    if not pid:
        if is_local_device(device_id):
            pid = await find_claude_pid_by_workdir(working_dir)
            if pid:
                logger.info(f"Kill: discovered PID {pid} via /proc scan for {working_dir}")
            else:
                # Mark stopped in DB anyway (cleanup)
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute(
                        "UPDATE claude_instances SET status = 'stopped', stopped_at = ? WHERE id = ?",
                        (now, instance_id)
                    )
                    await db.commit()
                await log_event("instance_killed", instance_id=instance_id, device_id=device_id,
                                details={"error": "no_pid", "status": "marked_stopped"})
                raise HTTPException(
                    status_code=400,
                    detail="No PID stored and could not discover process. Instance marked stopped."
                )
        else:
            # Can't scan /proc on remote device
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE claude_instances SET status = 'stopped', stopped_at = ? WHERE id = ?",
                    (now, instance_id)
                )
                await db.commit()
            await log_event("instance_killed", instance_id=instance_id, device_id=device_id,
                            details={"error": "no_pid_remote", "status": "marked_stopped"})
            raise HTTPException(
                status_code=400,
                detail=f"No PID stored for remote device '{device_id}'. Instance marked stopped."
            )

    # Kill sequence based on device type
    if is_local_device(device_id):
        # Validate PID still belongs to claude
        if not is_pid_claude(pid):
            # Process already exited or PID reused by another process
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE claude_instances SET status = 'stopped', stopped_at = ? WHERE id = ?",
                    (now, instance_id)
                )
                await db.commit()
            await log_event("instance_killed", instance_id=instance_id, device_id=device_id,
                            details={"pid": pid, "status": "already_dead"})
            return {"status": "already_dead", "pid": pid, "signal": None}

        # SIGINT×2 (mimics double Ctrl+C: first cancels operation, second exits gracefully)
        try:
            os.kill(pid, signal.SIGINT)
            kill_signal = "SIGINT"
            logger.info(f"Kill: sent first SIGINT to PID {pid}")
        except ProcessLookupError:
            # Already dead
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE claude_instances SET status = 'stopped', stopped_at = ? WHERE id = ?",
                    (now, instance_id)
                )
                await db.commit()
            await log_event("instance_killed", instance_id=instance_id, device_id=device_id,
                            details={"pid": pid, "status": "already_dead"})
            return {"status": "already_dead", "pid": pid, "signal": None}
        except PermissionError:
            raise HTTPException(status_code=500, detail=f"Permission denied killing PID {pid}")

        # Wait 1s then send second SIGINT
        await asyncio.sleep(1)
        if is_pid_claude(pid):
            try:
                os.kill(pid, signal.SIGINT)
                kill_signal = "SIGINT_x2"
                logger.info(f"Kill: sent second SIGINT to PID {pid}")
            except ProcessLookupError:
                pass  # Died after first SIGINT

        # Wait 3s for graceful shutdown
        await asyncio.sleep(3)

        # Check if still alive, escalate to SIGKILL
        if is_pid_claude(pid):
            try:
                os.kill(pid, signal.SIGKILL)
                kill_signal = "SIGKILL"
                logger.info(f"Kill: escalated to SIGKILL for PID {pid}")
            except ProcessLookupError:
                pass  # Died between check and kill

    else:
        # Phone/remote device - use sshp with SIGINT×2
        try:
            proc = await asyncio.create_subprocess_exec(
                "sshp", f"kill -INT {pid}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await asyncio.wait_for(proc.communicate(), timeout=10)
            kill_signal = "SIGINT"
            logger.info(f"Kill: sent first SIGINT via SSH to PID {pid} on {device_id}")

            # Wait 1s then send second SIGINT
            await asyncio.sleep(1)
            proc1b = await asyncio.create_subprocess_exec(
                "sshp", f"kill -INT {pid}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await asyncio.wait_for(proc1b.communicate(), timeout=10)
            kill_signal = "SIGINT_x2"
            logger.info(f"Kill: sent second SIGINT via SSH to PID {pid} on {device_id}")

            # Wait 3s then check/escalate
            await asyncio.sleep(3)

            proc2 = await asyncio.create_subprocess_exec(
                "sshp", f"kill -0 {pid}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout2, stderr2 = await asyncio.wait_for(proc2.communicate(), timeout=10)
            if proc2.returncode == 0:
                # Still alive, escalate
                proc3 = await asyncio.create_subprocess_exec(
                    "sshp", f"kill -9 {pid}",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                await asyncio.wait_for(proc3.communicate(), timeout=10)
                kill_signal = "SIGKILL"
                logger.info(f"Kill: escalated to SIGKILL via SSH for PID {pid} on {device_id}")
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail=f"SSH to {device_id} timed out")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"SSH kill failed: {str(e)}")

    # Mark stopped in DB
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE claude_instances SET status = 'stopped', stopped_at = ? WHERE id = ?",
            (now, instance_id)
        )
        await db.commit()

    # Log event
    await log_event(
        "instance_killed",
        instance_id=instance_id,
        device_id=device_id,
        details={"pid": pid, "signal": kill_signal}
    )

    logger.info(f"Kill: instance {instance_id[:12]}... killed (PID {pid}, {kill_signal})")
    return {"status": "killed", "pid": pid, "signal": kill_signal}


@app.post("/api/instances/{instance_id}/unstick")
async def unstick_instance(instance_id: str, level: int = 1):
    """Nudge a stuck Claude instance back to life.

    Level 1 (default): SIGWINCH - gentle window resize signal, interrupts blocking I/O
    Level 2: SIGINT - like Ctrl+C, cancels current operation but keeps instance alive
    Level 3: SIGKILL - nuclear option, kills process but preserves terminal for /resume

    Levels 1-2 are non-destructive. Waits 4 seconds and checks if instance activity changed.
    Level 3 kills immediately (use when deadlocked and L1/L2 don't work).
    """
    logger.info(f"Unstick request for instance: {instance_id[:12]}...")

    # Look up instance
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM claude_instances WHERE id = ?",
            (instance_id,)
        )
        row = await cursor.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Instance not found")

    instance = dict(row)
    pid = instance.get("pid")
    device_id = instance.get("device_id", "Mac-Mini")
    working_dir = instance.get("working_dir", "")
    last_activity_before = instance.get("last_activity")

    # PID discovery fallback
    if not pid:
        if is_local_device(device_id):
            pid = await find_claude_pid_by_workdir(working_dir)
            if not pid:
                raise HTTPException(
                    status_code=400,
                    detail="No PID stored and could not discover process."
                )
        else:
            raise HTTPException(
                status_code=400,
                detail=f"No PID stored for remote device '{device_id}'."
            )

    # Choose signal based on level
    if level == 3:
        sig = signal.SIGKILL
        sig_name = "SIGKILL"
        ssh_sig = "KILL"
    elif level == 2:
        sig = signal.SIGINT
        sig_name = "SIGINT"
        ssh_sig = "INT"
    else:
        sig = signal.SIGWINCH
        sig_name = "SIGWINCH"
        ssh_sig = "WINCH"

    # Send the signal
    diag_before = None
    if is_local_device(device_id):
        # If stored PID is stale, try to rediscover by working directory
        if not is_pid_claude(pid):
            logger.info(f"Unstick: stored PID {pid} is stale, attempting rediscovery...")
            new_pid = await find_claude_pid_by_workdir(working_dir)
            if new_pid:
                pid = new_pid
                logger.info(f"Unstick: rediscovered PID {pid} for {working_dir}")
                # Update the stored PID
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("UPDATE claude_instances SET pid = ? WHERE id = ?", (pid, instance_id))
                    await db.commit()
            else:
                raise HTTPException(status_code=400, detail=f"PID {pid} is stale and no Claude process found in {working_dir}")

        # Capture diagnostics BEFORE sending signal
        diag_before = get_process_diagnostics(pid)
        logger.info(f"Unstick L{level} BEFORE: PID {pid} state={diag_before.get('state', '?')} wchan={diag_before.get('wchan', '?')} children={len(diag_before.get('children', []))}")

        try:
            os.kill(pid, sig)
            logger.info(f"Unstick L{level}: sent {sig_name} to PID {pid}")
        except ProcessLookupError:
            raise HTTPException(status_code=400, detail=f"PID {pid} no longer exists")
        except PermissionError:
            raise HTTPException(status_code=500, detail=f"Permission denied sending {sig_name} to PID {pid}")
    else:
        try:
            proc = await asyncio.create_subprocess_exec(
                "sshp", f"kill -{ssh_sig} {pid}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await asyncio.wait_for(proc.communicate(), timeout=10)
            logger.info(f"Unstick L{level}: sent {sig_name} via SSH to PID {pid} on {device_id}")
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail=f"SSH to {device_id} timed out")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"SSH unstick failed: {str(e)}")

    # Wait and check for activity change
    await asyncio.sleep(4)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT last_activity FROM claude_instances WHERE id = ?",
            (instance_id,)
        )
        row = await cursor.fetchone()

    last_activity_after = dict(row).get("last_activity") if row else None
    activity_changed = last_activity_after != last_activity_before

    status = "nudged" if activity_changed else "no_change"

    # Capture diagnostics AFTER signal (desktop only)
    diag_after = None
    if is_local_device(device_id) and is_pid_claude(pid):
        diag_after = get_process_diagnostics(pid)
        logger.info(f"Unstick L{level} AFTER: PID {pid} state={diag_after.get('state', '?')} wchan={diag_after.get('wchan', '?')}")

    await log_event(
        "instance_unstick",
        instance_id=instance_id,
        device_id=device_id,
        details={
            "pid": pid,
            "signal": sig_name,
            "level": level,
            "activity_changed": activity_changed,
            "state_before": diag_before.get("state") if diag_before else None,
            "wchan_before": diag_before.get("wchan") if diag_before else None,
            "state_after": diag_after.get("state") if diag_after else None,
            "wchan_after": diag_after.get("wchan") if diag_after else None,
        }
    )

    logger.info(f"Unstick L{level}: instance {instance_id[:12]}... {status} (PID {pid}, {sig_name}, activity_changed={activity_changed})")

    response = {"status": status, "pid": pid, "signal": sig_name, "level": level, "activity_changed": activity_changed}
    if diag_before:
        response["diagnostics_before"] = {
            "state": diag_before.get("state"),
            "state_desc": diag_before.get("state_desc"),
            "wchan": diag_before.get("wchan"),
            "children": diag_before.get("children", []),
        }
    if diag_after:
        response["diagnostics_after"] = {
            "state": diag_after.get("state"),
            "state_desc": diag_after.get("state_desc"),
            "wchan": diag_after.get("wchan"),
        }
    return response


def get_process_diagnostics(pid: int) -> dict:
    """Get detailed diagnostics for a process. Returns dict with process info or error."""
    diag = {"pid": pid, "exists": False}

    try:
        # Check if process exists
        proc_dir = f"/proc/{pid}"
        if not os.path.exists(proc_dir):
            diag["error"] = "Process does not exist"
            return diag

        diag["exists"] = True

        # Get comm (process name)
        try:
            with open(f"{proc_dir}/comm", "r") as f:
                diag["comm"] = f.read().strip()
        except Exception as e:
            diag["comm_error"] = str(e)

        # Get cmdline
        try:
            with open(f"{proc_dir}/cmdline", "r") as f:
                cmdline = f.read().replace('\x00', ' ').strip()
                diag["cmdline"] = cmdline[:200] if cmdline else "(empty)"
        except Exception as e:
            diag["cmdline_error"] = str(e)

        # Get cwd
        try:
            diag["cwd"] = os.readlink(f"{proc_dir}/cwd")
        except Exception as e:
            diag["cwd_error"] = str(e)

        # Get process state from stat
        try:
            with open(f"{proc_dir}/stat", "r") as f:
                stat = f.read().split()
                # State is field 3 (0-indexed 2)
                state_char = stat[2] if len(stat) > 2 else "?"
                state_map = {
                    "R": "Running",
                    "S": "Sleeping (interruptible)",
                    "D": "Disk sleep (uninterruptible)",
                    "Z": "Zombie",
                    "T": "Stopped",
                    "t": "Tracing stop",
                    "X": "Dead",
                    "I": "Idle",
                }
                diag["state"] = state_char
                diag["state_desc"] = state_map.get(state_char, "Unknown")
                # PPID is field 4 (0-indexed 3)
                diag["ppid"] = int(stat[3]) if len(stat) > 3 else None
        except Exception as e:
            diag["stat_error"] = str(e)

        # Get file descriptors (especially stdin/stdout/stderr)
        try:
            fd_dir = f"{proc_dir}/fd"
            fds = {}
            for fd in ["0", "1", "2"]:  # stdin, stdout, stderr
                fd_path = f"{fd_dir}/{fd}"
                if os.path.exists(fd_path):
                    try:
                        target = os.readlink(fd_path)
                        fds[fd] = target
                    except Exception:
                        fds[fd] = "(unreadable)"
            diag["fds"] = fds
        except Exception as e:
            diag["fd_error"] = str(e)

        # Get wchan (what syscall it's waiting in)
        try:
            with open(f"{proc_dir}/wchan", "r") as f:
                wchan = f.read().strip()
                diag["wchan"] = wchan if wchan and wchan != "0" else "(not waiting)"
        except Exception as e:
            diag["wchan_error"] = str(e)

        # Check for child processes
        try:
            children = []
            for entry in os.listdir("/proc"):
                if not entry.isdigit():
                    continue
                try:
                    with open(f"/proc/{entry}/stat", "r") as f:
                        child_stat = f.read().split()
                        if len(child_stat) > 3 and int(child_stat[3]) == pid:
                            child_comm = "(unknown)"
                            try:
                                with open(f"/proc/{entry}/comm", "r") as cf:
                                    child_comm = cf.read().strip()
                            except Exception:
                                pass
                            children.append({"pid": int(entry), "comm": child_comm})
                except Exception:
                    continue
            diag["children"] = children
        except Exception as e:
            diag["children_error"] = str(e)

    except Exception as e:
        diag["error"] = str(e)

    return diag


@app.get("/api/instances/{instance_id}/diagnose")
async def diagnose_instance(instance_id: str):
    """Get detailed diagnostics for an instance's process state.

    Useful for debugging stuck instances. Returns process state,
    what syscall it's waiting on, child processes, file descriptors, etc.
    """
    # Look up instance
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM claude_instances WHERE id = ?",
            (instance_id,)
        )
        row = await cursor.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Instance not found")

    instance = dict(row)
    stored_pid = instance.get("pid")
    device_id = instance.get("device_id", "Mac-Mini")
    working_dir = instance.get("working_dir", "")
    last_activity = instance.get("last_activity")
    status = instance.get("status")

    result = {
        "instance_id": instance_id,
        "device_id": device_id,
        "working_dir": working_dir,
        "db_status": status,
        "last_activity": last_activity,
        "stored_pid": stored_pid,
    }

    # Calculate time since last activity
    if last_activity:
        try:
            from datetime import datetime
            # Parse the timestamp (assuming it's in local time from SQLite)
            last_dt = datetime.fromisoformat(last_activity.replace("Z", "+00:00")) if "T" in last_activity else datetime.strptime(last_activity, "%Y-%m-%d %H:%M:%S")
            age_seconds = (datetime.now() - last_dt).total_seconds()
            result["activity_age_seconds"] = int(age_seconds)
            result["activity_age_human"] = f"{int(age_seconds // 60)}m {int(age_seconds % 60)}s ago"
        except Exception as e:
            result["activity_age_error"] = str(e)

    if not is_local_device(device_id):
        result["note"] = "Detailed diagnostics only available for desktop instances"
        return result

    # Check stored PID
    if stored_pid:
        result["stored_pid_diagnostics"] = get_process_diagnostics(stored_pid)
        result["stored_pid_is_claude"] = is_pid_claude(stored_pid)

    # Try to discover current PID by working dir
    discovered_pid = await find_claude_pid_by_workdir(working_dir)
    result["discovered_pid"] = discovered_pid

    if discovered_pid and discovered_pid != stored_pid:
        result["pid_mismatch"] = True
        result["discovered_pid_diagnostics"] = get_process_diagnostics(discovered_pid)

    # Check if there are ANY claude processes
    try:
        claude_processes = []
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/comm", "r") as f:
                    if f.read().strip() == "claude":
                        pid = int(entry)
                        try:
                            cwd = os.readlink(f"/proc/{entry}/cwd")
                        except Exception:
                            cwd = "(unknown)"
                        claude_processes.append({"pid": pid, "cwd": cwd})
            except Exception:
                continue
        result["all_claude_processes"] = claude_processes
    except Exception as e:
        result["claude_scan_error"] = str(e)

    # Log the diagnosis
    logger.info(f"Diagnose: instance {instance_id[:12]}... stored_pid={stored_pid}, discovered_pid={discovered_pid}, status={status}")

    return result


class RenameInstanceRequest(BaseModel):
    tab_name: str


class LogEntry(BaseModel):
    """Single log entry."""
    timestamp: str
    level: str
    message: str


class LogsResponse(BaseModel):
    """Response for recent logs."""
    logs: List[LogEntry]
    count: int


@app.patch("/api/instances/{instance_id}/rename")
async def rename_instance(instance_id: str, request: RenameInstanceRequest):
    """Rename an instance's tab_name."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id, tab_name FROM claude_instances WHERE id = ?",
            (instance_id,)
        )
        row = await cursor.fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="Instance not found")

        old_name = row[1]
        await db.execute(
            "UPDATE claude_instances SET tab_name = ? WHERE id = ?",
            (request.tab_name, instance_id)
        )
        await db.commit()

    # Log event
    await log_event(
        "instance_renamed",
        instance_id=instance_id,
        details={"old_name": old_name, "new_name": request.tab_name}
    )

    return {"status": "renamed", "instance_id": instance_id, "tab_name": request.tab_name}


class VoiceChangeRequest(BaseModel):
    voice: str


@app.get("/api/voices")
async def list_voices():
    """List all available TTS voices from the profile pool."""
    voices = []
    for profile in PROFILES:
        voice = profile["tts_voice"]
        # Extract short name: "Microsoft George" -> "George"
        short_name = voice.replace("Microsoft ", "")
        voices.append({
            "voice": voice,
            "short_name": short_name,
            "profile_name": profile["name"]
        })
    return {"voices": voices}


def find_voice_linear_probe(used_voices: set) -> str | None:
    """Find an available voice using random offset + linear probe.

    Picks a random starting index in PROFILES, then iterates circularly
    until finding a voice not in used_voices. Returns None if all are used.
    """
    n = len(PROFILES)
    if n == 0:
        return None

    start = random.randint(0, n - 1)
    for i in range(n):
        idx = (start + i) % n
        voice = PROFILES[idx]["tts_voice"]
        if voice not in used_voices:
            return voice
    return None


@app.patch("/api/instances/{instance_id}/voice")
async def change_instance_voice(instance_id: str, request: VoiceChangeRequest):
    """Change an instance's TTS voice with collision handling.

    If the target voice is already in use by another instance, that instance
    gets bumped using random offset + linear probe to find an open slot.
    No cascade - bumped instance just finds the next available voice.
    """
    all_voices = {p["tts_voice"] for p in PROFILES}
    if request.voice not in all_voices:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid voice. Available: {', '.join(sorted(all_voices))}"
        )

    async with aiosqlite.connect(DB_PATH) as db:
        # Get all instances and their voices
        cursor = await db.execute("SELECT id, tts_voice, tab_name FROM claude_instances")
        rows = await cursor.fetchall()

        instance_to_voice = {row[0]: row[1] for row in rows}
        instance_to_name = {row[0]: row[2] for row in rows}
        voice_to_instance = {row[1]: row[0] for row in rows if row[1]}

        if instance_id not in instance_to_voice:
            raise HTTPException(status_code=404, detail="Instance not found")

        original_voice = instance_to_voice[instance_id]
        if original_voice == request.voice:
            return {"status": "no_change", "instance_id": instance_id, "voice": request.voice}

        # Changes to apply: [(instance_id, old_voice, new_voice), ...]
        changes = [(instance_id, original_voice, request.voice)]

        # Check for collision
        holder = voice_to_instance.get(request.voice)
        if holder and holder != instance_id:
            # Collision! Bump the holder to a new voice
            holder_old_voice = instance_to_voice[holder]

            # Build set of voices that will be in use after our change
            # (exclude original_voice since we're freeing it, include request.voice since we're taking it)
            used_after = set(voice_to_instance.keys())
            used_after.discard(original_voice)  # We're freeing this
            used_after.add(request.voice)  # We're taking this

            # Find new voice for bumped instance via linear probe
            new_voice_for_holder = find_voice_linear_probe(used_after)
            if not new_voice_for_holder:
                # All voices in use, give them the voice we just freed
                new_voice_for_holder = original_voice

            changes.append((holder, holder_old_voice, new_voice_for_holder))

        # Apply all changes to database
        for iid, _, new_voice in changes:
            await db.execute(
                "UPDATE claude_instances SET tts_voice = ? WHERE id = ?",
                (new_voice, iid)
            )
        await db.commit()

    # Log events for each change
    for iid, old_v, new_v in changes:
        name = instance_to_name.get(iid, iid[:8])
        await log_event(
            "instance_voice_changed",
            instance_id=iid,
            details={"old_voice": old_v, "new_voice": new_v, "bumped": iid != instance_id}
        )

    # Build response
    bumps = [
        {"instance_id": iid, "name": instance_to_name.get(iid, iid[:8]), "old": old_v, "new": new_v}
        for iid, old_v, new_v in changes
    ]

    return {
        "status": "voice_changed",
        "instance_id": instance_id,
        "voice": request.voice,
        "changes": bumps
    }


@app.post("/api/instances/{instance_id}/activity")
async def update_instance_activity(instance_id: str, request: ActivityRequest):
    """Update instance processing state. Called by hooks on prompt_submit and stop."""
    now = datetime.now().isoformat()

    if request.action == "prompt_submit":
        new_status = "processing"
        logger.info(f"Activity: {instance_id[:8]}... prompt submitted")
    elif request.action == "stop":
        new_status = "idle"
    else:
        raise HTTPException(status_code=400, detail=f"Unknown action: {request.action}")

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id FROM claude_instances WHERE id = ?",
            (instance_id,)
        )
        row = await cursor.fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="Instance not found")

        await db.execute(
            "UPDATE claude_instances SET status = ?, last_activity = ? WHERE id = ?",
            (new_status, now, instance_id)
        )
        await db.commit()

    return {
        "status": "updated",
        "instance_id": instance_id,
        "action": request.action,
        "new_status": new_status
    }


@app.get("/api/instances/{instance_id}/todos")
async def get_instance_todos(instance_id: str):
    """Get the task list for an instance from ~/.claude/tasks/{instance_id}/."""
    tasks_dir = Path.home() / ".claude" / "tasks" / instance_id

    if not tasks_dir.exists():
        return {"todos": [], "progress": 0, "current_task": None, "total": 0, "completed": 0}

    try:
        todos = []
        for task_file in tasks_dir.glob("*.json"):
            with open(task_file) as f:
                task = json.load(f)
                todos.append(task)

        if not todos:
            return {"todos": [], "progress": 0, "current_task": None, "total": 0, "completed": 0}

        # Sort by ID (numeric)
        todos.sort(key=lambda t: int(t.get("id", 0)))

        completed = sum(1 for t in todos if t.get("status") == "completed")
        total = len(todos)
        progress = int((completed / total) * 100) if total > 0 else 0

        current_task = None
        for t in todos:
            if t.get("status") == "in_progress":
                current_task = t.get("activeForm") or t.get("subject")
                break

        return {
            "todos": todos,
            "progress": progress,
            "completed": completed,
            "total": total,
            "current_task": current_task
        }
    except Exception as e:
        return {"todos": [], "progress": 0, "current_task": None, "total": 0, "completed": 0, "error": str(e)}


@app.get("/api/instances", response_model=List[dict])
async def list_instances(status: Optional[str] = None):
    """List all instances, optionally filtered by status."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        if status:
            cursor = await db.execute(
                "SELECT * FROM claude_instances WHERE status = ? ORDER BY registered_at DESC",
                (status,)
            )
        else:
            cursor = await db.execute(
                "SELECT * FROM claude_instances ORDER BY registered_at DESC"
            )

        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


@app.get("/api/instances/{instance_id}", response_model=dict)
async def get_instance(instance_id: str):
    """Get details of a specific instance."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM claude_instances WHERE id = ?",
            (instance_id,)
        )
        row = await cursor.fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="Instance not found")

        return dict(row)


# Dashboard Endpoint
@app.get("/api/dashboard", response_model=DashboardResponse)
async def get_dashboard():
    """Get dashboard data including instances, productivity status, and events."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Get all instances
        cursor = await db.execute(
            "SELECT * FROM claude_instances ORDER BY status ASC, registered_at DESC"
        )
        instances = [dict(row) for row in await cursor.fetchall()]

        # Check productivity (any active instances = productive)
        active_count = sum(1 for i in instances if i["status"] in ("processing", "idle"))
        productivity_active = active_count > 0

        # Get recent events (last 20)
        cursor = await db.execute(
            "SELECT * FROM events ORDER BY created_at DESC LIMIT 20"
        )
        events = []
        for row in await cursor.fetchall():
            event = dict(row)
            if event.get("details"):
                try:
                    event["details"] = json.loads(event["details"])
                except:
                    pass
            events.append(event)

        return DashboardResponse(
            instances=instances,
            productivity_active=productivity_active,
            recent_events=events,
            tts_queue=get_tts_queue_status()
        )


class LogEventRequest(BaseModel):
    event_type: str
    instance_id: Optional[str] = None
    details: Optional[dict] = None


# Distraction apps that require productivity to be allowed
DISTRACTION_APPS = [
    "brave.exe",  # Browser (when showing YouTube)
    # Add more as needed
]

# Window titles that indicate distraction content
DISTRACTION_PATTERNS = [
    "YouTube",
    "Netflix",
    "Twitch",
    "Twitter",
    "Reddit",
]

# Obsidian Timer Integration Config
OBSIDIAN_CONFIG = {
    "vault_name": "Token-ENV",
    # Map detected modes to Obsidian timer commands
    "mode_commands": {
        "silence": "timer-auto-work-silence",
        "music": "timer-auto-work-music",
        "video": "timer-auto-work-video",
        "gaming": "timer-auto-work-gaming",
        # Gym modes (triggered via geofence/MacroDroid)
        "gym": "timer-auto-gym",
        "work_gym": "timer-auto-work-gym",
    },
}

# Desktop mode state (tracks current mode from AHK detection)
DESKTOP_STATE = {
    "current_mode": "silence",
    "last_detection": None,
    # Work mode: "clocked_in" (normal enforcement), "clocked_out" (no enforcement), "gym" (gym timer)
    "work_mode": "clocked_in",
    "work_mode_changed_at": None,
}

# Phone HTTP server config (MacroDroid on phone via Tailscale)
PHONE_CONFIG = {
    "host": "100.102.92.24",
    "port": 7777,
    "timeout": 5,
    # === TEST SHIM - REMOVE AFTER TESTING ===
    # Set to True to bypass break time check and force blocking
    "test_force_block": False,
    # =========================================
}

# Phone activity state (tracks current app from MacroDroid)
PHONE_STATE = {
    "current_app": None,  # Current distraction app or None
    "last_activity": None,
    "is_distracted": False,
    "reachable": None,  # Last known reachability status
    "last_reachable_check": None,
}

# App categories for phone distraction detection
PHONE_DISTRACTION_APPS = {
    # Twitter/X
    "twitter": "video",
    "x": "video",
    "com.twitter.android": "video",
    # YouTube
    "youtube": "video",
    "com.google.android.youtube": "video",
    # Games - add specific games here
    "game": "gaming",
    "minecraft": "gaming",
    "com.mojang.minecraftpe": "gaming",
}

# ============ Timer State ============
# Timer is now built into Token API itself; no external file dependency.

def get_obsidian_timer_state() -> dict:
    """Return timer state stub — timer is integrated into Token API now."""
    return {"breakAvailableSeconds": 0, "isInBacklog": False}


# ============ Audio Proxy State ============
# Tracks phone audio proxy status for routing phone audio through PC

AUDIO_PROXY_STATE = {
    "phone_connected": False,
    "receiver_running": False,
    "receiver_pid": None,
    "last_connect_time": None,
    "last_disconnect_time": None,
}

# ============ Headless Mode (disabled on macOS) ============

def get_headless_state() -> dict:
    """Headless mode is not applicable on macOS."""
    return {"enabled": False, "last_changed": None, "hostname": None, "error": "not applicable on macOS"}


async def poll_for_state_change(
    get_state_fn: callable,
    key: str,
    original_value: any,
    timeout: float = 5.0,
    initial_interval: float = 0.1,
    max_interval: float = 0.5,
) -> tuple[bool, dict]:
    """
    Poll a state function until a key's value changes or timeout.

    Uses exponential backoff starting at initial_interval, capped at max_interval.
    This is a generic utility for waiting on async external operations that update
    state files (e.g., Windows scheduled tasks).

    Args:
        get_state_fn: Function that returns current state dict
        key: Key to monitor for changes
        original_value: Original value to compare against
        timeout: Max seconds to wait
        initial_interval: Initial poll interval in seconds
        max_interval: Maximum poll interval in seconds

    Returns:
        (changed: bool, final_state: dict)
    """
    elapsed = 0.0
    interval = initial_interval

    while elapsed < timeout:
        await asyncio.sleep(interval)
        elapsed += interval

        state = get_state_fn()
        if state.get(key) != original_value:
            return True, state

        # Exponential backoff
        interval = min(interval * 1.5, max_interval)

    # Timeout - return final state anyway
    return False, get_state_fn()


def trigger_headless_task(action: str = "toggle") -> tuple[bool, str]:
    """Headless mode is not applicable on macOS."""
    return False, "Headless mode not available on macOS"


def start_audio_receiver() -> dict:
    """Audio proxy is not available on macOS."""
    return {"success": False, "error": "not available on macOS"}


def stop_audio_receiver() -> dict:
    """Audio proxy is not available on macOS."""
    return {"success": True, "stopped_count": 0}


def check_audio_receiver_running() -> dict:
    """Audio proxy is not available on macOS."""
    return {"running": False, "pid": None}


def close_distraction_windows() -> dict:
    """Window enforcement is not available on macOS (no desktop browser to manage)."""
    logger.info("ENFORCE: close_distraction_windows not applicable on macOS")
    return {"success": True, "closed_count": 0}


def trigger_obsidian_command_async(command_id: str, no_focus: bool = False):
    """Fire-and-forget Obsidian trigger (log-only on macOS)."""
    trigger_obsidian_command(command_id, no_focus)


def trigger_obsidian_command(command_id: str, no_focus: bool = False) -> bool:
    """Log Obsidian command (Obsidian is just a log sink now, not a runtime dependency)."""
    logger.info(f"OBSIDIAN: command '{command_id}' (log-only, no_focus={no_focus})")
    return True


def enforce_phone_app(app_name: str, action: str = "disable") -> dict:
    """
    Send enforcement command to phone via MacroDroid HTTP server.

    Args:
        app_name: App to enable/disable (twitter, youtube, etc.)
        action: "disable" or "enable"

    Returns:
        dict with success status and details
    """
    host = PHONE_CONFIG["host"]
    port = PHONE_CONFIG["port"]
    timeout = PHONE_CONFIG["timeout"]

    url = f"http://{host}:{port}/enforce"
    params = {"action": action, "app": app_name}

    try:
        response = requests.get(url, params=params, timeout=timeout)
        PHONE_STATE["reachable"] = True
        PHONE_STATE["last_reachable_check"] = datetime.now().isoformat()

        print(f"PHONE: Enforce {action} {app_name} -> {response.status_code}")
        return {
            "success": response.status_code == 200,
            "status_code": response.status_code,
            "response": response.text[:200] if response.text else None
        }
    except requests.exceptions.Timeout:
        PHONE_STATE["reachable"] = False
        PHONE_STATE["last_reachable_check"] = datetime.now().isoformat()
        print(f"PHONE: Timeout enforcing {action} {app_name}")
        return {"success": False, "error": "timeout"}
    except requests.exceptions.ConnectionError:
        PHONE_STATE["reachable"] = False
        PHONE_STATE["last_reachable_check"] = datetime.now().isoformat()
        print(f"PHONE: Connection refused enforcing {action} {app_name}")
        return {"success": False, "error": "connection_refused"}
    except Exception as e:
        PHONE_STATE["reachable"] = False
        PHONE_STATE["last_reachable_check"] = datetime.now().isoformat()
        print(f"PHONE: Error enforcing {action} {app_name}: {e}")
        return {"success": False, "error": str(e)}


def check_phone_reachable() -> dict:
    """
    Check if phone is reachable via heartbeat endpoint.

    Returns:
        dict with reachable status
    """
    host = PHONE_CONFIG["host"]
    port = PHONE_CONFIG["port"]
    timeout = PHONE_CONFIG["timeout"]

    url = f"http://{host}:{port}/heartbeat"

    try:
        response = requests.get(url, timeout=timeout)
        PHONE_STATE["reachable"] = True
        PHONE_STATE["last_reachable_check"] = datetime.now().isoformat()
        return {"reachable": True, "status_code": response.status_code}
    except Exception:
        PHONE_STATE["reachable"] = False
        PHONE_STATE["last_reachable_check"] = datetime.now().isoformat()
        return {"reachable": False}


@app.post("/api/window/enforce", response_model=WindowEnforceResponse)
async def check_window_enforcement(request: WindowCheckRequest = None):
    """
    Check if distraction windows should be closed based on productivity status.

    This is the authoritative endpoint for AHK to determine whether to close
    distraction windows (like YouTube in Brave).

    Logic:
    - If at least one Claude instance is active -> productivity is active
    - If productivity is active -> distractions are allowed (earned break)
    - If productivity is NOT active -> distractions should be closed
    """
    async with aiosqlite.connect(DB_PATH) as db:
        # Count active Claude instances
        cursor = await db.execute(
            "SELECT COUNT(*) FROM claude_instances WHERE status IN ('processing', 'idle')"
        )
        row = await cursor.fetchone()
        active_count = row[0] if row else 0

    productivity_active = active_count > 0
    should_close = not productivity_active

    if productivity_active:
        reason = f"productivity_active:{active_count}_instances"
    else:
        reason = "no_productive_activity"

    # Log the enforcement check
    await log_event(
        "window_enforce_check",
        details={
            "productivity_active": productivity_active,
            "active_instances": active_count,
            "should_close": should_close,
            "source": request.source if request else "unknown",
            "window_title": request.window_title if request else None
        }
    )

    return WindowEnforceResponse(
        productivity_active=productivity_active,
        active_instance_count=active_count,
        should_close_distractions=should_close,
        distraction_apps=DISTRACTION_APPS,
        reason=reason
    )


@app.get("/api/window/enforce", response_model=WindowEnforceResponse)
async def check_window_enforcement_get():
    """GET version of window enforcement check (simpler for AHK to call)."""
    return await check_window_enforcement(None)


@app.post("/api/window/close")
async def trigger_window_close():
    """
    Manually trigger closing of distraction windows.
    This is a push-based enforcement that token-api executes directly.
    """
    result = close_distraction_windows()

    await log_event(
        "manual_enforcement",
        details={"result": result}
    )

    return {
        "action": "close_distractions",
        "result": result
    }


@app.post("/desktop", response_model=DesktopDetectionResponse)
async def handle_desktop_detection(request: DesktopDetectionRequest):
    """
    Handle desktop detection events from AHK.
    This is the authoritative endpoint for mode changes (migrated from mesh-pipe).

    AHK detects: video/music/gaming/silence
    token-api: decides if mode change is allowed, triggers Obsidian timer command

    Logic:
    - If work_mode is "clocked_out" -> all modes allowed, no enforcement
    - If work_mode is "gym" -> gym timer mode, all modes allowed
    - Video mode (distraction) requires productivity to be active when clocked_in
    - Other modes (music, gaming, silence) are always allowed
    """
    detected_mode = request.detected_mode.lower()
    window_title = request.window_title or ""
    source = request.source

    # Validate detected mode
    valid_modes = list(OBSIDIAN_CONFIG["mode_commands"].keys())
    if detected_mode not in valid_modes:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid detected_mode '{detected_mode}'. Valid: {valid_modes}"
        )

    work_mode = DESKTOP_STATE.get("work_mode", "clocked_in")
    print(f">>> Desktop detection from {source}: mode={detected_mode} window='{window_title}' work_mode={work_mode}")

    # Get current mode
    current_mode = DESKTOP_STATE["current_mode"]

    # Check if mode change is needed
    if detected_mode == current_mode:
        print(f"    Mode unchanged ({detected_mode}), skipping")
        return DesktopDetectionResponse(
            action="none",
            detected_mode=detected_mode,
            reason="mode_unchanged",
            productivity_active=True,  # Not relevant for unchanged
            active_instance_count=0,
            obsidian_triggered=False
        )

    # Check productivity status
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM claude_instances WHERE status IN ('processing', 'idle')"
        )
        row = await cursor.fetchone()
        active_count = row[0] if row else 0

    productivity_active = active_count > 0

    # Determine if mode change is allowed
    allowed = True
    reason = "allowed"

    # CLOCKED OUT: All modes allowed, no enforcement
    if work_mode == "clocked_out":
        allowed = True
        reason = "clocked_out"
        print(f"    Clocked out - all modes allowed")
    # GYM MODE: All modes allowed (gym has its own timer logic)
    elif work_mode == "gym":
        allowed = True
        reason = "gym_mode"
        print(f"    Gym mode - all modes allowed")
    # CLOCKED IN: Video/gaming mode requires either break time OR productivity
    elif detected_mode == "video" or detected_mode == "gaming":
        timer_state = get_obsidian_timer_state()
        has_break_time = timer_state.get("breakAvailableSeconds", 0) > 0

        if has_break_time:
            # User has earned break time - allow video/gaming, Obsidian will consume it
            allowed = True
            reason = "break_time_available"
            print(f"    {detected_mode.title()} allowed: {timer_state.get('breakAvailableSeconds', 0)}s break available")
        elif productivity_active:
            # No break time but actively working - video drains break (penalty)
            allowed = True
            reason = "productivity_active"
            print(f"    {detected_mode.title()} allowed: productivity active (penalty mode)")
        else:
            # No break time AND no productivity - block
            allowed = False
            reason = "no_productivity_no_break"
            print(f"    {detected_mode.title()} blocked: no break time, no productivity")

    if allowed:
        # Update state
        old_mode = DESKTOP_STATE["current_mode"]
        DESKTOP_STATE["current_mode"] = detected_mode
        DESKTOP_STATE["last_detection"] = datetime.now().isoformat()

        # Trigger Obsidian command
        command_id = OBSIDIAN_CONFIG["mode_commands"][detected_mode]
        obsidian_triggered = trigger_obsidian_command(command_id)

        # Log event
        await log_event(
            "desktop_mode_change",
            details={
                "old_mode": old_mode,
                "new_mode": detected_mode,
                "window_title": window_title,
                "source": source,
                "obsidian_triggered": obsidian_triggered,
                "productivity_active": productivity_active,
                "active_instances": active_count
            }
        )

        print(f"<<< Mode changed: {old_mode} -> {detected_mode} | obsidian={obsidian_triggered}")

        return DesktopDetectionResponse(
            action="mode_changed",
            detected_mode=detected_mode,
            old_mode=old_mode,
            new_mode=detected_mode,
            reason="allowed",
            obsidian_triggered=obsidian_triggered,
            productivity_active=productivity_active,
            active_instance_count=active_count
        )
    else:
        # Mode change blocked - immediately enforce by closing distraction windows
        print(f"<<< Mode change BLOCKED: {detected_mode} | reason={reason}")

        # Close distraction windows immediately (push-based enforcement)
        enforce_result = close_distraction_windows()

        await log_event(
            "desktop_mode_blocked",
            details={
                "detected_mode": detected_mode,
                "reason": reason,
                "window_title": window_title,
                "source": source,
                "productivity_active": productivity_active,
                "active_instances": active_count,
                "enforcement": enforce_result
            }
        )

        # Return 403 to indicate blocked
        raise HTTPException(
            status_code=403,
            detail=DesktopDetectionResponse(
                action="blocked",
                detected_mode=detected_mode,
                reason=reason,
                obsidian_triggered=False,
                productivity_active=productivity_active,
                active_instance_count=active_count
            ).model_dump()
        )


# ============ Phone Activity Detection ============
# MacroDroid sends app open/close events from phone

@app.post("/phone", response_model=PhoneActivityResponse)
async def handle_phone_activity(request: PhoneActivityRequest):
    """
    Handle phone app activity from MacroDroid.

    Called when distraction apps (Twitter, YouTube, games) are opened/closed.
    Returns whether the app is allowed based on break time or productivity.

    Unlike desktop, we don't force-close apps - just return allowed/blocked
    for MacroDroid to handle (show notification, etc).
    """
    app_name = request.app.lower()
    action = request.action.lower()
    package = request.package

    print(f">>> Phone activity: app={app_name} action={action} package={package}")

    # Handle app close
    if action == "close":
        old_app = PHONE_STATE.get("current_app")
        PHONE_STATE["current_app"] = None
        PHONE_STATE["is_distracted"] = False
        PHONE_STATE["last_activity"] = datetime.now().isoformat()

        # Switch Obsidian timer to silence when distraction app closes (async to not block response)
        obsidian_triggered = False
        if old_app:  # Only trigger if we were tracking an app
            DESKTOP_STATE["current_mode"] = "silence"
            DESKTOP_STATE["last_detection"] = datetime.now().isoformat()
            trigger_obsidian_command_async(
                OBSIDIAN_CONFIG["mode_commands"]["silence"],
                no_focus=True
            )
            obsidian_triggered = True  # Dispatched (async)
            print(f"    Phone close -> silence | obsidian=dispatched")

        await log_event(
            "phone_app_closed",
            details={
                "app": app_name,
                "package": package,
                "obsidian_triggered": obsidian_triggered
            }
        )

        return PhoneActivityResponse(
            allowed=True,
            reason="closed",
            message="App closed"
        )

    # Determine distraction category
    distraction_mode = PHONE_DISTRACTION_APPS.get(app_name)
    if not distraction_mode and package:
        distraction_mode = PHONE_DISTRACTION_APPS.get(package)

    # If not a known distraction app, allow it
    if not distraction_mode:
        print(f"    Unknown app, allowing: {app_name}")
        return PhoneActivityResponse(
            allowed=True,
            reason="not_tracked",
            message="App not in distraction list"
        )

    # Check work mode
    work_mode = DESKTOP_STATE.get("work_mode", "clocked_in")

    # Clocked out or gym mode = all allowed
    if work_mode in ("clocked_out", "gym"):
        PHONE_STATE["current_app"] = app_name
        PHONE_STATE["is_distracted"] = True
        PHONE_STATE["last_activity"] = datetime.now().isoformat()

        # Sync Obsidian timer to phone distraction mode (async to not block response)
        obsidian_triggered = False
        if distraction_mode in OBSIDIAN_CONFIG["mode_commands"]:
            DESKTOP_STATE["current_mode"] = distraction_mode
            DESKTOP_STATE["last_detection"] = datetime.now().isoformat()
            trigger_obsidian_command_async(
                OBSIDIAN_CONFIG["mode_commands"][distraction_mode],
                no_focus=True
            )
            obsidian_triggered = True  # Dispatched (async)
            print(f"    Phone open -> {distraction_mode} | obsidian=dispatched")

        await log_event(
            "phone_distraction_allowed",
            details={
                "app": app_name,
                "reason": work_mode,
                "obsidian_triggered": obsidian_triggered
            }
        )

        return PhoneActivityResponse(
            allowed=True,
            reason=work_mode,
            message=f"Allowed ({work_mode})"
        )

    # Clocked in - check break time and productivity
    timer_state = get_obsidian_timer_state()
    break_secs = timer_state.get("breakAvailableSeconds", 0)

    # Check productivity (active Claude instances)
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM claude_instances WHERE status IN ('processing', 'idle')"
        )
        row = await cursor.fetchone()
        active_count = row[0] if row else 0

    productivity_active = active_count > 0

    # === TEST SHIM - bypasses break/productivity checks ===
    test_force_block = PHONE_CONFIG.get("test_force_block", False)
    if test_force_block:
        print(f"    TEST MODE: Forcing block (ignoring break={break_secs}s, productivity={productivity_active})")
        break_secs = 0
        productivity_active = False
    # ======================================================

    # Decision logic (same as desktop)
    if break_secs > 0:
        # Has break time - allow
        PHONE_STATE["current_app"] = app_name
        PHONE_STATE["is_distracted"] = True
        PHONE_STATE["last_activity"] = datetime.now().isoformat()

        # Sync Obsidian timer to phone distraction mode (async to not block response)
        obsidian_triggered = False
        if distraction_mode in OBSIDIAN_CONFIG["mode_commands"]:
            DESKTOP_STATE["current_mode"] = distraction_mode
            DESKTOP_STATE["last_detection"] = datetime.now().isoformat()
            trigger_obsidian_command_async(
                OBSIDIAN_CONFIG["mode_commands"][distraction_mode],
                no_focus=True
            )
            obsidian_triggered = True  # Dispatched (async)
            print(f"    Phone open -> {distraction_mode} | obsidian=dispatched")

        await log_event(
            "phone_distraction_allowed",
            details={
                "app": app_name,
                "reason": "break_time",
                "break_seconds": break_secs,
                "obsidian_triggered": obsidian_triggered
            }
        )

        print(f"    Allowed: {break_secs}s break available")
        return PhoneActivityResponse(
            allowed=True,
            reason="break_time_available",
            break_seconds=break_secs,
            message=f"Break time: {break_secs // 60}m {break_secs % 60}s"
        )

    elif productivity_active:
        # No break but productive - allow with penalty
        PHONE_STATE["current_app"] = app_name
        PHONE_STATE["is_distracted"] = True
        PHONE_STATE["last_activity"] = datetime.now().isoformat()

        # Sync Obsidian timer to phone distraction mode (async to not block response)
        obsidian_triggered = False
        if distraction_mode in OBSIDIAN_CONFIG["mode_commands"]:
            DESKTOP_STATE["current_mode"] = distraction_mode
            DESKTOP_STATE["last_detection"] = datetime.now().isoformat()
            trigger_obsidian_command_async(
                OBSIDIAN_CONFIG["mode_commands"][distraction_mode],
                no_focus=True
            )
            obsidian_triggered = True  # Dispatched (async)
            print(f"    Phone open -> {distraction_mode} | obsidian=dispatched")

        await log_event(
            "phone_distraction_allowed",
            details={
                "app": app_name,
                "reason": "productivity_active",
                "active_instances": active_count,
                "obsidian_triggered": obsidian_triggered
            }
        )

        print(f"    Allowed: productivity active ({active_count} instances)")
        return PhoneActivityResponse(
            allowed=True,
            reason="productivity_active",
            break_seconds=0,
            message="Productivity active (penalty mode)"
        )

    else:
        # No break, no productivity - block and enforce
        print(f"    BLOCKED: no break time, no productivity")

        # Send enforcement command to phone to disable the app
        enforce_result = enforce_phone_app(app_name, action="disable")

        await log_event(
            "phone_distraction_blocked",
            details={
                "app": app_name,
                "reason": "no_break_no_productivity",
                "enforcement": enforce_result
            }
        )

        return PhoneActivityResponse(
            allowed=False,
            reason="blocked",
            break_seconds=0,
            message="No break time or productivity"
        )


@app.get("/phone")
async def get_phone_state():
    """Get current phone activity state."""
    timer_state = get_obsidian_timer_state()
    return {
        "current_app": PHONE_STATE.get("current_app"),
        "is_distracted": PHONE_STATE.get("is_distracted", False),
        "last_activity": PHONE_STATE.get("last_activity"),
        "break_seconds": timer_state.get("breakAvailableSeconds", 0),
        "work_mode": DESKTOP_STATE.get("work_mode", "clocked_in"),
        "reachable": PHONE_STATE.get("reachable"),
        "last_reachable_check": PHONE_STATE.get("last_reachable_check"),
    }


@app.get("/phone/ping")
async def ping_phone():
    """Check if phone is reachable."""
    result = check_phone_reachable()
    return result


@app.post("/phone/enforce")
async def manual_enforce_phone(app: str, action: str = "disable"):
    """Manually trigger phone enforcement (for testing)."""
    result = enforce_phone_app(app, action)
    return result


@app.post("/api/timer/break-exhausted")
async def handle_break_exhausted():
    """
    Called by Obsidian when break time hits 0.
    Instantly enforces phone app closure if a distraction app is active.
    This replaces the 5-second polling approach with push-based enforcement.
    """
    current_app = PHONE_STATE.get("current_app")
    if not current_app:
        return {"enforced": False, "reason": "no_active_app"}

    # Map app name to enforcement target
    enforce_app = current_app
    if current_app in ("x", "twitter", "com.twitter.android"):
        enforce_app = "twitter"
    elif current_app in ("youtube", "com.google.android.youtube", "app.revanced.android.youtube"):
        enforce_app = "youtube"
    elif current_app in PHONE_DISTRACTION_APPS:
        mode = PHONE_DISTRACTION_APPS.get(current_app)
        if mode == "gaming":
            enforce_app = "game"

    print(f"BREAK-EXHAUSTED: Enforcing disable on {current_app} (mapped to {enforce_app})")
    result = enforce_phone_app(enforce_app, action="disable")

    # Clear phone state since we're enforcing closure
    PHONE_STATE["current_app"] = None
    PHONE_STATE["is_distracted"] = False

    await log_event(
        "break_exhausted_enforcement",
        details={
            "app": current_app,
            "enforce_app": enforce_app,
            "result": result
        }
    )

    return {"enforced": True, "app": current_app, "result": result}


# ============ Work Mode / Geofence Endpoints ============
# MacroDroid uses geofence to send work mode changes

class WorkModeRequest(BaseModel):
    mode: str = Field(..., description="Work mode: clocked_in, clocked_out, gym")
    source: str = Field(default="api", description="Source of the request (macrodroid, manual, etc)")
    token: Optional[str] = Field(default=None, description="Optional auth token for MacroDroid")


@app.get("/api/work-mode")
async def get_work_mode():
    """Get current work mode status."""
    return {
        "work_mode": DESKTOP_STATE.get("work_mode", "clocked_in"),
        "work_mode_changed_at": DESKTOP_STATE.get("work_mode_changed_at"),
        "current_timer_mode": DESKTOP_STATE.get("current_mode", "silence"),
    }


@app.post("/api/work-mode")
async def set_work_mode(request: WorkModeRequest):
    """
    Set work mode. Called by MacroDroid geofence or manual toggle.

    Modes:
    - clocked_in: Normal enforcement (video requires productivity)
    - clocked_out: No enforcement, all modes allowed
    - gym: Gym timer mode, triggers gym timer in Obsidian

    MacroDroid can send:
    - POST /api/work-mode {"mode": "clocked_in", "source": "macrodroid"}
    - POST /api/work-mode {"mode": "gym", "source": "macrodroid"}
    """
    valid_modes = ["clocked_in", "clocked_out", "gym"]
    if request.mode not in valid_modes:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid work mode '{request.mode}'. Valid: {valid_modes}"
        )

    old_mode = DESKTOP_STATE.get("work_mode", "clocked_in")
    DESKTOP_STATE["work_mode"] = request.mode
    DESKTOP_STATE["work_mode_changed_at"] = datetime.now().isoformat()

    print(f">>> Work mode changed: {old_mode} -> {request.mode} (source: {request.source})")

    # If switching to gym mode, trigger the gym timer in Obsidian
    obsidian_triggered = False
    if request.mode == "gym":
        obsidian_triggered = trigger_obsidian_command(OBSIDIAN_CONFIG["mode_commands"]["gym"])

    await log_event(
        "work_mode_change",
        details={
            "old_mode": old_mode,
            "new_mode": request.mode,
            "source": request.source,
            "obsidian_triggered": obsidian_triggered,
        }
    )

    return {
        "status": "success",
        "old_mode": old_mode,
        "new_mode": request.mode,
        "obsidian_triggered": obsidian_triggered,
    }


@app.post("/api/clock-out")
async def clock_out():
    """Quick endpoint to clock out (disable enforcement)."""
    DESKTOP_STATE["work_mode"] = "clocked_out"
    DESKTOP_STATE["work_mode_changed_at"] = datetime.now().isoformat()
    await log_event("work_mode_change", details={"new_mode": "clocked_out", "source": "quick_api"})
    return {"status": "clocked_out", "message": "Enforcement disabled"}


@app.post("/api/clock-in")
async def clock_in():
    """Quick endpoint to clock in (enable enforcement)."""
    DESKTOP_STATE["work_mode"] = "clocked_in"
    DESKTOP_STATE["work_mode_changed_at"] = datetime.now().isoformat()
    await log_event("work_mode_change", details={"new_mode": "clocked_in", "source": "quick_api"})
    return {"status": "clocked_in", "message": "Enforcement enabled"}


@app.post("/api/events/log")
async def log_debug_event(request: LogEventRequest):
    """Log a custom event (for TUI debugging, etc.)."""
    await log_event(
        request.event_type,
        instance_id=request.instance_id,
        details=request.details
    )
    return {"status": "logged", "event_type": request.event_type}


# Device Endpoints
@app.get("/api/devices")
async def list_devices():
    """List all known devices."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM devices")
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


# ============ Audio Proxy Endpoints ============
# Handles phone audio routing through PC to headphones

@app.post("/api/audio-proxy/connect", response_model=AudioProxyConnectResponse)
async def audio_proxy_connect(request: AudioProxyConnectRequest):
    """
    Called by MacroDroid when phone connects to PC via Bluetooth.
    Starts the audio receiver on Windows to prepare for incoming audio stream.
    """
    global AUDIO_PROXY_STATE

    # Check if already connected
    if AUDIO_PROXY_STATE["phone_connected"]:
        # Verify receiver is actually running
        check = check_audio_receiver_running()
        return AudioProxyConnectResponse(
            success=True,
            action="already_connected",
            receiver_started=check.get("running", False),
            receiver_pid=check.get("pid"),
            message="Phone audio proxy already active"
        )

    # Start the audio receiver
    result = start_audio_receiver()

    if result.get("success"):
        # Update state
        AUDIO_PROXY_STATE["phone_connected"] = True
        AUDIO_PROXY_STATE["receiver_running"] = True
        AUDIO_PROXY_STATE["receiver_pid"] = result.get("pid")
        AUDIO_PROXY_STATE["last_connect_time"] = datetime.now().isoformat()

        # Log event
        await log_event(
            "audio_proxy_connected",
            device_id=request.phone_device_id,
            details={
                "bluetooth_device": request.bluetooth_device_name,
                "receiver_pid": result.get("pid"),
                "receiver_status": result.get("status"),
                "source": request.source,
                "port": AUDIO_RECEIVER_PORT
            }
        )

        action = "connected" if result.get("status") == "started" else "reconnected"
        return AudioProxyConnectResponse(
            success=True,
            action=action,
            receiver_started=True,
            receiver_pid=result.get("pid"),
            message=f"Audio proxy activated. Receiver listening on port {AUDIO_RECEIVER_PORT}."
        )
    else:
        # Failed to start receiver
        await log_event(
            "audio_proxy_connect_failed",
            device_id=request.phone_device_id,
            details={
                "error": result.get("error"),
                "source": request.source
            }
        )

        return AudioProxyConnectResponse(
            success=False,
            action="error",
            receiver_started=False,
            message=f"Failed to start audio receiver: {result.get('error')}"
        )


@app.post("/api/audio-proxy/disconnect")
async def audio_proxy_disconnect(request: AudioProxyDisconnectRequest):
    """
    Called by MacroDroid when phone disconnects from PC Bluetooth.
    Stops the audio receiver and cleans up.
    """
    global AUDIO_PROXY_STATE

    # Stop the audio receiver
    result = stop_audio_receiver()

    # Update state
    AUDIO_PROXY_STATE["phone_connected"] = False
    AUDIO_PROXY_STATE["receiver_running"] = False
    AUDIO_PROXY_STATE["receiver_pid"] = None
    AUDIO_PROXY_STATE["last_disconnect_time"] = datetime.now().isoformat()

    # Log event
    await log_event(
        "audio_proxy_disconnected",
        device_id=request.phone_device_id,
        details={
            "stopped_count": result.get("stopped_count", 0),
            "source": request.source
        }
    )

    return {
        "success": True,
        "action": "disconnected",
        "stopped_count": result.get("stopped_count", 0),
        "message": "Audio proxy deactivated. Phone can reconnect to headphones."
    }


@app.get("/api/audio-proxy/status", response_model=AudioProxyStatusResponse)
async def audio_proxy_status():
    """
    Get current audio proxy status.
    Verifies actual receiver state against stored state.
    """
    # Check actual receiver status
    check = check_audio_receiver_running()

    # Reconcile state if needed
    actual_running = check.get("running", False)
    actual_pid = check.get("pid")

    if actual_running != AUDIO_PROXY_STATE["receiver_running"]:
        AUDIO_PROXY_STATE["receiver_running"] = actual_running
        AUDIO_PROXY_STATE["receiver_pid"] = actual_pid

    return AudioProxyStatusResponse(
        phone_connected=AUDIO_PROXY_STATE["phone_connected"],
        receiver_running=actual_running,
        receiver_pid=actual_pid,
        last_connect_time=AUDIO_PROXY_STATE["last_connect_time"],
        last_disconnect_time=AUDIO_PROXY_STATE["last_disconnect_time"]
    )


# ============ Headless Mode Endpoints (disabled on macOS) ============

@app.get("/api/headless", response_model=HeadlessStatusResponse)
async def headless_status():
    """Headless mode is not applicable on macOS."""
    return HeadlessStatusResponse(**get_headless_state())


@app.post("/api/headless", response_model=HeadlessControlResponse)
async def headless_control(request: HeadlessControlRequest):
    """Headless mode is not applicable on macOS."""
    state = get_headless_state()
    return HeadlessControlResponse(
        success=False,
        action=request.action,
        before=HeadlessStatusResponse(**state),
        after=HeadlessStatusResponse(**state),
        message="Headless mode not available on macOS"
    )


# ============ System Control Endpoints ============
# Remote shutdown/restart

@app.post("/api/system/shutdown", response_model=ShutdownResponse)
async def system_shutdown(request: ShutdownRequest):
    """
    Shutdown or restart the Mac Mini.

    Actions:
    - shutdown: Power off the system
    - restart: Restart the system
    """
    action = request.action.lower()

    if action not in ("shutdown", "restart"):
        raise HTTPException(status_code=400, detail="Invalid action. Use 'shutdown' or 'restart'")

    if action == "restart":
        cmd = ["sudo", "shutdown", "-r"]
    else:
        cmd = ["sudo", "shutdown", "-h"]

    # macOS shutdown: +N means N minutes from now
    delay_minutes = max(1, request.delay_seconds // 60) if request.delay_seconds > 0 else 0
    cmd.append(f"+{delay_minutes}" if delay_minutes > 0 else "now")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)

        if result.returncode == 0:
            logger.info(f"SYSTEM: Initiated {action} with delay={delay_minutes}min")
            return ShutdownResponse(
                success=True,
                action=action,
                delay_seconds=request.delay_seconds,
                message=f"System {action} initiated" + (f" in {delay_minutes} minutes" if delay_minutes > 0 else "")
            )
        else:
            error_msg = result.stderr.strip() or result.stdout.strip()
            logger.error(f"SYSTEM: Failed to {action}: {error_msg}")
            return ShutdownResponse(
                success=False, action=action, delay_seconds=request.delay_seconds,
                message=f"Failed: {error_msg}"
            )
    except Exception as e:
        logger.error(f"SYSTEM: Error during {action}: {e}")
        return ShutdownResponse(
            success=False, action=action, delay_seconds=request.delay_seconds, message=str(e)
        )


@app.post("/api/system/shutdown/cancel")
async def cancel_shutdown():
    """Cancel a pending shutdown/restart."""
    try:
        result = subprocess.run(
            ["sudo", "killall", "shutdown"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            logger.info("SYSTEM: Cancelled pending shutdown")
            return {"success": True, "message": "Shutdown cancelled"}
        else:
            return {"success": False, "message": f"No pending shutdown or cancel failed: {result.stderr.strip()}"}
    except Exception as e:
        return {"success": False, "message": str(e)}


# ============ Task Endpoints ============

@app.get("/api/tasks", response_model=List[TaskResponse])
async def list_tasks():
    """List all scheduled tasks with their status."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM scheduled_tasks ORDER BY id")
        tasks = await cursor.fetchall()

        result = []
        for task in tasks:
            task_dict = dict(task)
            task_id = task_dict["id"]

            # Get last execution
            cursor = await db.execute(
                """SELECT * FROM task_executions
                   WHERE task_id = ?
                   ORDER BY started_at DESC LIMIT 1""",
                (task_id,)
            )
            last_exec = await cursor.fetchone()

            last_run = None
            if last_exec:
                last_exec_dict = dict(last_exec)
                last_run = {
                    "status": last_exec_dict["status"],
                    "started_at": last_exec_dict["started_at"],
                    "duration_ms": last_exec_dict["duration_ms"]
                }

            # Get next run time from scheduler
            next_run = None
            job = scheduler.get_job(task_id)
            if job and job.next_run_time:
                next_run = job.next_run_time.isoformat()

            result.append(TaskResponse(
                id=task_dict["id"],
                name=task_dict["name"],
                description=task_dict["description"],
                task_type=task_dict["task_type"],
                schedule=task_dict["schedule"],
                enabled=bool(task_dict["enabled"]),
                max_retries=task_dict["max_retries"],
                last_run=last_run,
                next_run=next_run
            ))

        return result


@app.get("/api/tasks/{task_id}", response_model=TaskResponse)
async def get_task(task_id: str):
    """Get details of a specific task."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM scheduled_tasks WHERE id = ?",
            (task_id,)
        )
        task = await cursor.fetchone()

        if not task:
            raise HTTPException(status_code=404, detail="Task not found")

        task_dict = dict(task)

        # Get last execution
        cursor = await db.execute(
            """SELECT * FROM task_executions
               WHERE task_id = ?
               ORDER BY started_at DESC LIMIT 1""",
            (task_id,)
        )
        last_exec = await cursor.fetchone()

        last_run = None
        if last_exec:
            last_exec_dict = dict(last_exec)
            last_run = {
                "status": last_exec_dict["status"],
                "started_at": last_exec_dict["started_at"],
                "duration_ms": last_exec_dict["duration_ms"]
            }

        # Get next run time
        next_run = None
        job = scheduler.get_job(task_id)
        if job and job.next_run_time:
            next_run = job.next_run_time.isoformat()

        return TaskResponse(
            id=task_dict["id"],
            name=task_dict["name"],
            description=task_dict["description"],
            task_type=task_dict["task_type"],
            schedule=task_dict["schedule"],
            enabled=bool(task_dict["enabled"]),
            max_retries=task_dict["max_retries"],
            last_run=last_run,
            next_run=next_run
        )


@app.patch("/api/tasks/{task_id}", response_model=TaskResponse)
async def update_task(task_id: str, request: TaskUpdateRequest):
    """Update a task's schedule or enabled status."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Check task exists
        cursor = await db.execute(
            "SELECT * FROM scheduled_tasks WHERE id = ?",
            (task_id,)
        )
        task = await cursor.fetchone()

        if not task:
            raise HTTPException(status_code=404, detail="Task not found")

        task_dict = dict(task)

        # Build update query
        updates = []
        params = []

        if request.schedule is not None:
            updates.append("schedule = ?")
            params.append(request.schedule)
            task_dict["schedule"] = request.schedule

        if request.enabled is not None:
            updates.append("enabled = ?")
            params.append(1 if request.enabled else 0)
            task_dict["enabled"] = request.enabled

        if request.max_retries is not None:
            updates.append("max_retries = ?")
            params.append(request.max_retries)
            task_dict["max_retries"] = request.max_retries

        if updates:
            updates.append("updated_at = ?")
            params.append(datetime.now().isoformat())
            params.append(task_id)

            await db.execute(
                f"UPDATE scheduled_tasks SET {', '.join(updates)} WHERE id = ?",
                params
            )
            await db.commit()

            # Update scheduler
            if request.enabled is False:
                # Remove job from scheduler
                if scheduler.get_job(task_id):
                    scheduler.remove_job(task_id)
            elif request.enabled is True or request.schedule is not None:
                # Re-register with new schedule
                if scheduler.get_job(task_id):
                    scheduler.remove_job(task_id)

                if task_dict["enabled"]:
                    try:
                        if task_dict["task_type"] == "interval":
                            trigger_kwargs = parse_interval_schedule(task_dict["schedule"])
                            trigger = IntervalTrigger(**trigger_kwargs)
                        else:
                            parts = task_dict["schedule"].split()
                            trigger = CronTrigger(
                                minute=parts[0],
                                hour=parts[1],
                                day=parts[2],
                                month=parts[3],
                                day_of_week=parts[4]
                            )

                        scheduler.add_job(
                            execute_task,
                            trigger=trigger,
                            args=[task_id],
                            id=task_id,
                            replace_existing=True
                        )
                    except Exception as e:
                        raise HTTPException(status_code=400, detail=f"Invalid schedule: {e}")

    # Return updated task
    return await get_task(task_id)


@app.post("/api/tasks/{task_id}/trigger")
async def trigger_task(task_id: str):
    """Manually trigger a task to run immediately."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id FROM scheduled_tasks WHERE id = ?",
            (task_id,)
        )
        if not await cursor.fetchone():
            raise HTTPException(status_code=404, detail="Task not found")

    if task_id not in TASK_REGISTRY:
        raise HTTPException(status_code=400, detail="Task has no implementation")

    # Run the task asynchronously
    asyncio.create_task(execute_task(task_id))

    return {"status": "triggered", "task_id": task_id}


@app.get("/api/tasks/{task_id}/history", response_model=List[TaskExecutionResponse])
async def get_task_history(task_id: str, limit: int = 20):
    """Get execution history for a task."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Check task exists
        cursor = await db.execute(
            "SELECT id FROM scheduled_tasks WHERE id = ?",
            (task_id,)
        )
        if not await cursor.fetchone():
            raise HTTPException(status_code=404, detail="Task not found")

        cursor = await db.execute(
            """SELECT * FROM task_executions
               WHERE task_id = ?
               ORDER BY started_at DESC
               LIMIT ?""",
            (task_id, limit)
        )
        rows = await cursor.fetchall()

        result = []
        for row in rows:
            row_dict = dict(row)
            result_data = None
            if row_dict["result"]:
                try:
                    result_data = json.loads(row_dict["result"])
                except:
                    result_data = {"raw": row_dict["result"]}

            result.append(TaskExecutionResponse(
                id=row_dict["id"],
                task_id=row_dict["task_id"],
                status=row_dict["status"],
                started_at=row_dict["started_at"],
                completed_at=row_dict["completed_at"],
                duration_ms=row_dict["duration_ms"],
                result=result_data,
                retry_count=row_dict["retry_count"]
            ))

        return result


# Health check
@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}


@app.get("/api/logs/recent", response_model=LogsResponse)
async def get_recent_logs(limit: int = 50):
    """
    Get recent server logs from circular buffer.

    Args:
        limit: Maximum number of logs to return (default 50, max 100)

    Returns:
        LogsResponse with recent logs and count
    """
    # Limit the limit parameter to max 100
    limit = min(limit, 100)

    # Get the most recent N entries from the buffer
    recent_logs = list(log_buffer)[-limit:]

    return {
        "logs": recent_logs,
        "count": len(recent_logs)
    }


# Root endpoint
@app.get("/")
async def root():
    """Root endpoint with API info."""
    return {
        "name": "Token-API",
        "version": "0.1.0",
        "description": "Local FastAPI server for Claude instance management",
        "docs": "/docs"
    }


# ============ TTS/Notification System ============

# Platform detection
IS_MACOS = sys.platform == "darwin"
DEFAULT_SOUND = "chimes.wav"


SOUND_MAP = {
    "chimes.wav": "/System/Library/Sounds/Glass.aiff",
    "notify.wav": "/System/Library/Sounds/Ping.aiff",
    "ding.wav": "/System/Library/Sounds/Tink.aiff",
    "tada.wav": "/System/Library/Sounds/Hero.aiff",
}


def play_sound(sound_file: str = None) -> dict:
    """Play a notification sound using macOS afplay."""
    sound_name = sound_file or DEFAULT_SOUND
    sound_path = SOUND_MAP.get(sound_name, SOUND_MAP["chimes.wav"])

    try:
        result = subprocess.run(
            ["afplay", sound_path],
            capture_output=True,
            timeout=10
        )
        if result.returncode == 0:
            return {"success": True, "method": "afplay", "file": sound_path}
        return {"success": False, "error": f"afplay failed: {result.stderr.decode()[:100]}"}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Sound playback timed out"}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def log_event_sync(event_type: str, instance_id: str = None, device_id: str = None, details: dict = None):
    """Synchronous wrapper for logging events (for use in sync functions)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO events (event_type, instance_id, device_id, details)
               VALUES (?, ?, ?, ?)""",
            (event_type, instance_id, device_id, json.dumps(details) if details else None)
        )
        await db.commit()


def clean_markdown_for_tts(text: str) -> str:
    """Clean markdown syntax for natural TTS output.

    Removes/transforms markdown that sounds bad when spoken aloud,
    like table separators ("pipe dash dash dash") or headers ("hash hash").
    """
    import re

    # Unicode arrows/symbols that TTS mispronounces
    text = text.replace('→', ' to ')
    text = text.replace('←', ' from ')
    text = text.replace('↔', ' both ways ')
    text = text.replace('⇒', ' implies ')
    text = text.replace('⇐', ' implied by ')
    text = text.replace('➜', ' to ')
    text = text.replace('➔', ' to ')
    text = text.replace('•', ',')  # Bullet point
    text = text.replace('…', '...')  # Ellipsis
    text = text.replace('—', ', ')  # Em dash
    text = text.replace('–', ', ')  # En dash

    # Remove backslashes that might be read aloud
    text = text.replace('\\', ' ')

    # Path compression - replace long paths with friendly names
    path_replacements = [
        ('~/.openclaw/workspace/', ''),
        ('~/', ''),
    ]
    for path, replacement in path_replacements:
        text = text.replace(path, replacement)

    # Table separators: |---|---| or |:---:|:---:| → remove entirely
    text = re.sub(r'\|[-:]+\|[-:|\s]+', '', text)  # Table separator rows
    text = re.sub(r'^-{3,}$', '', text, flags=re.MULTILINE)  # Horizontal rules

    # Headers: ## Title → Title (strip # sequences followed by space)
    text = re.sub(r'#{1,6}\s+', '', text)

    # Bold/italic: **text** or *text* or __text__ or _text_ → text
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)  # Bold
    text = re.sub(r'\*(.+?)\*', r'\1', text)       # Italic
    text = re.sub(r'__(.+?)__', r'\1', text)       # Bold alt
    text = re.sub(r'_(.+?)_', r'\1', text)         # Italic alt

    # Code blocks: ```...``` → [code block]
    text = re.sub(r'```[\s\S]*?```', '[code block]', text)

    # Inline code: `code` → code
    text = re.sub(r'`([^`]+)`', r'\1', text)

    # Links: [text](url) → text
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)

    # Bullet points: - item or * item → item
    text = re.sub(r'^[\-\*]\s+', '', text, flags=re.MULTILINE)

    # Numbered lists: 1. item → item
    text = re.sub(r'^\d+\.\s+', '', text, flags=re.MULTILINE)

    # Table pipes: | cell | cell | → cell, cell
    text = re.sub(r'\|', ', ', text)

    # Clean up multiple spaces/newlines
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'  +', ' ', text)
    text = re.sub(r', ,', ',', text)  # Clean double commas from empty cells

    return text.strip()


def speak_tts(message: str, voice: str = None, rate: int = 0, instance_id: str = None) -> dict:
    """Speak a message using macOS `say` command.

    Uses Popen instead of run() to allow process termination via skip_tts().
    """
    global tts_current_process, tts_skip_requested

    if not message:
        return {"success": False, "error": "No message provided"}

    # Clean markdown syntax for natural TTS output
    message = clean_markdown_for_tts(message)

    voice = voice or "Daniel"
    # Map SAPI rate scale (-10..10) to say WPM; default 0 → 190 WPM (slightly fast)
    wpm = 190 if rate == 0 else 175 + (rate * 15)
    wpm = max(80, min(300, wpm))

    try:
        process = subprocess.Popen(
            ["say", "-v", voice, "-r", str(wpm), message],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        tts_current_process = process
        process.wait(timeout=300)
        tts_current_process = None

        if process.returncode == 0:
            return {"success": True, "method": "macos_say", "voice": voice, "message": message[:50]}
        if tts_skip_requested:
            tts_skip_requested = False
            return {"success": True, "method": "skipped", "message": message[:50]}
        return {"success": False, "error": f"say failed with code {process.returncode}"}
    except subprocess.TimeoutExpired:
        if tts_current_process:
            tts_current_process.kill()
            tts_current_process = None
        return {"success": False, "error": "TTS timed out"}
    except Exception as e:
        tts_current_process = None
        return {"success": False, "error": str(e)}


# ============ TTS Queue System ============
# Ensures TTS messages don't overlap - each plays sequentially

from dataclasses import dataclass, field

@dataclass
class TTSQueueItem:
    """Item in the TTS queue."""
    instance_id: str
    message: str
    voice: str
    sound: str
    tab_name: str
    queued_at: datetime = field(default_factory=datetime.now)
    status: str = "queued"  # queued, playing, completed

# Global TTS queue state
tts_queue: Deque[TTSQueueItem] = deque()
tts_current: Optional[TTSQueueItem] = None
tts_current_process: Optional[subprocess.Popen] = None  # Current TTS/sound process for skip support
tts_skip_requested: bool = False  # Flag to indicate skip was requested (vs. actual failure)
tts_queue_lock = asyncio.Lock()
tts_worker_task: Optional[asyncio.Task] = None
stale_flag_cleaner_task: Optional[asyncio.Task] = None


async def tts_queue_worker():
    """Background worker that processes TTS queue sequentially."""
    global tts_current

    while True:
        try:
            # Wait for items in queue
            async with tts_queue_lock:
                if tts_queue:
                    tts_current = tts_queue.popleft()
                else:
                    tts_current = None

            if tts_current:
                # Log TTS starting
                await log_event(
                    "tts_playing",
                    instance_id=tts_current.instance_id,
                    details={
                        "message": tts_current.message[:100],
                        "voice": tts_current.voice,
                        "tab_name": tts_current.tab_name
                    }
                )

                # Play notification sound first (run in executor to not block event loop)
                sound_result = None
                if tts_current.sound:
                    loop = asyncio.get_event_loop()
                    sound_result = await loop.run_in_executor(None, play_sound, tts_current.sound)
                    logger.info(f"TTS worker: sound result = {json.dumps(sound_result)}")
                    if not sound_result.get("success"):
                        logger.warning(f"Sound failed: {sound_result.get('error')}")
                    await asyncio.sleep(0.3)  # Brief pause after sound

                # Speak the message (run in executor to allow skip API to interrupt)
                logger.info(f"TTS worker: speaking {len(tts_current.message)} chars with {tts_current.voice}")
                loop = asyncio.get_event_loop()
                tts_result = await loop.run_in_executor(None, speak_tts, tts_current.message, tts_current.voice)
                logger.info(f"TTS worker: speak result = {json.dumps(tts_result)}")

                # Log completion, skip, or failure
                if tts_result.get("success"):
                    if tts_result.get("method") == "skipped":
                        logger.info(f"TTS skipped for {tts_current.instance_id}")
                        await log_event(
                            "tts_skipped",
                            instance_id=tts_current.instance_id,
                            details={
                                "message": tts_current.message[:50],
                                "voice": tts_current.voice
                            }
                        )
                    else:
                        await log_event(
                            "tts_completed",
                            instance_id=tts_current.instance_id,
                            details={
                                "message": tts_current.message[:50],
                                "voice": tts_current.voice
                            }
                        )
                else:
                    logger.error(f"TTS failed for {tts_current.instance_id}: {tts_result.get('error')}")
                    await log_event(
                        "tts_failed",
                        instance_id=tts_current.instance_id,
                        details={
                            "message": tts_current.message[:50],
                            "voice": tts_current.voice,
                            "error": tts_result.get("error", "Unknown error"),
                            "sound_result": sound_result
                        }
                    )

                tts_current = None
                await asyncio.sleep(0.5)  # Brief pause between items
            else:
                # No items - wait a bit before checking again
                await asyncio.sleep(0.1)

        except Exception as e:
            print(f"TTS worker error: {e}")
            await asyncio.sleep(1)


async def clear_stale_processing_flags():
    """Background worker that auto-clears status='processing' for instances inactive > 5 minutes."""
    while True:
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute("""
                    UPDATE claude_instances
                    SET status = 'idle'
                    WHERE status = 'processing'
                      AND datetime(last_activity) < datetime('now', 'localtime', '-5 minutes')
                """)
                await db.commit()

                if cursor.rowcount > 0:
                    logger.warning(f"Auto-cleared {cursor.rowcount} stale processing flags")

            await asyncio.sleep(60)  # Run every minute

        except Exception as e:
            logger.error(f"Error clearing stale flags: {e}")
            await asyncio.sleep(60)


async def detect_stuck_instances():
    """Background worker that detects potentially stuck instances and logs diagnostics.

    An instance is considered potentially stuck if:
    - Status is 'processing' or 'idle' (not stopped)
    - Last activity was > 10 minutes ago
    - The stored PID doesn't match any running claude process

    Runs every 5 minutes. Logs warnings but doesn't take action.
    """
    await asyncio.sleep(120)  # Wait 2 min after startup before first check

    while True:
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute("""
                    SELECT id, tab_name, working_dir, pid, status, device_id, last_activity
                    FROM claude_instances
                    WHERE status IN ('processing', 'idle')
                      AND device_id = 'desktop'
                      AND datetime(last_activity) < datetime('now', 'localtime', '-10 minutes')
                """)
                stale_instances = await cursor.fetchall()

            for row in stale_instances:
                instance = dict(row)
                instance_id = instance["id"]
                tab_name = instance.get("tab_name", instance_id[:8])
                stored_pid = instance.get("pid")
                working_dir = instance.get("working_dir", "")
                last_activity = instance.get("last_activity")

                # Check if stored PID is still a claude process
                pid_valid = stored_pid and is_pid_claude(stored_pid)

                # Try to discover actual PID
                discovered_pid = await find_claude_pid_by_workdir(working_dir) if working_dir else None

                if not pid_valid and not discovered_pid:
                    # Ghost instance: no process found
                    logger.warning(
                        f"STUCK DETECTION: Ghost instance '{tab_name}' ({instance_id[:8]}...) - "
                        f"stored_pid={stored_pid} invalid, no process found in {working_dir}, "
                        f"last_activity={last_activity}"
                    )
                elif not pid_valid and discovered_pid:
                    # PID mismatch: process exists but DB has wrong PID
                    logger.warning(
                        f"STUCK DETECTION: PID mismatch '{tab_name}' ({instance_id[:8]}...) - "
                        f"stored_pid={stored_pid} invalid, discovered_pid={discovered_pid}, "
                        f"last_activity={last_activity}"
                    )
                elif pid_valid:
                    # Process exists but inactive for >10 min - might be stuck
                    diag = get_process_diagnostics(stored_pid)
                    state = diag.get("state", "?")
                    wchan = diag.get("wchan", "?")
                    children = len(diag.get("children", []))

                    # Log if in uninterruptible sleep (D) or has been in same state
                    if state == "D":
                        logger.warning(
                            f"STUCK DETECTION: Uninterruptible sleep '{tab_name}' ({instance_id[:8]}...) - "
                            f"PID {stored_pid} state=D wchan={wchan}, last_activity={last_activity}"
                        )
                    else:
                        logger.info(
                            f"STUCK CHECK: '{tab_name}' ({instance_id[:8]}...) - "
                            f"PID {stored_pid} state={state} wchan={wchan} children={children}, "
                            f"last_activity={last_activity} (>10min stale but process alive)"
                        )

            await asyncio.sleep(300)  # Run every 5 minutes

        except Exception as e:
            logger.error(f"Error in stuck detection: {e}")
            await asyncio.sleep(300)


async def queue_tts(instance_id: str, message: str) -> dict:
    """Queue a TTS message for an instance, using their profile's voice/sound."""
    # Look up instance to get their profile
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT tab_name, tts_voice, notification_sound FROM claude_instances WHERE id = ?",
            (instance_id,)
        )
        row = await cursor.fetchone()

    if not row:
        return {"success": False, "error": f"Instance {instance_id} not found"}

    voice = row["tts_voice"] or "Daniel"
    sound = row["notification_sound"] or "chimes.wav"
    tab_name = row["tab_name"] or instance_id

    item = TTSQueueItem(
        instance_id=instance_id,
        message=message,
        voice=voice,
        sound=sound,
        tab_name=tab_name
    )

    async with tts_queue_lock:
        tts_queue.append(item)
        position = len(tts_queue)

    # Log queued event
    await log_event(
        "tts_queued",
        instance_id=instance_id,
        details={
            "message": message[:100],
            "voice": voice,
            "position": position
        }
    )

    return {
        "success": True,
        "queued": True,
        "position": position,
        "voice": voice,
        "sound": sound
    }


def get_tts_queue_status() -> dict:
    """Get current TTS queue status for dashboard."""
    queue_list = []
    for item in tts_queue:
        queue_list.append({
            "instance_id": item.instance_id,
            "tab_name": item.tab_name,
            "message": item.message[:50] + "..." if len(item.message) > 50 else item.message,
            "voice": item.voice,
            "queued_at": item.queued_at.isoformat()
        })

    current = None
    if tts_current:
        current = {
            "instance_id": tts_current.instance_id,
            "tab_name": tts_current.tab_name,
            "message": tts_current.message[:50] + "..." if len(tts_current.message) > 50 else tts_current.message,
            "voice": tts_current.voice
        }

    return {
        "current": current,
        "queue": queue_list,
        "queue_length": len(queue_list)
    }


async def skip_tts(clear_queue: bool = False) -> dict:
    """Skip current TTS and optionally clear the queue.

    Args:
        clear_queue: If True, also clear all pending items in the queue.

    Returns:
        Dict with skipped (bool) and cleared (int) counts.
    """
    global tts_current_process, tts_current, tts_queue, tts_skip_requested

    result = {"skipped": False, "cleared": 0}

    # Kill current TTS process if running
    if tts_current_process and tts_current_process.poll() is None:
        # Set flag BEFORE killing so speak_tts() knows this was intentional
        tts_skip_requested = True
        try:
            tts_current_process.kill()
            tts_current_process.wait(timeout=1.0)
            result["skipped"] = True
            logger.info("TTS process killed via skip")
        except Exception as e:
            logger.warning(f"Error killing TTS process: {e}")

        tts_current_process = None

    # Clear queue if requested
    if clear_queue:
        async with tts_queue_lock:
            result["cleared"] = len(tts_queue)
            tts_queue.clear()
            if result["cleared"] > 0:
                logger.info(f"Cleared {result['cleared']} items from TTS queue")

    return result


def send_webhook(webhook_url: str, message: str, data: dict = None) -> dict:
    """Send notification via HTTP webhook."""
    payload = {
        "type": "notification",
        "message": message,
        "timestamp": datetime.now().isoformat(),
        **(data or {})
    }

    try:
        result = subprocess.run(
            [
                "curl", "-X", "POST",
                "-H", "Content-Type: application/json",
                "-d", json.dumps(payload),
                "--connect-timeout", "5",
                "-s",
                webhook_url
            ],
            capture_output=True,
            text=True,
            timeout=10
        )

        if result.returncode == 0:
            return {"success": True, "method": "webhook", "url": webhook_url}
        return {"success": False, "error": f"Webhook failed: {result.stderr}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/api/notify")
async def send_notification(request: NotifyRequest):
    """Send notification to a device (sound + TTS or webhook)."""
    results = {"sound": None, "tts": None, "webhook": None}

    # Determine target device
    device_id = request.device_id

    if not device_id and request.instance_id:
        # Look up instance to get device
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT device_id FROM claude_instances WHERE id = ?",
                (request.instance_id,)
            )
            row = await cursor.fetchone()
            if row:
                device_id = row["device_id"]

    if not device_id:
        device_id = "Mac-Mini"  # Default

    # Get device config
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM devices WHERE id = ?",
            (device_id,)
        )
        device = await cursor.fetchone()

    if not device:
        raise HTTPException(status_code=404, detail=f"Device not found: {device_id}")

    device = dict(device)
    method = device.get("notification_method", "tts_sound")

    if method == "tts_sound":
        # Desktop: play sound and speak
        await log_event(
            "tts_starting",
            instance_id=request.instance_id,
            device_id=device_id,
            details={"message": request.message[:100], "voice": request.voice or "default"}
        )
        results["sound"] = play_sound(request.sound)
        results["tts"] = speak_tts(request.message, request.voice)
    elif method == "webhook":
        # Mobile: send webhook
        webhook_url = device.get("webhook_url")
        if webhook_url:
            results["webhook"] = send_webhook(webhook_url, request.message)
        else:
            results["webhook"] = {"success": False, "error": "No webhook_url configured"}

    # Log the notification event
    await log_event(
        "notification_sent",
        device_id=device_id,
        details={"message": request.message[:100], "results": results}
    )

    return {
        "device_id": device_id,
        "method": method,
        "results": results
    }


@app.post("/api/notify/tts")
async def notify_tts(request: TTSRequest):
    """Speak a message using TTS only."""
    # Log TTS starting
    await log_event(
        "tts_starting",
        instance_id=request.instance_id,
        details={"message": request.message[:100], "voice": request.voice or "default"}
    )

    # Run in executor to allow skip API to interrupt
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, speak_tts, request.message, request.voice, request.rate)

    # Log TTS result
    await log_event(
        "tts_completed",
        instance_id=request.instance_id,
        details={"message": request.message[:50], "success": result.get("success", False)}
    )

    return result


@app.post("/api/notify/sound")
async def notify_sound(request: SoundRequest):
    """Play a notification sound only."""
    result = play_sound(request.sound_file)

    await log_event(
        "sound_played",
        details={"file": request.sound_file, "result": result}
    )

    return result


class QueueTTSRequest(BaseModel):
    instance_id: str
    message: str


@app.post("/api/notify/queue")
async def queue_tts_message(request: QueueTTSRequest):
    """Queue a TTS message for an instance. Uses the instance's profile voice/sound.

    Messages are played sequentially - if another TTS is playing, this will queue.
    Returns the queue position.
    """
    return await queue_tts(request.instance_id, request.message)


@app.get("/api/notify/queue/status")
async def get_queue_status():
    """Get current TTS queue status."""
    return get_tts_queue_status()


@app.post("/api/tts/skip")
async def api_tts_skip(clear_queue: bool = False):
    """Skip current TTS playback and optionally clear the queue.

    Args:
        clear_queue: Query param - if true, also clears all pending items.

    Returns:
        Dict with 'skipped' (bool) and 'cleared' (int count).
    """
    result = await skip_tts(clear_queue)
    await log_event("tts_skipped", details=result)
    return result


@app.get("/api/notify/test")
async def test_notification():
    """Test the notification system with a simple message."""
    sound_result = play_sound()
    tts_result = speak_tts("Token API notification test")

    return {
        "sound": sound_result,
        "tts": tts_result,
        "message": "Test notification sent"
    }


# ============ Claude Code Hook Handlers ============
# Centralized handling for all Claude Code hooks
# Replaces shell scripts with Python for better reliability and debugging

async def handle_session_start(payload: dict) -> dict:
    """Handle SessionStart hook - register new Claude instance."""
    session_id = payload.get("session_id") or payload.get("conversation_id")
    if not session_id:
        session_id = f"claude-{int(time.time())}-{os.getpid()}"

    # Detect origin type from SSH_CLIENT env var in payload
    origin_type = "local"
    source_ip = None
    if payload.get("env", {}).get("SSH_CLIENT"):
        origin_type = "ssh"
        source_ip = payload["env"]["SSH_CLIENT"].split()[0]

    # Get working directory and tab name
    working_dir = payload.get("cwd") or os.getcwd()
    tab_name = payload.get("env", {}).get("CLAUDE_TAB_NAME") or f"Claude {datetime.now().strftime('%H:%M')}"

    # Resolve device_id from source_ip
    device_id = resolve_device_from_ip(source_ip) if source_ip else "Mac-Mini"

    async with aiosqlite.connect(DB_PATH) as db:
        # Check if already registered
        cursor = await db.execute(
            "SELECT id FROM claude_instances WHERE id = ?",
            (session_id,)
        )
        if await cursor.fetchone():
            return {"success": True, "action": "already_registered", "instance_id": session_id}

        # Get currently used profiles
        cursor = await db.execute(
            "SELECT profile_name FROM claude_instances WHERE status IN ('processing', 'idle')"
        )
        rows = await cursor.fetchall()
        used_profiles = {row[0] for row in rows if row[0]}

        # Assign profile
        profile = get_next_available_profile(used_profiles)

        # Insert instance
        now = datetime.now().isoformat()
        internal_session_id = str(uuid.uuid4())
        await db.execute(
            """INSERT INTO claude_instances
               (id, session_id, tab_name, working_dir, origin_type, source_ip, device_id,
                profile_name, tts_voice, notification_sound, pid, status,
                registered_at, last_activity)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'idle', ?, ?)""",
            (
                session_id,
                internal_session_id,
                tab_name,
                working_dir,
                origin_type,
                source_ip,
                device_id,
                profile["name"],
                profile["tts_voice"],
                profile["notification_sound"],
                payload.get("pid"),
                now,
                now
            )
        )
        await db.commit()

    logger.info(f"Hook: SessionStart registered {session_id[:12]}... ({working_dir})")
    await log_event("instance_registered", instance_id=session_id, device_id=device_id,
                    details={"tab_name": tab_name, "origin_type": origin_type, "source": "hook"})

    return {
        "success": True,
        "action": "registered",
        "instance_id": session_id,
        "profile": profile["name"]
    }


async def handle_session_end(payload: dict) -> dict:
    """Handle SessionEnd hook - deregister Claude instance."""
    session_id = payload.get("session_id") or payload.get("conversation_id")
    if not session_id:
        return {"success": False, "action": "no_session_id"}

    now = datetime.now().isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id, device_id FROM claude_instances WHERE id = ?",
            (session_id,)
        )
        row = await cursor.fetchone()

        if not row:
            return {"success": False, "action": "not_found", "instance_id": session_id}

        await db.execute(
            "UPDATE claude_instances SET status = 'stopped', stopped_at = ? WHERE id = ?",
            (now, session_id)
        )
        await db.commit()

        # Check remaining active instances
        cursor = await db.execute(
            "SELECT COUNT(*) FROM claude_instances WHERE status IN ('processing', 'idle')"
        )
        count_row = await cursor.fetchone()
        remaining_active = count_row[0] if count_row else 0

    logger.info(f"Hook: SessionEnd stopped {session_id[:12]}...")
    await log_event("instance_stopped", instance_id=session_id, device_id=row[1],
                    details={"source": "hook"})

    # Handle productivity enforcement if needed
    result = {"success": True, "action": "stopped", "instance_id": session_id}
    if remaining_active == 0 and DESKTOP_STATE.get("current_mode") == "video":
        enforce_result = close_distraction_windows()
        result["enforcement_triggered"] = True
        result["enforcement_result"] = enforce_result

    return result


async def handle_prompt_submit(payload: dict) -> dict:
    """Handle UserPromptSubmit hook - mark instance as processing."""
    session_id = payload.get("session_id")
    if not session_id:
        return {"success": False, "action": "no_session_id"}

    now = datetime.now().isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id FROM claude_instances WHERE id = ?",
            (session_id,)
        )
        if not await cursor.fetchone():
            return {"success": False, "action": "not_found"}

        # Also resurrect stopped instances - activity means they're active
        # Backfill PID if payload contains one and DB value is NULL
        await db.execute(
            """UPDATE claude_instances
               SET status = 'processing', last_activity = ?, stopped_at = NULL,
                   pid = COALESCE(pid, ?)
               WHERE id = ?""",
            (now, payload.get("pid"), session_id)
        )
        await db.commit()

    logger.info(f"Hook: PromptSubmit {session_id[:12]}... -> processing (resurrected if stopped)")
    return {"success": True, "action": "processing", "instance_id": session_id}


async def handle_post_tool_use(payload: dict) -> dict:
    """Handle PostToolUse hook - heartbeat with debouncing, ensures status='processing'."""
    session_id = payload.get("session_id")
    if not session_id:
        return {"success": False, "action": "no_session_id"}

    # Debounce: only update every 2 seconds per session
    current_time = time.time()
    last_call = _post_tool_debounce.get(session_id, 0)
    if current_time - last_call < 2:
        return {"success": True, "action": "debounced"}

    _post_tool_debounce[session_id] = current_time

    # Update last_activity as heartbeat AND ensure status='processing'
    # This catches cases where prompt_submit was missed (e.g., after context clear)
    # Also resurrect stopped instances - activity means they're active
    # Backfill PID if payload contains one and DB value is NULL
    now = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE claude_instances
               SET status = 'processing', last_activity = ?, stopped_at = NULL,
                   pid = COALESCE(pid, ?)
               WHERE id = ?""",
            (now, payload.get("pid"), session_id)
        )
        await db.commit()

    return {"success": True, "action": "heartbeat", "instance_id": session_id}


async def handle_stop(payload: dict) -> dict:
    """Handle Stop hook - response completed, trigger TTS/notifications."""
    session_id = payload.get("session_id")
    if not session_id:
        return {"success": False, "action": "no_session_id"}

    # Prevent infinite loops
    if payload.get("stop_hook_active"):
        return {"success": True, "action": "skipped_recursive"}

    # Get instance info
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM claude_instances WHERE id = ?",
            (session_id,)
        )
        instance = await cursor.fetchone()

    if not instance:
        return {"success": False, "action": "instance_not_found"}

    instance = dict(instance)
    device_id = instance.get("device_id", "Mac-Mini")
    tab_name = instance.get("tab_name", "Claude")
    tts_voice = instance.get("tts_voice", "Daniel")
    notification_sound = instance.get("notification_sound", "chimes.wav")

    # Mark as no longer processing
    now = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE claude_instances SET status = 'idle', last_activity = ? WHERE id = ?",
            (now, session_id)
        )
        await db.commit()

    result = {
        "success": True,
        "action": "stop_processed",
        "instance_id": session_id,
        "device_id": device_id
    }

    # Mobile path: send webhook notification
    if device_id == "Token-S24":
        webhook_result = send_webhook(
            "http://100.102.92.24:7777/notify",
            f"[{tab_name}] Claude finished"
        )
        result["notification"] = webhook_result
        logger.info(f"Hook: Stop {session_id[:12]}... -> mobile notification")
        return result

    # Desktop path: TTS and notification
    # Extract TTS text from transcript
    transcript_path = payload.get("transcript_path")
    tts_text = None

    if transcript_path and os.path.exists(transcript_path):
        try:
            with open(transcript_path, 'r') as f:
                lines = f.readlines()

            # Find last assistant message
            for line in reversed(lines):
                if '"role":"assistant"' in line:
                    try:
                        data = json.loads(line)
                        content = data.get("message", {}).get("content")
                        if isinstance(content, str):
                            tts_text = content
                        elif isinstance(content, list):
                            # Extract text from content array
                            texts = [c.get("text", "") for c in content if c.get("type") == "text"]
                            tts_text = "\n".join(texts)
                        elif isinstance(content, dict) and "text" in content:
                            tts_text = content["text"]
                        break
                    except json.JSONDecodeError:
                        continue
        except Exception as e:
            logger.warning(f"Failed to read transcript: {e}")

    # Check TTS config
    tts_config_file = Path.home() / ".claude" / ".tts-config.json"
    tts_enabled = True

    if tts_config_file.exists():
        try:
            with open(tts_config_file) as f:
                config = json.load(f)
                tts_enabled = config.get("enabled", True)
        except Exception:
            pass

    # Sanitize TTS text (remove markdown formatting and normalize whitespace)
    if tts_text:
        # Strip markdown headers (must be before newline conversion)
        tts_text = re.sub(r'^#{1,6}\s*', '', tts_text, flags=re.MULTILINE)
        # Strip markdown bold/italic
        tts_text = re.sub(r'\*\*([^*]+)\*\*', r'\1', tts_text)  # **bold**
        tts_text = re.sub(r'\*([^*]+)\*', r'\1', tts_text)      # *italic*
        tts_text = re.sub(r'__([^_]+)__', r'\1', tts_text)      # __bold__
        tts_text = re.sub(r'_([^_]+)_', r'\1', tts_text)        # _italic_
        # Strip inline code
        tts_text = re.sub(r'`([^`]+)`', r'\1', tts_text)
        # Strip code blocks
        tts_text = re.sub(r'```[\s\S]*?```', '', tts_text)
        # Strip bullet points and list markers
        tts_text = re.sub(r'^[\s]*[-*+]\s+', '', tts_text, flags=re.MULTILINE)
        tts_text = re.sub(r'^[\s]*\d+\.\s+', '', tts_text, flags=re.MULTILINE)
        # Convert newlines to spaces
        tts_text = tts_text.replace('\n', ' ')
        # Normalize multiple spaces
        tts_text = re.sub(r' +', ' ', tts_text)
        tts_text = tts_text.strip()

    # Queue TTS if enabled and we have text
    if tts_enabled and tts_text:
        logger.info(f"Hook: Stop queuing TTS, {len(tts_text)} chars: {tts_text[:80]}...")
        tts_result = await queue_tts(session_id, tts_text)
        logger.info(f"Hook: Stop queue_tts result: {json.dumps(tts_result)}")
        result["tts"] = tts_result
    else:
        # Just play notification sound without TTS
        logger.info(f"Hook: Stop no TTS text (tts_enabled={tts_enabled}, has_text={bool(tts_text)})")
        play_sound(notification_sound)
        result["sound"] = {"played": notification_sound}

    logger.info(f"Hook: Stop {session_id[:12]}... -> desktop notification")
    await log_event("hook_stop", instance_id=session_id, details={"tts_enabled": tts_enabled, "tts_length": len(tts_text) if tts_text else 0})

    return result


async def handle_pre_tool_use(payload: dict) -> dict:
    """Handle PreToolUse hook - marks processing, can block operations like 'make deploy'."""
    session_id = payload.get("session_id")
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {})

    # Mark instance as processing (catches cases where prompt_submit was missed)
    # Also resurrect stopped instances - activity means they're active
    if session_id:
        now = datetime.now().isoformat()
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """UPDATE claude_instances
                   SET status = 'processing', last_activity = ?, stopped_at = NULL
                   WHERE id = ?""",
                (now, session_id)
            )
            await db.commit()

    # Only check Bash commands for blocking
    if tool_name != "Bash":
        return {"success": True, "action": "allowed"}

    command = tool_input.get("command", "")

    # Block 'make deploy' commands
    if "make deploy" in command or command.strip() == "make deploy":
        # Build alternative command suggestion
        deploy_args = []
        if "ENVIRONMENT=production" in command:
            deploy_args.append("production")
        if "--blocking" in command:
            deploy_args.append("--blocking")

        alt_command = "deploy"
        if deploy_args:
            alt_command += " " + " ".join(deploy_args)

        return {
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"'make deploy' is disabled. Use autonomous deployment instead:\n\n"
                f"  {alt_command}\n\n"
                f"This provides better error detection and log monitoring."
            )
        }

    return {"success": True, "action": "allowed"}


async def handle_notification(payload: dict) -> dict:
    """Handle Notification hook - play notification sound."""
    session_id = payload.get("session_id")

    # Get instance profile for sound selection
    sound_file = "chimes.wav"  # default

    if session_id:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT notification_sound FROM claude_instances WHERE id = ?",
                (session_id,)
            )
            row = await cursor.fetchone()
            if row and row["notification_sound"]:
                sound_file = row["notification_sound"]

    result = play_sound(sound_file)
    return {"success": True, "action": "sound_played", "sound": sound_file, "result": result}


# Hook dispatcher endpoint
@app.post("/api/hooks/{action_type}")
async def dispatch_hook(action_type: str, payload: dict) -> dict:
    """
    Unified hook dispatcher for Claude Code hooks.

    Receives hook events from generic-hook.sh and routes to appropriate handler.
    Always returns a response - errors are logged but don't cause failures.
    """
    handlers = {
        "SessionStart": handle_session_start,
        "SessionEnd": handle_session_end,
        "UserPromptSubmit": handle_prompt_submit,
        "PostToolUse": handle_post_tool_use,
        "Stop": handle_stop,
        "PreToolUse": handle_pre_tool_use,
        "Notification": handle_notification,
    }

    handler = handlers.get(action_type)
    if not handler:
        logger.warning(f"Hook: Unknown action type: {action_type}")
        return {"success": False, "action": "unknown_hook_type", "type": action_type}

    try:
        result = await handler(payload)
        return result
    except Exception as e:
        logger.error(f"Hook handler error ({action_type}): {e}")
        await log_event("hook_error", details={"action_type": action_type, "error": str(e)})
        return {"success": False, "action": "handler_error", "error": str(e)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=SERVER_PORT)
