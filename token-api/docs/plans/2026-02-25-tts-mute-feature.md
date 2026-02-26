# TTS Mute Feature Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add per-instance and global three-state TTS mute (verbose/muted/silent) with TUI controls (`m`/`M`).

**Architecture:** New `tts_mode` column on `claude_instances` (default `'verbose'`). In-memory `TTS_GLOBAL_MODE` dict on the server (resets to `verbose` on restart). The `queue_tts` function checks both per-instance and global mode — the more restrictive wins. Voice slots are released only in `silent` mode.

**Tech Stack:** Python, FastAPI, aiosqlite, Rich TUI

---

### Task 1: Database Migration — Add `tts_mode` Column

**Files:**
- Modify: `init_db.py:50-60` (add migration alongside existing ones)
- Modify: `main.py:465-490` (add migration in `init_db()` async version)

**Step 1: Add migration to `init_db.py`**

In `init_db.py`, after the existing `if 'spawner' not in columns:` block (~line 60), add:

```python
if 'tts_mode' not in columns:
    cursor.execute("ALTER TABLE claude_instances ADD COLUMN tts_mode TEXT DEFAULT 'verbose'")
```

**Step 2: Add migration to `main.py` async `init_db()`**

Find the async `init_db()` function in `main.py` that has the same pattern of PRAGMA table_info checks. Add the same migration there:

```python
if 'tts_mode' not in columns:
    await db.execute("ALTER TABLE claude_instances ADD COLUMN tts_mode TEXT DEFAULT 'verbose'")
```

**Step 3: Verify migration works**

Run: `cd /home/token/Scripts/token-api && python3 init_db.py`
Then: `agents-db query "PRAGMA table_info(claude_instances)"` — confirm `tts_mode` column exists.

**Step 4: Commit**

```bash
git add init_db.py main.py
git commit -m "feat: add tts_mode column to claude_instances (verbose/muted/silent)"
```

---

### Task 2: Server — Global TTS Mode State + API Endpoints

**Files:**
- Modify: `main.py` — add global state near `TTS_BACKEND` (~line 2396), add two new endpoints

**Step 1: Add global TTS mode state**

After the `TTS_BACKEND` dict (~line 2396), add:

```python
# Global TTS mute state (in-memory, resets to "verbose" on server restart)
TTS_GLOBAL_MODE = {
    "mode": "verbose",  # "verbose" | "muted" | "silent"
}
```

**Step 2: Add per-instance tts-mode endpoint**

Add near the other instance PATCH endpoints (after `/api/instances/{instance_id}/voice`):

```python
@app.patch("/api/instances/{instance_id}/tts-mode")
async def set_instance_tts_mode(instance_id: str, request: Request):
    """Set TTS mode for an instance: verbose, muted, or silent."""
    body = await request.json()
    mode = body.get("mode", "verbose")
    if mode not in ("verbose", "muted", "silent"):
        raise HTTPException(status_code=400, detail=f"Invalid mode: {mode}. Must be verbose, muted, or silent")

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT id, tts_voice, notification_sound FROM claude_instances WHERE id = ?", (instance_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Instance not found")

        old_voice = row["tts_voice"]
        old_sound = row["notification_sound"]

        if mode == "silent":
            # Release voice slot
            await db.execute(
                "UPDATE claude_instances SET tts_mode = ?, tts_voice = NULL, notification_sound = NULL WHERE id = ?",
                (mode, instance_id)
            )
        elif mode == "verbose" and not old_voice:
            # Re-assign voice from pool
            cursor2 = await db.execute(
                "SELECT tts_voice FROM claude_instances WHERE status IN ('processing', 'idle') AND tts_voice IS NOT NULL"
            )
            rows = await cursor2.fetchall()
            used_voices = {r[0] for r in rows}
            profile, _ = get_next_available_profile(used_voices)
            await db.execute(
                "UPDATE claude_instances SET tts_mode = ?, tts_voice = ?, notification_sound = ? WHERE id = ?",
                (mode, profile["wsl_voice"], profile["notification_sound"], instance_id)
            )
        else:
            # muted or verbose (with existing voice)
            await db.execute(
                "UPDATE claude_instances SET tts_mode = ? WHERE id = ?",
                (mode, instance_id)
            )
        await db.commit()

    await log_event("tts_mode_changed", instance_id=instance_id, details={"mode": mode})

    return {"status": "ok", "instance_id": instance_id, "mode": mode}
```

