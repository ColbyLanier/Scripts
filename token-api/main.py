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
from pydantic import BaseModel, Field
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

# Configure logging for TUI capture
logger = logging.getLogger("token_api")
logger.setLevel(logging.INFO)


# Configuration
DB_PATH = Path.home() / ".claude" / "agents.db"
SERVER_PORT = 7777  # Authoritative port for Token API

# Device IP mapping for SSH detection
DEVICE_IPS = {
    "100.102.92.24": "Token-S24",
    "100.66.10.74": "desktop",
}

# Profile pool for voice/sound assignment
PROFILES = [
    {"name": "profile_1", "tts_voice": "Microsoft David Desktop", "notification_sound": "chimes.wav", "color": "#0099ff"},
    {"name": "profile_2", "tts_voice": "Microsoft Zira Desktop", "notification_sound": "notify.wav", "color": "#00cc66"},
    {"name": "profile_3", "tts_voice": "Microsoft David Desktop", "notification_sound": "ding.wav", "color": "#ff9900"},
    {"name": "profile_4", "tts_voice": "Microsoft Zira Desktop", "notification_sound": "tada.wav", "color": "#cc66ff"},
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
    is_processing: bool = False
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


# Database initialization
async def init_db():
    """Initialize SQLite database with required tables."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    async with aiosqlite.connect(DB_PATH) as db:
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
                status TEXT DEFAULT 'active',
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


def get_next_available_profile(used_profiles: set) -> dict:
    """Get the next available profile from the pool."""
    for profile in PROFILES:
        if profile["name"] not in used_profiles:
            return profile
    # If all profiles used, cycle back to first
    return PROFILES[0]


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
            WHERE status = 'active'
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
    await run_overdue_tasks()
    yield
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
        device_id = "desktop"  # Default to desktop for local sessions

    async with aiosqlite.connect(DB_PATH) as db:
        # Get currently used profiles
        cursor = await db.execute(
            "SELECT profile_name FROM claude_instances WHERE status = 'active'"
        )
        rows = await cursor.fetchall()
        used_profiles = {row[0] for row in rows if row[0]}

        # Assign profile
        profile = get_next_available_profile(used_profiles)

        # Insert instance
        now = datetime.now().isoformat()
        await db.execute(
            """INSERT INTO claude_instances
               (id, session_id, tab_name, working_dir, origin_type, source_ip, device_id,
                profile_name, tts_voice, notification_sound, pid, status,
                registered_at, last_activity)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
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
        active_count = sum(1 for _, _, status in all_instances if status == 'active')

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
            "SELECT COUNT(*) FROM claude_instances WHERE status = 'active'"
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


class RenameInstanceRequest(BaseModel):
    tab_name: str


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


@app.post("/api/instances/{instance_id}/activity")
async def update_instance_activity(instance_id: str, request: ActivityRequest):
    """Update instance processing state. Called by hooks on prompt_submit and stop."""
    now = datetime.now().isoformat()

    if request.action == "prompt_submit":
        is_processing = 1
        logger.info(f"Activity: {instance_id[:8]}... prompt submitted")
    elif request.action == "stop":
        is_processing = 0
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
            "UPDATE claude_instances SET is_processing = ?, last_activity = ? WHERE id = ?",
            (is_processing, now, instance_id)
        )
        await db.commit()

    return {
        "status": "updated",
        "instance_id": instance_id,
        "action": request.action,
        "is_processing": bool(is_processing)
    }


@app.get("/api/instances/{instance_id}/todos")
async def get_instance_todos(instance_id: str):
    """Get the todo list for an instance (uses instance_id as conversation ID)."""
    todos_dir = Path.home() / ".claude" / "todos"

    # Todo files are named with the conversation ID (which is the instance_id)
    todo_file = todos_dir / f"{instance_id}-agent-{instance_id}.json"

    if not todo_file.exists():
        return {"todos": [], "progress": 0, "current_task": None}

    try:
        with open(todo_file) as f:
            todos = json.load(f)

        if not todos:
            return {"todos": [], "progress": 0, "current_task": None}

        completed = sum(1 for t in todos if t.get("status") == "completed")
        total = len(todos)
        progress = int((completed / total) * 100) if total > 0 else 0

        current_task = None
        for t in todos:
            if t.get("status") == "in_progress":
                current_task = t.get("activeForm") or t.get("content")
                break

        return {
            "todos": todos,
            "progress": progress,
            "completed": completed,
            "total": total,
            "current_task": current_task
        }
    except Exception as e:
        return {"todos": [], "progress": 0, "current_task": None, "error": str(e)}


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
        active_count = sum(1 for i in instances if i["status"] == "active")
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

