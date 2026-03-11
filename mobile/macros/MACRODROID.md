# MacroDroid Macro Inventory

Current state of macros deployed to the phone. Last updated 2026-03-11 (v2 — Shizuku-free).

**Archive:** Pre-v2 backup at `archive/pre-v2-shizuku-era-2026-03-10.mdr`

## Summary

- **Total Macros:** 23
- **Enabled:** 21
- **Disabled:** 2 (Test Discord Fallback, Button)
- **Endpoint:** All phone HTTP endpoints run on MacroDroid HTTP server, port 7777
- **Logging:** Telemetry macros log to `/storage/emulated/0/MacroDroid/logs/telemetry.log`

## Global Variables

| Variable | Type | Purpose |
|----------|------|---------|
| `yt_bg` | bool | YouTube background playback active (PiP/audio-only) |

## Telemetry (2 macros)

Unified open/close macros. Each has N triggers (one per monitored app). Body is the raw MacroDroid trigger name — no hardcoded package names.

| Macro | Triggers | POST Body | Endpoint |
|-------|----------|-----------|----------|
| App Open | X, YouTube, YouTube Revanced, games, Snapchat, etc. | `{"app": "{trigger_that_fired}"}` | `POST /phone/event` |
| App Close | Same apps | `{"app": "{trigger_that_fired}"}` | `POST /phone/event` |

**Trigger name format** (what `{trigger_that_fired}` produces):
- `Application Launched (X)` → server parses as `twitter`, action `open`
- `Application Closed (YouTube)` → server parses as `youtube`, action `close`

**Fallback:** If server returns non-200, macro posts same body to Discord #fallback webhook directly.

**Server-side map** (`MACRODROID_TRIGGER_APP_MAP` in `token-api/main.py`):
```
"x" → "twitter"
"youtube" → "youtube"
"thronefall" / "slice & dice" / etc. → "game"
"spotify" → "spotify"
```

### YouTube Background Audio (`yt_bg`)

YouTube playing audio in the background (PiP / audio-only) is tracked separately. When YouTube leaves the foreground and music is still playing, `yt_bg` is set to `true`. Music start/stop events with `yt_bg==true` re-report open/close. Spotify opening clears `yt_bg` (new media source takes over).

## Token-Ping (1 macro)

Local HTTP relay for macro parameterization. MacroDroid's `run_macro` action doesn't support passing parameters. Token-Ping solves this: any macro can call another macro with parameters by hitting a local HTTP endpoint.

| Macro | Endpoint | Purpose |
|-------|----------|---------|
| Token-Ping | `/Token-Ping` | Receives `endpoint`, `method`, body via query params + POST body; forwards to Token-API; falls back to Discord webhook on failure |

**Pattern:**
```
Caller: HTTP GET/POST → localhost:7777/Token-Ping?endpoint=/phone/event&method=POST
        POST body = {"app": "Application Launched (X)"}

Token-Ping: reads {http_param=endpoint}, {http_param=method}, {http_request_body}
            → forwards to Token-API
            → on failure: posts to Discord #fallback webhook
```

## Enforcement (2 macros)

| Macro | Endpoint | Purpose |
|-------|----------|---------|
| Enforce Cascade v2 | `/enforce?level=<1-5>&app=<name>` | Server-pushed escalation, no Shizuku |
| Discord Fallback v2 | (notification trigger) | Detects Discord notification containing `POST /phone/enforce`, relays to localhost |

### Enforce Cascade v2

Levels triggered by Token-API on a timer. Cascade stops when `app_close` telemetry received.

| Level | Action |
|-------|--------|
| 1 | Persistent notification + vibrate |
| 2 | Loud notification + vibrate + TTS "Close the app now" |
| 3 | Notification spam (3×) + vibrate + TTS "Final warning" |
| 4 | Launch Spotify + play (dopamine redirect) |
| 5 | Pavlok zap via Termux:Tasker `pavlok.sh zap 30` |

### Discord Fallback v2

Trigger: Discord app notification containing `POST /phone/enforce`.