**Step 3: Add global tts-mode endpoint**

```python
@app.post("/api/tts/global-mode")
async def set_global_tts_mode(request: Request):
    """Set global TTS mode. Overrides all instances."""
    body = await request.json()
    mode = body.get("mode", "verbose")
    if mode not in ("verbose", "muted", "silent"):
        raise HTTPException(status_code=400, detail=f"Invalid mode: {mode}")

    old_mode = TTS_GLOBAL_MODE["mode"]
    TTS_GLOBAL_MODE["mode"] = mode

    # Update all active instances to match
    async with aiosqlite.connect(DB_PATH) as db:
        if mode == "silent":
            # Release all voice slots
            await db.execute(
                "UPDATE claude_instances SET tts_mode = ?, tts_voice = NULL, notification_sound = NULL WHERE status IN ('processing', 'idle')",
                (mode,)
            )
        elif mode == "verbose" and old_mode == "silent":
            # Re-assign voices to all active instances that lost theirs
            cursor = await db.execute(
                "SELECT id FROM claude_instances WHERE status IN ('processing', 'idle') AND tts_voice IS NULL AND is_subagent = 0"
            )
            rows = await cursor.fetchall()
            used_voices = set()
            for row in rows:
                profile, _ = get_next_available_profile(used_voices)
                await db.execute(
                    "UPDATE claude_instances SET tts_mode = ?, tts_voice = ?, notification_sound = ? WHERE id = ?",
                    (mode, profile["wsl_voice"], profile["notification_sound"], row[0])
                )
                used_voices.add(profile["wsl_voice"])
        else:
            # muted or verbose (voices already assigned)
            await db.execute(
                "UPDATE claude_instances SET tts_mode = ? WHERE status IN ('processing', 'idle')",
                (mode,)
            )
        await db.commit()

    await log_event("tts_global_mode_changed", details={"mode": mode, "old_mode": old_mode})

    return {"status": "ok", "mode": mode, "old_mode": old_mode}
```

**Step 4: Add global mode to health endpoint**

In the `/health` endpoint response dict, add `"tts_global_mode": TTS_GLOBAL_MODE["mode"]` alongside `tts_backend`.

**Step 5: Add global mode to TTS queue status**

In `get_tts_queue_status()` (~line 6231), add `"global_mode": TTS_GLOBAL_MODE["mode"]` to the returned dict.

**Step 6: Verify endpoints work**

Run: `token-restart --mac-only` (restart server to pick up migration + new endpoints)
Then:
```bash
token-ping health                        # Should show tts_global_mode: verbose
token-ping tts/global-mode mode=muted    # Should set global mode
token-ping health                        # Should show tts_global_mode: muted
token-ping tts/global-mode mode=verbose  # Reset
```

**Step 7: Commit**

```bash
git add main.py
git commit -m "feat: add per-instance and global TTS mode endpoints (verbose/muted/silent)"
```

---

### Task 3: Server — Wire `queue_tts` to Respect TTS Mode

**Files:**
- Modify: `main.py:6155-6207` — the `queue_tts()` function

**Step 1: Update `queue_tts` to check tts_mode**

In `queue_tts()`, after the quiet hours check (~line 6160) and after fetching the instance row (~line 6169), add mode checking logic. The updated function should read `tts_mode` from the DB row and also check `TTS_GLOBAL_MODE`. The more restrictive mode wins (silent > muted > verbose).