# ============ Audio Proxy State ============
# Tracks phone audio proxy status for routing phone audio through PC

AUDIO_PROXY_STATE = {
    "phone_connected": False,
    "receiver_running": False,
    "receiver_pid": None,
    "last_connect_time": None,
    "last_disconnect_time": None,
}

# Path to audio receiver script on Windows
AUDIO_RECEIVER_PATH = r"C:\Scripts\audio-receiver.py"
AUDIO_RECEIVER_PORT = 8765

# ============ Headless Mode State ============
# Controls monitor disconnection for headless operation

HEADLESS_STATE_FILE = Path("/home/token/Scripts/Powershell/headless-state.json")


def get_headless_state() -> dict:
    """
    Read headless mode state from Windows state file.
    Returns dict with 'enabled', 'lastChanged', 'hostname'.
    """
    if HEADLESS_STATE_FILE.exists():
        try:
            with open(HEADLESS_STATE_FILE, encoding="utf-8-sig") as f:
                data = json.load(f)
                return {
                    "enabled": data.get("enabled", False),
                    "last_changed": data.get("lastChanged"),
                    "hostname": data.get("hostname"),
                }
        except (json.JSONDecodeError, Exception) as e:
            logger.warning(f"Failed to read headless state: {e}")
    return {"enabled": False, "last_changed": None, "hostname": None, "error": "state file not found"}


def trigger_headless_task(action: str = "toggle") -> tuple[bool, str]:
    """
    Trigger Windows scheduled task to control headless mode.

    Args:
        action: "toggle", "enable", or "disable"

    Returns:
        (success, message)
    """
    task_map = {
        "toggle": "HeadlessToggle",
        "enable": "HeadlessEnable",
        "disable": "HeadlessDisable"
    }

    task_name = task_map.get(action.lower())
    if not task_name:
        return False, f"Invalid action: {action}. Use toggle/enable/disable"

    try:
        schtasks_path = "/mnt/c/Windows/System32/schtasks.exe"
        result = subprocess.run(
            [schtasks_path, "/run", "/tn", task_name],
            capture_output=True,
            text=True,
            timeout=10
        )

        if result.returncode == 0:
            logger.info(f"HEADLESS: Triggered task {task_name}")
            return True, f"Task {task_name} triggered successfully"
        else:
            error_msg = result.stderr.strip() or result.stdout.strip()
            logger.error(f"HEADLESS: Failed to trigger {task_name}: {error_msg}")
            return False, f"Failed to trigger task: {error_msg}"

    except subprocess.TimeoutExpired:
        return False, "Task trigger timed out"
    except Exception as e:
        logger.error(f"HEADLESS: Error triggering task: {e}")
        return False, str(e)


