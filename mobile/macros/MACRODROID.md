# MacroDroid Macro Inventory

Current state of macros deployed to the phone.

## Summary

- **Total Macros:** 35
- **Enabled:** 33
- **Disabled:** 2 (Twitter Management, Games Management - local fallback only)
- **Logging:** All telemetry/geofence/enforce/heartbeat macros log to `/storage/emulated/0/MacroDroid/logs/telemetry.log`

## Global Variables

| Variable | Type | Purpose |
|----------|------|---------|
| `yt_bg` | bool | YouTube background playback active (PiP/audio) |
| `Connection` | bool | Server connectivity state |
| `Revanced` | bool | ReVanced variant tracking |

## Telemetry (9 macros)

Report app events to server at `POST /phone`.

| Macro | Trigger | POST Body | Notes |
|-------|---------|-----------|-------|
| Twitter Open | X app opened | `{"app": "twitter", "action": "open"}` | Fallback on server error |
| Twitter Close | X app closed | `{"app": "twitter", "action": "close"}` | |
| YouTube Open | YouTube opened | `{"app": "youtube", "action": "open"}` | Clears `yt_bg`, fallback + reconnect logic |
| YouTube Close | YouTube closed | `{"app": "youtube", "action": "close"}` | PiP detection: if music playing, sets `yt_bg=true` and waits |
| Youtube Play | Music starts (bg) | (runs YouTube Open) | Constraint: `yt_bg==true` |
| Youtube Pause | Music stops (bg) | (runs YouTube Close) | Constraint: `yt_bg==true` |
| Games Open | Game opened | `{"app": "game", "action": "open"}` | Fallback on server error |
| Games Close | Game closed | `{"app": "game", "action": "close"}` | |
| Spotify open | Spotify opened | (clears `yt_bg`) | New media source takes over |

**YouTube PiP/Background Detection:**
When YouTube leaves foreground, if music is still playing (PiP or background audio), the Close macro sets `yt_bg=true` and waits for music to stop. The Play/Pause macros use `MusicPlaying` trigger + `yt_bg` constraint to re-report open/close events during background playback.

**Fallback behavior:** If server returns non-200 on open, triggers local management. If server reconnects (Connection==response_code), disables local fallback.

## Geofence (4 macros)

Report location events to server.

| Macro | Trigger | POST Body |
|-------|---------|-----------|
| Geofence Home Enter | Enter Home (20m) | `{"location": "home", "action": "enter"}` |
| Geofence Home Exit | Exit Home | `{"location": "home", "action": "exit"}` |
| Geofence Gym Enter | Enter Gym (150m) | `{"location": "gym", "action": "enter"}` |
| Geofence Gym Exit | Exit Gym | `{"location": "gym", "action": "exit"}` |

**Geofence IDs:**
- Home: `1e4e4f0d-...` (33.41694, -111.92196) r=20m
- Gym: `7fa61f1d-fcc7-4ffc-8b83-0e5b6ba0e0dd` (33.42584, -111.91496) r=150m

## Enforcement (7 macros)

| Macro | Status | Purpose |
|-------|--------|---------|
| Enforce | ✓ | Server-pushed enable/disable via HTTP `/enforce` |
| Enable Local Fallback | ✓ | Enable local management macros |
| Disable Local Fallback | ✓ | Disable local management (server back) |
| Twitter Management | ✗ | Local fallback enforcement for X |
| Games Management | ✗ | Local fallback enforcement for games |
| Youtube Enable | ✓ | Enable YouTube apps |
| Youtube Disable | ✓ | Disable YouTube apps |

### Enforce Endpoint

Accepts `?action=disable&app=<name>` or `?action=enable&app=<name>`:
- `app=twitter` → X (com.twitter.android)
- `app=youtube` → YouTube + ReVanced (com.google.android.youtube, app.revanced.android.youtube)
- `app=game` → Thronefall, Slice & Dice, 20 Minutes Till Dawn, OneBit Adventure