After the existing `if not row:` check, add:

```python
    # Check TTS mode (per-instance and global, most restrictive wins)
    instance_mode = row["tts_mode"] or "verbose"
    global_mode = TTS_GLOBAL_MODE["mode"]
    # Restrictiveness: silent > muted > verbose
    mode_rank = {"verbose": 0, "muted": 1, "silent": 2}
    effective_mode = max(instance_mode, global_mode, key=lambda m: mode_rank.get(m, 0))

    if effective_mode == "silent":
        logger.info(f"TTS suppressed (silent mode): {message[:80]}")
        return {"success": True, "queued": False, "reason": "silent"}
```

Also update the DB query to include `tts_mode`:

Change:
```python
"SELECT tab_name, tts_voice, notification_sound FROM claude_instances WHERE id = ?"
```
To:
```python
"SELECT tab_name, tts_voice, notification_sound, tts_mode FROM claude_instances WHERE id = ?"
```

For `muted` mode, we still enqueue but skip the voice (sound only). Modify the item creation:

```python
    if effective_mode == "muted":
        # Sound only, no TTS speech
        item = TTSQueueItem(
            instance_id=instance_id,
            message="",  # Empty message = no speech
            voice=voice,
            sound=sound,
            tab_name=tab_name
        )
    else:
        item = TTSQueueItem(
            instance_id=instance_id,
            message=message,
            voice=voice,
            sound=sound,
            tab_name=tab_name
        )
```

**Step 2: Update `tts_queue_worker` to handle empty messages**

In `tts_queue_worker()` (~line 5797), wrap the speech section with a check:

```python
                # Speak the message (only if non-empty, muted mode sends empty)
                if tts_current.message:
                    logger.info(f"TTS worker: speaking ...")
                    # ... existing speak_tts call ...
                else:
                    logger.info(f"TTS worker: muted mode, sound only for {tts_current.instance_id}")
```

**Step 3: Verify TTS mode filtering**

Test with:
```bash
# Set an instance to muted
token-ping instances                     # Get an instance ID
# PATCH /api/instances/<id>/tts-mode with mode=muted
# Then trigger a notification — should hear sound but no speech
```

**Step 4: Commit**

```bash
git add main.py
git commit -m "feat: queue_tts respects per-instance and global TTS mode"
```

---

### Task 4: TUI — Per-Instance Mute Toggle (`m` key)

**Files:**
- Modify: `token-api-tui.py` — key listener, action handler, detail panel

**Step 1: Add `m` key to key listener**

In the key listener section (~line 2599-2630), add before the `elif key == 'f':` block:

```python
                    elif key == 'm':
                        with action_lock:
                            action_queue.append('mute_toggle')
                        update_flag.set()
```

**Step 2: Add helper function to call the tts-mode API**

Near the other TUI API helpers (around line 700), add:

```python
def cycle_instance_tts_mode(instance_id: str, current_mode: str) -> dict | None:
    """Cycle TTS mode: verbose -> muted -> silent -> verbose."""
    mode_cycle = {"verbose": "muted", "muted": "silent", "silent": "verbose"}
    new_mode = mode_cycle.get(current_mode, "muted")
    try:
        resp = requests.patch(
            f"{API_BASE}/api/instances/{instance_id}/tts-mode",
            json={"mode": new_mode},
            timeout=3
        )
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None
```

**Step 3: Add action handler for `mute_toggle`**

In the action processing loop (after the `voice` action handler, ~line 2850), add:

```python
                    elif action == 'mute_toggle' and displayed and table_mode == "instances":
                        if 0 <= selected_index < len(displayed):
                            instance = displayed[selected_index]
                            instance_id = instance.get("id")
                            current_mode = instance.get("tts_mode", "verbose") or "verbose"
                            result = cycle_instance_tts_mode(instance_id, current_mode)
                            if result:
                                new_mode = result.get("mode", "?")
                                mode_display = {"verbose": "Verbose (TTS+Sound)", "muted": "Muted (Sound only)", "silent": "Silent"}
                                unstick_feedback = (time.time(), f"TTS: {mode_display.get(new_mode, new_mode)}")
                                instances_cache = get_instances()
```

