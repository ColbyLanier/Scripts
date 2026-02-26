# Shizuku Reliability Plan

**Status**: Logging phase — gathering data before next fix iteration
**Last updated**: 2026-02-26

---

## What We Know

### The Problem
Shizuku (ADB shell service) on Samsung S24+ with One UI 7/8 dies hours after being started, not just on reboot. Previously it survived until device restart.

### Root Cause (Working Theory)
Samsung's One UI 6.1.1+ introduced aggressive background process killing that terminates the Shizuku service process directly — not just the ADB connection. This is a firmware regression confirmed across many S24/Z Fold 6 devices on GitHub issues #612, #1454, #1459.

**Shizuku architecture**: Wireless debugging is only needed to *start* the service. Once running, Shizuku's binder service runs independently. It should survive network changes. The fact that it was surviving network changes before confirms this — Samsung's process killer is the culprit, not the network.

### Why Standard Fixes Fail
Samsung kills the Shizuku **process** directly, not via standard Android battery management. Battery optimization exclusions, Device Care exemptions, and "never sleeping apps" lists don't protect against Samsung's proprietary killer.

### Device Context
- **Device**: Samsung S24+ (US Snapdragon SM-S926U)
- **Rooting**: Not possible — US Snapdragon has permanently locked bootloader
- **One UI version**: TBD (check Settings > About)
- **Wireless debugging**: Leaving ON (safe on Tailscale/home network)

---

## Logging Infrastructure (In Place)

### Shizuku Death Logger macro
- Triggers: screen on + screen off
- Checks `pidof moe.shizuku.privileged.api` → RUNNING or STOPPED
- Logs to `/storage/emulated/0/MacroDroid/logs/shizuku.log`
- Fields per entry: timestamp, SSID, battery %, charge state, screen state
- Notifies + opens Shizuku on death (once per outage, no spam)
- Reports `shizuku_died` / `shizuku_restored` events to Token-API `/phone`

### To read logs
```bash
ssh-phone "cat /storage/emulated/0/MacroDroid/logs/shizuku.log"
# or
ssh-phone "tail -50 /storage/emulated/0/MacroDroid/logs/shizuku.log"
```

### What to look for
- **SSID at death**: Home SSID = killed before network change (Samsung); different SSID = network change correlation
- **Screen state at death**: `screen_off` trigger = Samsung kills on screen lock (known One UI 7+ behavior)
- **Battery at death**: Low battery + Doze mode correlation
- **Time pattern**: Does it die at a consistent interval? (suggests a timeout/background limit)

---

## Mitigations Applied

| Setting | Status | Effect |
|---------|--------|--------|
| Wireless debugging left ON | ✅ Done | Keeps ADB session available for restart |
| Battery > Shizuku > Unrestricted | Do manually | Removes standard battery kill |
| Device Care > Never sleeping apps > Shizuku | Do manually | Removes memory killer (partial) |
| Developer Options > ADB auth timeout revocation | Disable | Prevents auth expiry (days-scale, not hours) |
| Samsung verbose debug logs | Enabled | Will reveal what's killing the process |

---

## Next Steps (Pending Log Data)

1. **Collect 2-3 death events** via the Death Logger macro
2. **Read the log**: `ssh-phone "cat /storage/emulated/0/MacroDroid/logs/shizuku.log"`
3. **Check Samsung debug logs** for process kill reason around the death timestamp
4. **Assess**:
   - If `screen_off` → Samsung screen-lock kill → try Developer Options > USB config workaround (not available on One UI 7, available on 8)
   - If consistent interval (e.g. 30min, 1hr) → likely a background execution limit
   - If correlated with Doze → need to find Samsung-specific Doze exclusion

---

## Shizuku Restart Flow (Manual, Current State)
1. Death Logger notifies + opens Shizuku app
2. Tap "Start" in Shizuku
3. Confirm wireless debugging dialog (Android 14+ requires manual tap)
4. Enforcement resumes

**Total friction**: ~3 taps from notification

---

## Future Options (If Logging Doesn't Lead to Fix)

- **One UI 8 upgrade**: Reportedly adds "Debugging only" USB config option that prevents screen-lock kills — but One UI 8 also removes bootloader unlock permanently
- **Shizuku via USB**: Tether to Mac/laptop, run `adb start-server` — more reliable but requires cable
- **Accept the limitation**: Enforcement gaps when Shizuku is down; server tracks `shizuku_dead` state and adjusts behavior accordingly