def start_audio_receiver() -> dict:
    """
    Start audio-receiver.py on Windows via PowerShell.
    Called from WSL to start the audio receiver on Windows.
    Returns dict with success status, PID if started.
    """
    # PowerShell script to start the audio receiver
    ps_script = f'''
    # Check if already running
    $existing = Get-Process -Name "python*" -ErrorAction SilentlyContinue | Where-Object {{
        $_.CommandLine -like "*audio-receiver*"
    }}
    if ($existing) {{
        Write-Output "already_running:$($existing.Id)"
        exit 0
    }}

    # Check if script exists
    if (-not (Test-Path "{AUDIO_RECEIVER_PATH}")) {{
        Write-Output "error:script_not_found"
        exit 1
    }}

    # Start the receiver in background
    $proc = Start-Process -FilePath "python" -ArgumentList "{AUDIO_RECEIVER_PATH}" -PassThru -WindowStyle Hidden
    Start-Sleep -Seconds 1

    # Verify it started
    if ($proc -and $proc.Id) {{
        $check = Get-Process -Id $proc.Id -ErrorAction SilentlyContinue
        if ($check) {{
            Write-Output "started:$($proc.Id)"
            exit 0
        }}
    }}
    Write-Output "error:failed_to_start"
    exit 1
    '''

    try:
        result = subprocess.run(
            ["powershell.exe", "-Command", ps_script],
            capture_output=True,
            text=True,
            timeout=15
        )

        output = result.stdout.strip().split('\n')[-1] if result.stdout.strip() else ""

        if output.startswith("started:"):
            pid = int(output.split(":")[1])
            print(f"AUDIO_PROXY: Started receiver with PID {pid}")
            return {"success": True, "status": "started", "pid": pid}
        elif output.startswith("already_running:"):
            pid = int(output.split(":")[1])
            print(f"AUDIO_PROXY: Receiver already running with PID {pid}")
            return {"success": True, "status": "already_running", "pid": pid}
        elif output.startswith("error:"):
            error = output.split(":")[1]
            print(f"AUDIO_PROXY: Error starting receiver: {error}")
            return {"success": False, "error": error}
        else:
            print(f"AUDIO_PROXY: Unknown response: {output}")
            return {"success": False, "error": f"unknown_response: {output}"}

    except subprocess.TimeoutExpired:
        print("AUDIO_PROXY: Timeout starting receiver")
        return {"success": False, "error": "timeout"}
    except Exception as e:
        print(f"AUDIO_PROXY: Exception starting receiver: {e}")
        return {"success": False, "error": str(e)}


def stop_audio_receiver() -> dict:
    """
    Stop audio-receiver.py on Windows via PowerShell.
    Returns dict with success status.
    """
    ps_script = '''
    $stopped = 0
    # Find python processes running audio-receiver
    Get-Process -Name "python*" -ErrorAction SilentlyContinue | ForEach-Object {
        try {
            $cmdline = (Get-CimInstance Win32_Process -Filter "ProcessId = $($_.Id)").CommandLine
            if ($cmdline -like "*audio-receiver*") {
                Stop-Process -Id $_.Id -Force
                $stopped++
            }
        } catch {}
    }
    # Also kill any orphaned ffplay processes
    Get-Process -Name "ffplay" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Write-Output "stopped:$stopped"
    '''

    try:
        result = subprocess.run(
            ["powershell.exe", "-Command", ps_script],
            capture_output=True,
            text=True,
            timeout=10
        )

        output = result.stdout.strip().split('\n')[-1] if result.stdout.strip() else ""

        if output.startswith("stopped:"):
            count = int(output.split(":")[1])
            print(f"AUDIO_PROXY: Stopped {count} receiver process(es)")
            return {"success": True, "stopped_count": count}
        else:
            return {"success": True, "stopped_count": 0}

    except subprocess.TimeoutExpired:
        print("AUDIO_PROXY: Timeout stopping receiver")
        return {"success": False, "error": "timeout"}
    except Exception as e:
        print(f"AUDIO_PROXY: Exception stopping receiver: {e}")
        return {"success": False, "error": str(e)}


def check_audio_receiver_running() -> dict:
    """
    Check if audio-receiver.py is currently running on Windows.
    Returns dict with running status and PID if found.
    """
    ps_script = '''
    $found = $null
    Get-Process -Name "python*" -ErrorAction SilentlyContinue | ForEach-Object {
        try {
            $cmdline = (Get-CimInstance Win32_Process -Filter "ProcessId = $($_.Id)").CommandLine
            if ($cmdline -like "*audio-receiver*") {
                $found = $_.Id
            }
        } catch {}
    }
    if ($found) {
        Write-Output "running:$found"
    } else {
        Write-Output "not_running"
    }
    '''

    try:
        result = subprocess.run(
            ["powershell.exe", "-Command", ps_script],
            capture_output=True,
            text=True,
            timeout=10
        )

        output = result.stdout.strip().split('\n')[-1] if result.stdout.strip() else ""

        if output.startswith("running:"):
            pid = int(output.split(":")[1])
            return {"running": True, "pid": pid}
        else:
            return {"running": False, "pid": None}

    except Exception as e:
        return {"running": False, "pid": None, "error": str(e)}