Flow:
1. Notification received from Discord
2. Shell script parses `level` and `app` from notification text
3. `curl localhost:7777/enforce?level=N&app=X`
4. ACK: `POST /phone/event {"event": "discord_fallback_received"}`

**Security:** Only `POST /phone/enforce` messages are relayed. No arbitrary execution.

## Geofence (6 macros)

Location entry/exit events. Post via Token-Ping pattern to Token-API `/phone/event`. Server parses trigger name and routes to `/api/location` internally.

| Macro | Trigger | Trigger Name Sent |
|-------|---------|-------------------|
| Geofence Home Enter | Enter Home zone | `Geofence Entry (Home)` |
| Geofence Home Exit | Exit Home zone | `Geofence Exit (Home)` |
| Geofence Gym Enter | Enter Gym zone | `Geofence Entry (Gym)` |
| Geofence Gym Exit | Exit Gym zone | `Geofence Exit (Gym)` |
| Campus Enter | Enter Campus zone | `Geofence Entry (Campus)` |
| Campus Exit | Exit Campus zone | `Geofence Exit (Campus)` |

**Geofence IDs:**
- Home: `1e4e4f0d-ccd8-40a2-b84a-6027a2843cb8` (33.41694, -111.92196) r=20m
- Gym: `7fa61f1d-fcc7-4ffc-8b83-0e5b6ba0e0dd` (33.42584, -111.91496) r=150m

## Endpoints (4 macros)

| Macro | Endpoint | Purpose |
|-------|----------|---------|
| Heartbeat | `/heartbeat` | Reachability check, returns status JSON |
| /notify | `/notify?notification_text=X&tts_text=Y` | Show notification + speak TTS |
| List Exports API | `/list-exports` | Export .mdr and respond with file list |
| sshd | (boot trigger) | Start Termux sshd on boot |

### /notify

```
GET /notify?notification_text=Close+the+app&tts_text=Close+the+app+now
```
Both params optional. `notification_text` shown on screen (keep short). `tts_text` spoken by MacroDroid TTS engine.

## System (2 macros)

| Macro | Trigger | Purpose |
|-------|---------|---------|
| Boot Startup | Device boot | Start sshd, notify user |
| Phone Health | Every 15 min | POST heartbeat to server |

## Audio / Misc (7 macros)

| Macro | Purpose |
|-------|---------|
| BT Disconnect XM5 | HTTP `/bt-disconnect` — disconnect WF-1000XM5 |
| Change song ⏮️⏭️ | Swipe gestures — next/prev track on locked screen |
| Zappa | (media control) |
| Claude notifications | Notification forwarding to server |
| Potential bluetooth device priority | Placeholder (blocked by BT permissions) |
| Test Discord Fallback ✗ | Send test enforcement message to webhook — disabled after testing |
| Button ✗ | Test macro — disabled |

## Telemetry Log

**File:** `/storage/emulated/0/MacroDroid/logs/telemetry.log`

```
{system_time} INFO app-open TRIGGERED app=twitter
{system_time} INFO app-open HTTP code=200
{system_time} INFO app-close TRIGGERED app=youtube
{system_time} INFO discord-fallback TRIGGERED notif=POST /phone/enforce {...}
{system_time} INFO enforce-cascade TRIGGERED level=2 app=twitter
```

**Access:**
```bash
ssh-phone "tail -50 /storage/emulated/0/MacroDroid/logs/telemetry.log"
curl http://100.102.92.24:7777/logs  # last 200 lines via HTTP
```

## YAML Sources

v2 specs in `~/Scripts/mobile/macros/`:
- `v2-app-open.yaml` — App Open (hardcoded body now superseded by trigger_that_fired on phone)
- `v2-app-close.yaml` — App Close
- `v2-discord-fallback.yaml` — Discord Fallback v2
- `v2-enforce-cascade.yaml` — Enforce Cascade v2
- `test-discord-fallback.yaml` — Test macro (superseded)

Legacy v1 specs preserved for reference (no longer deployed):
- `twitter-open.yaml`, `twitter-close.yaml`, `enforce.yaml`, etc.

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