## Endpoints (3 macros)

| Macro | Endpoint | Purpose |
|-------|----------|---------|
| Claude notifications | `/notify` | Show notification with vibrate |
| Heartbeat | `/heartbeat` | Server reachability check (returns status JSON) |
| Server Poll | Every 10min | GET /health, disables local fallback if server is up |

## Automation (2 macros)

| Macro | Endpoint | Purpose |
|-------|----------|---------|
| List Exports API | `/list-exports` | Export .mdr and respond |
| Pavlok Endpoint | `/pavlok` | Trigger Pavlok stimulus |

## System (3 macros)

| Macro | Trigger | Purpose |
|-------|---------|---------|
| Boot Start SSHD | Device boot | Start Termux sshd via Termux:Tasker plugin |
| Log Viewer | HTTP `/logs` | Serve last 200 lines of telemetry log |
| Log Rotate | Daily 3 AM | Trim telemetry.log to 1000 lines if >2000 |

## Telemetry Logging

All telemetry, geofence, enforce, and heartbeat macros append structured log lines via shell actions.

**Log file:** `/storage/emulated/0/MacroDroid/logs/telemetry.log`

**Format:** `<unix_epoch> <LEVEL> <macro-name> <EVENT> [detail]`

**Examples:**
```
1740067200 INFO twitter-open TRIGGERED
1740067200 INFO twitter-open HTTP_RESULT code=200
1740067200 WARN twitter-open FALLBACK server_unreachable
1740067200 INFO enforce RECEIVED action=disable app=twitter
1740067200 INFO heartbeat PING
1740067200 INFO geofence-home ENTER
1740067200 INFO log-rotate ROTATED from=2500 to=1000
```

**Retrieval:**
```bash
# Via SSH
sshp "tail -50 /storage/emulated/0/MacroDroid/logs/telemetry.log"

# Via HTTP endpoint (last 200 lines)
curl http://<phone-ip>:7777/logs
```

## Music & Audio (2 macros)

| Macro | Trigger | Purpose |
|-------|---------|---------|
| Change song | Swipe gestures | Next/prev track on locked screen |
| Spotify start | - | Auto-start Spotify |

## Uncategorized / Test (5 macros)

| Macro | Notes |
|-------|-------|
| Youtube Toggle | Toggle YouTube state |
| Twitter | Test/legacy |
| Tele | Test macro (media button trigger) |
| Hello worl | Test macro |
| Bluetooth priority | Placeholder (blocked by permissions) |

## App Package References

| App | Package |
|-----|---------|
| X (Twitter) | `com.twitter.android` |
| YouTube | `com.google.android.youtube` |
| YouTube ReVanced | `app.revanced.android.youtube` |
| Spotify | `com.spotify.music` |
| Thronefall | `com.doghowlgames.thronefall` |
| Slice & Dice | `com.com.tann.dice` |
| 20 Minutes Till Dawn | `com.Flanne.MinutesTillDawn.roguelike.shooting.gp` |
| OneBit Adventure | `com.GalacticSlice.OneBitAdventure` |

## YAML Sources

All macro specs are in `~/Scripts/mobile/macros/*.yaml` (Mac) or `/home/token/Scripts/mobile/macros/*.yaml` (WSL):
- `twitter-open.yaml`, `twitter-close.yaml`
- `youtube-open.yaml`, `youtube-close.yaml`
- `youtube-play.yaml`, `youtube-pause.yaml`
- `spotify-open.yaml`
- `games-open.yaml`, `games-close.yaml`
- `geofence-home-enter.yaml`, `geofence-home-exit.yaml`
- `geofence-gym-enter.yaml`, `geofence-gym-exit.yaml`
- `enforce.yaml`
- `enable-local-fallback.yaml`, `disable-local-fallback.yaml`
- `list-exports.yaml`
- `heartbeat.yaml`, `pavlok.yaml`, `boot-sshd.yaml`
- `log-viewer.yaml`, `log-rotate.yaml`