def close_distraction_windows() -> dict:
    """
    Close distraction windows (YouTube in Brave) via PowerShell.
    Called from WSL to execute on Windows.

    More aggressive approach: uses taskkill if WM_CLOSE fails.

    Returns dict with success status and details.
    """
    # PowerShell script to close Brave windows with YouTube in title
    # Uses multiple methods: WM_CLOSE first, then taskkill if needed
    ps_script = '''
    $closed = 0
    $targetPids = @()

    # Method 1: Find Brave processes with YouTube in title via Get-Process
    Get-Process -Name "brave" -ErrorAction SilentlyContinue | ForEach-Object {
        $proc = $_
        if ($proc.MainWindowTitle -match "YouTube") {
            $targetPids += $proc.Id
            try {
                $proc.CloseMainWindow() | Out-Null
                $closed++
            } catch {}
        }
    }

    # Method 2: Enumerate all windows to find YouTube tabs (catches non-main windows)
    Add-Type @"
    using System;
    using System.Runtime.InteropServices;
    using System.Text;
    using System.Collections.Generic;
    public class Win32Enum {
        [DllImport("user32.dll")]
        public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
        [DllImport("user32.dll")]
        public static extern int GetWindowText(IntPtr hWnd, StringBuilder lpString, int nMaxCount);
        [DllImport("user32.dll")]
        public static extern bool IsWindowVisible(IntPtr hWnd);
        [DllImport("user32.dll")]
        public static extern IntPtr SendMessage(IntPtr hWnd, uint Msg, IntPtr wParam, IntPtr lParam);
        [DllImport("user32.dll")]
        public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);
        public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
        public const uint WM_CLOSE = 0x0010;

        public static List<IntPtr> handles = new List<IntPtr>();
        public static List<uint> pids = new List<uint>();
    }
"@ -ErrorAction SilentlyContinue

    $callback = {
        param($hwnd, $lparam)
        if ([Win32Enum]::IsWindowVisible($hwnd)) {
            $sb = New-Object System.Text.StringBuilder 512
            [Win32Enum]::GetWindowText($hwnd, $sb, 512) | Out-Null
            $title = $sb.ToString()
            # Match YouTube in any Brave window
            if ($title -match "YouTube" -and $title -match "Brave") {
                [Win32Enum]::handles.Add($hwnd) | Out-Null
                $pid = 0
                [Win32Enum]::GetWindowThreadProcessId($hwnd, [ref]$pid) | Out-Null
                if ($pid -gt 0) {
                    [Win32Enum]::pids.Add($pid) | Out-Null
                }
            }
        }
        return $true
    }

    [Win32Enum]::EnumWindows($callback, [IntPtr]::Zero) | Out-Null

    # Send WM_CLOSE to all matching windows
    foreach ($h in [Win32Enum]::handles) {
        [Win32Enum]::SendMessage($h, [Win32Enum]::WM_CLOSE, [IntPtr]::Zero, [IntPtr]::Zero) | Out-Null
        $closed++
    }

    # Collect all target PIDs
    $allPids = $targetPids + [Win32Enum]::pids | Select-Object -Unique

    # Method 3: If we found processes but WM_CLOSE might not have worked, use taskkill
    # Wait a moment then check if still running
    if ($allPids.Count -gt 0) {
        Start-Sleep -Milliseconds 500
        foreach ($pid in $allPids) {
            $stillRunning = Get-Process -Id $pid -ErrorAction SilentlyContinue
            if ($stillRunning -and $stillRunning.MainWindowTitle -match "YouTube") {
                # Force kill this specific tab/process
                taskkill /PID $pid /F 2>$null | Out-Null
                $closed++
            }
        }
    }

    Write-Output $closed
    '''

    try:
        result = subprocess.run(
            ["powershell.exe", "-Command", ps_script],
            capture_output=True,
            text=True,
            timeout=10
        )

        closed_count = 0
        if result.stdout.strip():
            try:
                closed_count = int(result.stdout.strip().split('\n')[-1])
            except ValueError:
                pass

        if result.returncode == 0:
            print(f"ENFORCE: Closed {closed_count} distraction window(s)")
            return {"success": True, "closed_count": closed_count}
        else:
            print(f"ENFORCE: PowerShell error: {result.stderr[:200]}")
            return {"success": False, "error": result.stderr[:200]}

    except subprocess.TimeoutExpired:
        print("ENFORCE: PowerShell timeout")
        return {"success": False, "error": "timeout"}
    except Exception as e:
        print(f"ENFORCE: Error: {e}")
        return {"success": False, "error": str(e)}


