# MacroDroid Macro Inventory

Current state of macros deployed to the phone.

## Summary

- **Total Macros:** 22
- **Enabled:** 20
- **Disabled:** 2 (Twitter Management, Games Management - local fallback only)

## Telemetry (6 macros)

Report app events to server at `POST /phone`.

| Macro | Trigger | POST Body | Fallback |
|-------|---------|-----------|----------|
| Twitter Open | X app opened | `{"app": "twitter", "action": "open"}` | Yes |
| Twitter Close | X app closed | `{"app": "twitter", "action": "close"}` | No |
| YouTube Open | YouTube opened | `{"app": "youtube", "action": "open"}` | Yes |
| YouTube Close | YouTube closed | `{"app": "youtube", "action": "close"}` | No |
| Games Open | Game opened | `{"app": "game", "action": "open"}` | Yes |
| Games Close | Game closed | `{"app": "game", "action": "close"}` | No |

**Fallback behavior:** If server returns non-200, triggers local management and enables all local fallback macros.

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

## Enforcement (6 macros)

| Macro | Status | Trigger | Purpose |
|-------|--------|---------|---------|
| Enforce | ✓ | HTTP `/enforce` | Server-pushed enable/disable |
| Enable Local Fallback | ✓ | HTTP `/enable-local` + run_macro | Enable local management |
| Disable Local Fallback | ✓ | HTTP `/disable-local` | Disable local management |
| Twitter Management | ✗ | X app opened | Local fallback enforcement |
| Games Management | ✗ | Game opened | Local fallback enforcement |
| Youtube management | ✓ | Various | Time/geofence based control |

### Enforce Endpoint

Accepts `?action=disable&app=<name>` or `?action=enable&app=<name>`:
- `app=twitter` → X (com.twitter.android)
- `app=youtube` → YouTube + ReVanced (com.google.android.youtube, app.revanced.android.youtube)
- `app=game` → Thronefall, Slice & Dice, 20 Minutes Till Dawn, OneBit Adventure

## Endpoints (2 macros)

| Macro | Endpoint | Purpose |
|-------|----------|---------|
| List Exports API | `/list-exports` | List available .mdr exports |
| Claude notifications | `/notify` | Show notification with vibrate |

## Other (4 macros)

| Macro | Status | Purpose |
|-------|--------|---------|
| Spotify start | ✓ | Auto-start Spotify |
| Youtube Toggle | ✓ | Toggle YouTube state |
| Youtube Enable | ✓ | Enable YouTube apps |
| Youtube Disable | ✓ | Sunrise-triggered YouTube control |
| YouTube Gym Enable | ✓ | Enable YouTube at gym |
| YouTube Gym Disable | ✓ | Disable YouTube leaving gym |
| Change song | ✓ | Media control via swipe |
| Server Poll | ✓ | Periodic server health check |

## App Package References

| App | Package |
|-----|---------|
| X (Twitter) | `com.twitter.android` |
| YouTube | `com.google.android.youtube` |
| YouTube ReVanced | `app.revanced.android.youtube` |
| Thronefall | `com.doghowlgames.thronefall` |
| Slice & Dice | `com.com.tann.dice` |
| 20 Minutes Till Dawn | `com.Flanne.MinutesTillDawn.roguelike.shooting.gp` |
| OneBit Adventure | `com.GalacticSlice.OneBitAdventure` |

## YAML Sources

All macro specs are in `/home/token/Scripts/mobile/macros/*.yaml`:
- `twitter-open.yaml`, `twitter-close.yaml`
- `youtube-open.yaml`, `youtube-close.yaml`
- `games-open.yaml`, `games-close.yaml`
- `geofence-home-enter.yaml`, `geofence-home-exit.yaml`
- `geofence-gym-enter.yaml`, `geofence-gym-exit.yaml`
- `enforce.yaml`
- `enable-local-fallback.yaml`, `disable-local-fallback.yaml`
- `list-exports.yaml`