**Step 4: Update detail panel to show TTS mode**

In the detail panel rendering (~line 1554), change the voice display line:

Replace:
```python
    lines.append(f"[cyan]Voice:[/cyan] {voice_short}  [dim](profile {profile_num})[/dim]")
```

With:
```python
    tts_mode = instance.get("tts_mode", "verbose") or "verbose"
    if tts_mode == "verbose":
        lines.append(f"[cyan]Voice:[/cyan] {voice_short}  [dim](profile {profile_num})[/dim]")
    elif tts_mode == "muted":
        lines.append(f"[cyan]Voice:[/cyan] [yellow]muted[/yellow]  [dim]({voice_short} reserved)[/dim]")
    else:  # silent
        lines.append(f"[cyan]Voice:[/cyan] [red]silent[/red]")
```

Also update the compact format (~line 1539):

Replace:
```python
        parts.append(f"[cyan]Voice:[/cyan] {voice_short}")
```

With:
```python
        tts_mode = instance.get("tts_mode", "verbose") or "verbose"
        if tts_mode == "verbose":
            parts.append(f"[cyan]Voice:[/cyan] {voice_short}")
        elif tts_mode == "muted":
            parts.append(f"[cyan]Voice:[/cyan] [yellow]muted[/yellow]")
        else:
            parts.append(f"[cyan]Voice:[/cyan] [red]silent[/red]")
```

**Step 5: Verify in TUI**

Launch TUI, select an instance, press `m` — should cycle through verbose/muted/silent. Detail panel should update.

**Step 6: Commit**

```bash
git add token-api-tui.py
git commit -m "feat: add per-instance TTS mute toggle (m key) in TUI"
```

---

### Task 5: TUI — Global Mute Toggle (`M` key)

**Files:**
- Modify: `token-api-tui.py` — key listener, action handler, status bar

**Step 1: Add `M` key to key listener**

In the key listener, add:

```python
                    elif key == 'M':
                        with action_lock:
                            action_queue.append('global_mute_toggle')
                        update_flag.set()
```

**Step 2: Add helper function for global mode**

```python
def cycle_global_tts_mode() -> dict | None:
    """Cycle global TTS mode and return new state."""
    try:
        # Get current mode from health endpoint
        resp = requests.get(f"{API_BASE}/health", timeout=3)
        if resp.status_code == 200:
            current = resp.json().get("tts_global_mode", "verbose")
        else:
            current = "verbose"
    except Exception:
        current = "verbose"

    mode_cycle = {"verbose": "muted", "muted": "silent", "silent": "verbose"}
    new_mode = mode_cycle.get(current, "muted")

    try:
        resp = requests.post(
            f"{API_BASE}/api/tts/global-mode",
            json={"mode": new_mode},
            timeout=5
        )
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None
```

**Step 3: Add action handler for `global_mute_toggle`**

In the action processing loop:

```python
                    elif action == 'global_mute_toggle':
                        result = cycle_global_tts_mode()
                        if result:
                            new_mode = result.get("mode", "?")
                            mode_display = {"verbose": "Verbose", "muted": "Muted", "silent": "Silent"}
                            unstick_feedback = (time.time(), f"Global TTS: {mode_display.get(new_mode, new_mode)}")
                            instances_cache = get_instances()
```

**Step 4: Add global TTS mode to status bar**

In `create_status_bar()` (~line 2040-2050), add a global TTS mode indicator. After the subagent indicator block, add:

```python
    # Global TTS mode indicator
    tts_mode_indicator = ""
    try:
        resp = requests.get(f"{API_BASE}/health", timeout=1)
        if resp.status_code == 200:
            gm = resp.json().get("tts_global_mode", "verbose")
            if gm == "muted":
                tts_mode_indicator = "  [yellow]TTS:muted[/yellow]"
            elif gm == "silent":
                tts_mode_indicator = "  [red]TTS:silent[/red]"
    except Exception:
        pass
```

Wait — the status bar renders every 2 seconds. Making an HTTP call in the status bar renderer would add latency. Better approach: cache the global mode in the TUI.

**Revised Step 4: Cache global TTS mode in TUI**

Add a global variable near the other TUI state globals:

```python
global_tts_mode = "verbose"  # Cached from API
```

Update it when `instances_cache` refreshes (in the main refresh loop), by calling the health endpoint periodically. Or simpler: update it whenever `global_mute_toggle` or `mute_toggle` actions fire, and on initial load.

Add a refresh function:

```python
def refresh_global_tts_mode():
    """Fetch global TTS mode from server."""
    global global_tts_mode
    try:
        resp = requests.get(f"{API_BASE}/health", timeout=1)
        if resp.status_code == 200:
            global_tts_mode = resp.json().get("tts_global_mode", "verbose")
    except Exception:
        pass
```

Call this:
1. On TUI startup (alongside first `get_instances()`)
2. After `global_mute_toggle` action
3. After `mute_toggle` action (global may have changed)

Then in `create_status_bar()`, use the cached value:

```python
    tts_mode_indicator = ""
    if global_tts_mode == "muted":
        tts_mode_indicator = "  [yellow]TTS:muted[/yellow]"
    elif global_tts_mode == "silent":
        tts_mode_indicator = "  [red]TTS:silent[/red]"
```

Append after subagent indicator:

```python
    if tts_mode_indicator:
        text.append_text(Text.from_markup(tts_mode_indicator))
```

**Step 5: Update help text in status bar**

Update the dim help text (~line 2080) to include `m`/`M`:

```python
text.append_text(Text.from_markup("[dim]jk=nav r=rename s=stop d=del m=mute M=global q=quit[/dim]"))
```

**Step 6: Verify**

Launch TUI, press `M` — status bar should show `TTS:muted`, press again for `TTS:silent`, again to clear.

**Step 7: Commit**

```bash
git add token-api-tui.py
git commit -m "feat: add global TTS mute toggle (M key) with status bar indicator"
```

---

### Task 6: Integration Test — End-to-End Verification

**Step 1: Restart server to load all changes**

```bash
token-restart --mac-only
```

**Step 2: Verify database migration**

```bash
agents-db query "PRAGMA table_info(claude_instances)" | grep tts_mode
```

**Step 3: Test per-instance mode API**

```bash
# Get an active instance
agents-db query "SELECT id, tab_name, tts_mode FROM claude_instances WHERE status IN ('processing', 'idle') LIMIT 3"

# Cycle through modes
token-ping instances/<id>/tts-mode mode=muted
token-ping instances/<id>/tts-mode mode=silent
token-ping instances/<id>/tts-mode mode=verbose
```

**Step 4: Test global mode API**

```bash
token-ping tts/global-mode mode=muted
token-ping health  # Should show tts_global_mode: muted
token-ping tts/global-mode mode=verbose  # Reset
```

**Step 5: Test TTS suppression**

```bash
# Set global to muted
token-ping tts/global-mode mode=muted
# Trigger TTS test — should hear sound but no voice
token-ping notify/test
# (wait 10s for sound)

# Set global to silent
token-ping tts/global-mode mode=silent
# Trigger TTS test — should hear nothing
token-ping notify/test

# Reset
token-ping tts/global-mode mode=verbose
```

**Step 6: Test TUI controls**

Launch TUI, verify:
- `m` on instance cycles verbose -> muted -> silent
- `M` cycles global mode, status bar updates
- Detail panel shows correct voice/muted/silent state

**Step 7: Final commit**

If any fixes needed, commit them. Otherwise, tag this as complete.