def trigger_obsidian_command(command_id: str) -> bool:
    """
    Trigger an Obsidian command via Advanced URI plugin.
    From WSL, uses PowerShell to launch the URI on Windows.
    Returns True if command was dispatched successfully.
    """
    from urllib.parse import quote
    vault = quote(OBSIDIAN_CONFIG["vault_name"])
    uri = f"obsidian://advanced-uri?vault={vault}&commandid={command_id}"

    try:
        result = subprocess.run(
            ["powershell.exe", "-Command", f'Start-Process "{uri}"'],
            capture_output=True,
            text=True,
            timeout=5,
            cwd="/mnt/c"
        )
        if result.returncode == 0:
            print(f"OBSIDIAN: Triggered command '{command_id}'")
            return True
        else:
            print(f"OBSIDIAN: Failed to trigger '{command_id}': {result.stderr}")
            return False
    except subprocess.TimeoutExpired:
        print(f"OBSIDIAN: Timeout triggering '{command_id}'")
        return False
    except Exception as e:
        print(f"OBSIDIAN: Error triggering '{command_id}': {e}")
        return False


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
            "SELECT COUNT(*) FROM claude_instances WHERE status = 'active'"
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
            "SELECT COUNT(*) FROM claude_instances WHERE status = 'active'"
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
    # CLOCKED IN: Video mode (distraction) requires productivity to be active
    elif detected_mode == "video":
        if not productivity_active:
            allowed = False
            reason = "productivity_inactive"
            print(f"    Video mode blocked: no productivity active")

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


# ============ Headless Mode Endpoints ============
# Control monitor disconnection for headless operation

@app.get("/api/headless", response_model=HeadlessStatusResponse)
async def headless_status():
    """
    Get current headless mode status.
    Reads state from Windows-side state file.
    """
    state = get_headless_state()
    return HeadlessStatusResponse(**state)


@app.post("/api/headless", response_model=HeadlessControlResponse)
async def headless_control(request: HeadlessControlRequest):
    """
    Control headless mode (toggle/enable/disable monitors).

    Triggers Windows scheduled task which runs PowerShell with admin privileges.
    Requires Setup-HeadlessTask.ps1 to have been run as Administrator first.

    Idempotent: enable when already enabled (or disable when already disabled)
    returns success without triggering the task again.
    """
    action = request.action.lower()

    # Get state before action
    before_state = get_headless_state()
    current_enabled = before_state.get("enabled", False)

    # Idempotency check - skip if already in desired state
    if action == "enable" and current_enabled:
        return HeadlessControlResponse(
            success=True,
            action=action,
            before=HeadlessStatusResponse(**before_state),
            after=HeadlessStatusResponse(**before_state),
            message="Already enabled (no action taken)"
        )
    elif action == "disable" and not current_enabled:
        return HeadlessControlResponse(
            success=True,
            action=action,
            before=HeadlessStatusResponse(**before_state),
            after=HeadlessStatusResponse(**before_state),
            message="Already disabled (no action taken)"
        )

    # Trigger the scheduled task
    success, message = trigger_headless_task(action)

    if not success:
        return HeadlessControlResponse(
            success=False,
            action=action,
            before=HeadlessStatusResponse(**before_state),
            after=None,
            message=f"{message}. Hint: Run Setup-HeadlessTask.ps1 as Administrator first."
        )

    # Wait briefly for state file to update
    time.sleep(0.5)

    # Get state after action
    after_state = get_headless_state()

    return HeadlessControlResponse(
        success=True,
        action=action,
        before=HeadlessStatusResponse(**before_state),
        after=HeadlessStatusResponse(**after_state),
        message=message
    )


