"""Core migration execution logic.

Handles running SQL migrations against dev/staging (direct) and production
(open IP -> run -> close IP lifecycle).
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any

try:
    import asyncpg
except ImportError:
    asyncpg = None  # type: ignore[assignment]

try:
    from google.cloud.sql.connector import Connector, IPTypes

    CLOUD_SQL_CONNECTOR_AVAILABLE = True
except ImportError:
    CLOUD_SQL_CONNECTOR_AVAILABLE = False
    Connector = None  # type: ignore[assignment, misc]
    IPTypes = None  # type: ignore[assignment, misc]

from cli_tools.db_query.query_runner import get_env_config, get_password, normalize_env


@dataclass
class MigrationResult:
    """Result of a migration run."""

    success: bool
    statements_executed: int = 0
    error: str | None = None
    duration_ms: float = 0
    messages: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# gcloud IP management (production only)
# ---------------------------------------------------------------------------


def get_public_ip() -> str:
    """Get our public IP address."""
    result = subprocess.run(
        ["curl", "-s", "ifconfig.me"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    ip = result.stdout.strip()
    if not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", ip):
        raise RuntimeError(f"Could not determine public IP (got: {ip!r})")
    return ip


def _parse_instance_connection(instance_conn: str) -> tuple[str, str]:
    """Parse 'project:region:instance' into (project, instance)."""
    parts = instance_conn.split(":")
    if len(parts) != 3:
        raise ValueError(
            f"Invalid INSTANCE_CONNECTION_NAME format: {instance_conn!r}. "
            "Expected 'project:region:instance'."
        )
    return parts[0], parts[2]


def open_public_ip(project: str, instance: str, ip: str) -> None:
    """Add our IP to the Cloud SQL authorized networks."""
    print(f"  Opening authorized network: {ip}/32 on {instance}...")
    result = subprocess.run(
        [
            "gcloud",
            "sql",
            "instances",
            "patch",
            instance,
            f"--project={project}",
            f"--authorized-networks={ip}/32",
            "--quiet",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to open authorized network:\n{result.stderr.strip()}"
        )


def close_public_ip(project: str, instance: str) -> None:
    """Clear all authorized networks on the Cloud SQL instance."""
    print("  Closing authorized networks...")
    result = subprocess.run(
        [
            "gcloud",
            "sql",
            "instances",
            "patch",
            instance,
            f"--project={project}",
            "--clear-authorized-networks",
            "--quiet",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        print(
            f"  WARNING: Failed to close authorized networks: {result.stderr.strip()}",
            file=sys.stderr,
        )
    else:
        print("  Authorized networks cleared.")


def wait_for_instance(project: str, instance: str, timeout_s: int = 120) -> None:
    """Poll until the Cloud SQL instance is RUNNABLE."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        result = subprocess.run(
            [
                "gcloud",
                "sql",
                "instances",
                "describe",
                instance,
                f"--project={project}",
                "--format=value(state)",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        state = result.stdout.strip()
        if state == "RUNNABLE":
            return
        print(f"  Instance state: {state}, waiting...")
        time.sleep(5)
    raise RuntimeError(f"Instance {instance} did not become RUNNABLE within {timeout_s}s")


# ---------------------------------------------------------------------------
# SQL helpers
# ---------------------------------------------------------------------------


def _sql_has_transaction_control(sql: str) -> bool:
    """Check if SQL already contains BEGIN/COMMIT/ROLLBACK statements."""
    upper = sql.upper()
    return bool(re.search(r"\bBEGIN\b", upper) or re.search(r"\bCOMMIT\b", upper))


# ---------------------------------------------------------------------------
# Migration execution
# ---------------------------------------------------------------------------


async def _connect_direct(
    env_config: dict[str, Any], password: str | None
) -> Any:
    """Connect via direct TCP (for prod after IP is opened)."""
    return await asyncio.wait_for(
        asyncpg.connect(
            host=env_config["host"],
            port=env_config["port"],
            database=env_config["database"],
            user=env_config["user"],
            password=password,
            timeout=30,
        ),
        timeout=30,
    )


async def _connect_connector(
    env_config: dict[str, Any], password: str | None
) -> tuple[Any, Any]:
    """Connect via Cloud SQL Python Connector (for dev/staging).

    Returns (connection, connector) — caller must close both.
    """
    loop = asyncio.get_running_loop()
    connector = Connector(loop=loop)

    conn = await connector.connect_async(
        env_config["instance"],
        "asyncpg",
        user=env_config["user"],
        password=password,
        db=env_config["database"],
        ip_type=IPTypes.PUBLIC,
    )
    return conn, connector


async def _execute_migration(
    conn: Any,
    sql_content: str,
    dry_run: bool,
) -> MigrationResult:
    """Run migration SQL on an already-established connection."""
    start = time.monotonic()
    has_txn = _sql_has_transaction_control(sql_content)
    messages: list[str] = []

    try:
        if dry_run:
            await conn.execute("BEGIN")
            messages.append("DRY RUN: Transaction started")
            try:
                result = await conn.execute(sql_content)
                messages.append(f"Executed successfully: {result}")
            except Exception as e:
                messages.append(f"Error during execution: {e}")
                await conn.execute("ROLLBACK")
                messages.append("DRY RUN: Rolled back")
                elapsed = (time.monotonic() - start) * 1000
                return MigrationResult(
                    success=False, error=str(e), duration_ms=elapsed, messages=messages
                )
            await conn.execute("ROLLBACK")
            messages.append("DRY RUN: Rolled back (no changes applied)")
        elif has_txn:
            messages.append("SQL contains transaction control, executing as-is")
            result = await conn.execute(sql_content)
            messages.append(f"Result: {result}")
        else:
            await conn.execute("BEGIN")
            messages.append("Transaction started")
            try:
                result = await conn.execute(sql_content)
                messages.append(f"Result: {result}")
                await conn.execute("COMMIT")
                messages.append("Transaction committed")
            except Exception as e:
                await conn.execute("ROLLBACK")
                messages.append(f"Transaction rolled back due to error: {e}")
                elapsed = (time.monotonic() - start) * 1000
                return MigrationResult(
                    success=False, error=str(e), duration_ms=elapsed, messages=messages
                )

        elapsed = (time.monotonic() - start) * 1000
        return MigrationResult(
            success=True, statements_executed=1, duration_ms=elapsed, messages=messages
        )
    except Exception as e:
        elapsed = (time.monotonic() - start) * 1000
        return MigrationResult(
            success=False, error=str(e), duration_ms=elapsed, messages=messages
        )


async def run_migration(
    env_config: dict[str, Any],
    sql_content: str,
    password: str | None,
    dry_run: bool = False,
    use_connector: bool = False,
) -> MigrationResult:
    """Execute a migration against the database.

    use_connector=True uses the Cloud SQL Python Connector (for dev/staging).
    use_connector=False uses direct TCP (for prod after IP is opened).
    """
    if asyncpg is None:
        return MigrationResult(
            success=False,
            error="asyncpg is required. Install with: pip install asyncpg",
        )

    start = time.monotonic()
    conn = None
    connector = None

    try:
        if use_connector:
            if not CLOUD_SQL_CONNECTOR_AVAILABLE:
                return MigrationResult(
                    success=False,
                    error="Cloud SQL Connector not available. Install with: pip install cloud-sql-python-connector[asyncpg]",
                )
            conn, connector = await _connect_connector(env_config, password)
        else:
            conn = await _connect_direct(env_config, password)

        return await _execute_migration(conn, sql_content, dry_run)

    except asyncio.TimeoutError:
        elapsed = (time.monotonic() - start) * 1000
        return MigrationResult(
            success=False, error="Connection timed out after 30s", duration_ms=elapsed
        )
    except Exception as e:
        elapsed = (time.monotonic() - start) * 1000
        return MigrationResult(
            success=False, error=str(e), duration_ms=elapsed
        )
    finally:
        if conn:
            await conn.close()
        if connector:
            await connector.close_async()


def run_prod_migration(
    env_config: dict[str, Any],
    sql_content: str,
    password: str | None,
    dry_run: bool = False,
) -> MigrationResult:
    """Run a migration against production with the open/run/close lifecycle.

    1. Get public IP
    2. Open authorized network on Cloud SQL
    3. Wait for instance RUNNABLE
    4. Run migration
    5. Close authorized network (always, via finally)
    """
    instance_conn = env_config.get("instance", "")
    project, instance = _parse_instance_connection(instance_conn)

    ip = get_public_ip()
    print(f"\n  Our public IP: {ip}")

    try:
        open_public_ip(project, instance, ip)
        wait_for_instance(project, instance)

        # Get the public IP of the Cloud SQL instance for direct connection
        result = subprocess.run(
            [
                "gcloud",
                "sql",
                "instances",
                "describe",
                instance,
                f"--project={project}",
                "--format=value(ipAddresses[0].ipAddress)",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        db_public_ip = result.stdout.strip()
        if not db_public_ip:
            return MigrationResult(
                success=False,
                error="Could not determine Cloud SQL public IP address",
            )

        print(f"  Cloud SQL public IP: {db_public_ip}")

        # Override host to use the public IP
        prod_config = env_config.copy()
        prod_config["host"] = db_public_ip

        print("  Running migration...\n")
        return asyncio.run(run_migration(prod_config, sql_content, password, dry_run))

    finally:
        close_public_ip(project, instance)
