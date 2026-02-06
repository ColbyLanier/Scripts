#!/usr/bin/env python3
"""
Initialize the SQLite database with required tables and seed data.
Run this script standalone or let the FastAPI app initialize on startup.
"""

import sqlite3
from pathlib import Path

DB_PATH = Path.home() / ".claude" / "agents.db"


def init_database():
    """Initialize SQLite database with required tables."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Enable WAL mode for concurrent read/write access
    # This prevents TUI reads from blocking server writes
    cursor.execute("PRAGMA journal_mode=WAL")

    # Set busy timeout to 5 seconds (prevents indefinite blocking on lock contention)
    cursor.execute("PRAGMA busy_timeout=5000")

    # Create claude_instances table
    cursor.execute("""
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
    cursor.execute("PRAGMA table_info(claude_instances)")
    columns = [col[1] for col in cursor.fetchall()]
    if 'is_processing' not in columns:
        cursor.execute("ALTER TABLE claude_instances ADD COLUMN is_processing INTEGER DEFAULT 0")
    if 'working_dir' not in columns:
        cursor.execute("ALTER TABLE claude_instances ADD COLUMN working_dir TEXT")

    # Migration: Convert two-field status (status + is_processing) to single enum
    # Old: status='active' + is_processing=0/1 → New: status='processing'/'idle'/'stopped'
    cursor.execute("SELECT COUNT(*) FROM claude_instances WHERE status = 'active'")
    if cursor.fetchone()[0] > 0:
        cursor.execute("""
            UPDATE claude_instances SET status = CASE
                WHEN status = 'active' AND is_processing = 1 THEN 'processing'
                WHEN status = 'active' AND is_processing = 0 THEN 'idle'
                ELSE status
            END
        """)
        conn.commit()

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_instances_status ON claude_instances(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_instances_device ON claude_instances(device_id)")

    # Create devices table
    cursor.execute("""
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
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            instance_id TEXT,
            device_id TEXT,
            details TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_events_time ON events(created_at DESC)")

    # Create scheduled_tasks table
    cursor.execute("""
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
    cursor.execute("""
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

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_task_executions_task_id ON task_executions(task_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_task_executions_started_at ON task_executions(started_at)")

    # Create task_locks table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS task_locks (
            task_id TEXT PRIMARY KEY,
            locked_at TIMESTAMP NOT NULL,
            locked_by TEXT,
            FOREIGN KEY (task_id) REFERENCES scheduled_tasks(id)
        )
    """)

    # Create audio_proxy_state table (for phone audio routing through PC)
    cursor.execute("""
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

    # Seed devices
    cursor.execute("""
        INSERT OR IGNORE INTO devices (id, name, type, tailscale_ip, notification_method, tts_engine)
        VALUES ('desktop', 'Desktop', 'local', '100.66.10.74', 'tts_sound', 'windows_sapi')
    """)

    cursor.execute("""
        INSERT OR IGNORE INTO devices (id, name, type, tailscale_ip, notification_method, webhook_url)
        VALUES ('Token-S24', 'Pixel Phone', 'mobile', '100.102.92.24', 'webhook', 'http://100.102.92.24:7777/notify')
    """)

    # Seed scheduled tasks
    cursor.execute("""
        INSERT OR IGNORE INTO scheduled_tasks (id, name, description, task_type, schedule, max_retries)
        VALUES ('cleanup_stale_instances', 'Cleanup Stale Instances',
                'Mark instances with no activity for 3+ hours as stopped',
                'interval', '30m', 2)
    """)

    cursor.execute("""
        INSERT OR IGNORE INTO scheduled_tasks (id, name, description, task_type, schedule, max_retries)
        VALUES ('purge_old_events', 'Purge Old Events',
                'Delete events older than 30 days',
                'cron', '0 3 * * *', 1)
    """)

    conn.commit()
    conn.close()

    print(f"Database initialized at {DB_PATH}")
    print("Tables created: claude_instances, devices, events, scheduled_tasks, task_executions, task_locks, audio_proxy_state")
    print("Devices seeded: desktop, Token-S24")
    print("Tasks seeded: cleanup_stale_instances, purge_old_events")


if __name__ == "__main__":
    init_database()