# ============ System Control Endpoints ============
# Remote shutdown/restart

@app.post("/api/system/shutdown", response_model=ShutdownResponse)
async def system_shutdown(request: ShutdownRequest):
    """
    Shutdown or restart the Windows system.

    Actions:
    - shutdown: Power off the system
    - restart: Restart the system

    Options:
    - delay_seconds: Wait before shutdown (default: 0)
    - force: Force close applications without saving (default: false)
    """
    action = request.action.lower()

    if action not in ("shutdown", "restart"):
        raise HTTPException(status_code=400, detail="Invalid action. Use 'shutdown' or 'restart'")

    # Build shutdown command
    # /s = shutdown, /r = restart, /t = timeout, /f = force
    shutdown_path = "/mnt/c/Windows/System32/shutdown.exe"
    cmd = [shutdown_path]

    if action == "restart":
        cmd.append("/r")
    else:
        cmd.append("/s")

    cmd.extend(["/t", str(request.delay_seconds)])

    if request.force:
        cmd.append("/f")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10
        )

        if result.returncode == 0:
            logger.info(f"SYSTEM: Initiated {action} with delay={request.delay_seconds}s, force={request.force}")
            return ShutdownResponse(
                success=True,
                action=action,
                delay_seconds=request.delay_seconds,
                message=f"System {action} initiated" + (f" in {request.delay_seconds} seconds" if request.delay_seconds > 0 else "")
            )
        else:
            error_msg = result.stderr.strip() or result.stdout.strip()
            logger.error(f"SYSTEM: Failed to {action}: {error_msg}")
            return ShutdownResponse(
                success=False,
                action=action,
                delay_seconds=request.delay_seconds,
                message=f"Failed to initiate {action}: {error_msg}"
            )

    except subprocess.TimeoutExpired:
        return ShutdownResponse(
            success=False,
            action=action,
            delay_seconds=request.delay_seconds,
            message="Command timed out"
        )
    except Exception as e:
        logger.error(f"SYSTEM: Error during {action}: {e}")
        return ShutdownResponse(
            success=False,
            action=action,
            delay_seconds=request.delay_seconds,
            message=str(e)
        )


@app.post("/api/system/shutdown/cancel")
async def cancel_shutdown():
    """Cancel a pending shutdown/restart."""
    try:
        result = subprocess.run(
            ["/mnt/c/Windows/System32/shutdown.exe", "/a"],
            capture_output=True,
            text=True,
            timeout=10
        )

        if result.returncode == 0:
            logger.info("SYSTEM: Cancelled pending shutdown")
            return {"success": True, "message": "Shutdown cancelled"}
        else:
            error_msg = result.stderr.strip() or result.stdout.strip()
            return {"success": False, "message": f"Failed to cancel: {error_msg}"}

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
def is_wsl() -> bool:
    """Check if running in WSL."""
    try:
        with open("/proc/version", "r") as f:
            return "microsoft" in f.read().lower() or "wsl" in f.read().lower()
    except:
        return False

IS_WSL = is_wsl()
DEFAULT_SOUND = "chimes.wav"  # Windows Media sound name


def play_sound(sound_file: str = None) -> dict:
    """Play a notification sound. In WSL, uses Windows Media.SoundPlayer."""
    sound_name = sound_file or DEFAULT_SOUND

    if IS_WSL:
        # Use Windows SoundPlayer via PowerShell
        sound_path = f"C:\\\\Windows\\\\Media\\\\{sound_name}"
        try:
            result = subprocess.run(
                ["powershell.exe", "-c", f"(New-Object Media.SoundPlayer '{sound_path}').PlaySync()"],
                capture_output=True,
                timeout=10
            )
            if result.returncode == 0:
                return {"success": True, "method": "windows_soundplayer", "file": sound_name}
            return {"success": False, "error": f"PowerShell sound failed: {result.stderr.decode()[:100]}"}
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "Sound playback timed out"}
        except Exception as e:
            return {"success": False, "error": str(e)}
    else:
        # Linux: try paplay then aplay
        sound_path = sound_file or "/usr/share/sounds/freedesktop/stereo/complete.oga"
        try:
            result = subprocess.run(["paplay", sound_path], capture_output=True, timeout=10)
            if result.returncode == 0:
                return {"success": True, "method": "paplay", "file": sound_path}
            result = subprocess.run(["aplay", sound_path], capture_output=True, timeout=10)
            if result.returncode == 0:
                return {"success": True, "method": "aplay", "file": sound_path}
            return {"success": False, "error": "Both paplay and aplay failed"}
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


def speak_tts(message: str, voice: str = None, rate: int = 0, instance_id: str = None) -> dict:
    """Speak a message using TTS. In WSL, uses Windows SAPI via PowerShell."""
    if not message:
        return {"success": False, "error": "No message provided"}

    if IS_WSL:
        # Use Windows SAPI via PowerShell
        voice = voice or "Microsoft Zira Desktop"

        # Escape special characters for PowerShell
        escaped = message.replace("\\", "\\\\").replace("'", "''").replace("$", "\\$").replace("`", "\\`")

        ps_script = f"""
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$synth.SelectVoice('{voice}')
$synth.Rate = [int]{rate}
$synth.Speak('{escaped}')
"""
        try:
            result = subprocess.run(
                ["powershell.exe"],
                input=ps_script.encode(),
                capture_output=True,
                timeout=30
            )
            if result.returncode == 0:
                return {"success": True, "method": "windows_sapi", "voice": voice, "message": message[:50]}
            error = result.stderr.decode()[:200] if result.stderr else "Unknown error"
            return {"success": False, "error": f"SAPI failed: {error}"}
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "TTS timed out"}
        except Exception as e:
            return {"success": False, "error": str(e)}
    else:
        # Linux: try espeak then festival
        try:
            espeak_rate = 175 + (rate * 25)  # Convert -10..10 to espeak scale
            espeak_rate = max(80, min(500, espeak_rate))
            result = subprocess.run(["espeak", "-s", str(espeak_rate), message], capture_output=True, timeout=30)
            if result.returncode == 0:
                return {"success": True, "method": "espeak", "message": message[:50]}

            result = subprocess.run(["festival", "--tts"], input=message.encode(), capture_output=True, timeout=30)
            if result.returncode == 0:
                return {"success": True, "method": "festival", "message": message[:50]}

            return {"success": False, "error": "Both espeak and festival failed"}
        except Exception as e:
            return {"success": False, "error": str(e)}


# ============ TTS Queue System ============
# Ensures TTS messages don't overlap - each plays sequentially

from dataclasses import dataclass, field
from typing import Deque
from collections import deque

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

                # Play notification sound first
                if tts_current.sound:
                    play_sound(tts_current.sound)
                    await asyncio.sleep(0.3)  # Brief pause after sound

                # Speak the message
                speak_tts(tts_current.message, tts_current.voice)

                # Log completion
                await log_event(
                    "tts_completed",
                    instance_id=tts_current.instance_id,
                    details={
                        "message": tts_current.message[:50],
                        "voice": tts_current.voice
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
    """Background worker that auto-clears is_processing for instances inactive > 5 minutes."""
    while True:
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute("""
                    UPDATE claude_instances
                    SET is_processing = 0
                    WHERE is_processing = 1
                      AND datetime(last_activity) < datetime('now', '-5 minutes')
                """)
                await db.commit()

                if cursor.rowcount > 0:
                    logger.warning(f"Auto-cleared {cursor.rowcount} stale processing flags")

            await asyncio.sleep(60)  # Run every minute

        except Exception as e:
            logger.error(f"Error clearing stale flags: {e}")
            await asyncio.sleep(60)


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

    voice = row["tts_voice"] or "Microsoft Zira Desktop"
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
        device_id = "desktop"  # Default

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

    result = speak_tts(request.message, request.voice, request.rate)

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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=SERVER_PORT)
