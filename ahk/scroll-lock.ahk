; Direction-locked smooth scroll for main mouse
; Fixes scroll wheel snapback by locking direction on first event in a sequence.
; While locked, ANY scroll event (up or down) adds velocity in the locked direction.
; Lock releases after 500ms of no scroll input.
;
; Safe with ring-remap.ahk: AHK v2 SendLevel 0 (default) synthetic events
; don't trigger InputLevel 0 hotkeys, so ring's MouseClick output bypasses these.

; ============== CONFIGURATION ==============
SCROLL_LOCK_RELEASE_MS := 200     ; Unlock after this much silence
SCROLL_LOCK_MULTIPLIER := 0.4     ; Velocity added per scroll event
SCROLL_LOCK_DECAY := 0.85         ; Velocity multiplier per tick (lower = faster stop)
SCROLL_LOCK_TICK_MS := 8          ; Timer interval for smooth output
SCROLL_LOCK_MIN_VELOCITY := 0.2   ; Stop threshold
SCROLL_LOCK_WINDOW := 4           ; Rolling window size for consensus check
SCROLL_LOCK_CONSENSUS := 3        ; Break lock if this many of last N are opposite
; ===========================================

; State
global scrollLockDir := 0           ; 0=unlocked, 1=up, -1=down
global scrollLockVelocity := 0.0
global scrollLockAccum := 0.0
global scrollLockTimerRunning := false
global scrollLockHistory := []      ; Rolling window of recent directions

WheelUp::HandleLockedScroll(1)
WheelDown::HandleLockedScroll(-1)

HandleLockedScroll(direction) {
    global scrollLockDir, scrollLockVelocity, scrollLockTimerRunning, scrollLockHistory
    global SCROLL_LOCK_RELEASE_MS, SCROLL_LOCK_MULTIPLIER, SCROLL_LOCK_TICK_MS
    global SCROLL_LOCK_WINDOW, SCROLL_LOCK_CONSENSUS

    ; Lock direction on first event in sequence
    if (scrollLockDir == 0)
        scrollLockDir := direction

    ; Track rolling window of recent directions
    scrollLockHistory.Push(direction)
    if (scrollLockHistory.Length > SCROLL_LOCK_WINDOW)
        scrollLockHistory.RemoveAt(1)

    ; Consensus check: if N of last M inputs are opposite, break and re-lock
    if (scrollLockHistory.Length >= SCROLL_LOCK_WINDOW) {
        oppositeCount := 0
        for d in scrollLockHistory {
            if (d != scrollLockDir)
                oppositeCount++
        }
        if (oppositeCount >= SCROLL_LOCK_CONSENSUS) {
            ; Genuine direction change — reset and re-lock opposite
            scrollLockVelocity := 0
            scrollLockAccum := 0
            scrollLockDir := direction
            scrollLockHistory := [direction]
        }
    }

    ; Reset the lock release timer
    SetTimer(ReleaseScrollLock, -SCROLL_LOCK_RELEASE_MS)

    ; Immediate output on fresh start (no latency on first scroll)
    if (scrollLockVelocity == 0) {
        if (scrollLockDir > 0)
            MouseClick("WheelUp",,, 1)
        else
            MouseClick("WheelDown",,, 1)
    }

    ; Add velocity in LOCKED direction regardless of actual event direction
    scrollLockVelocity += scrollLockDir * SCROLL_LOCK_MULTIPLIER

    ; Start smooth scroll timer if not running
    if (!scrollLockTimerRunning) {
        scrollLockTimerRunning := true
        SetTimer(ScrollLockTick, SCROLL_LOCK_TICK_MS)
    }
}

ScrollLockTick() {
    global scrollLockVelocity, scrollLockAccum, scrollLockTimerRunning
    global SCROLL_LOCK_DECAY, SCROLL_LOCK_MIN_VELOCITY

    scrollLockVelocity *= SCROLL_LOCK_DECAY

    ; Stop when velocity is negligible
    if (scrollLockVelocity > -SCROLL_LOCK_MIN_VELOCITY && scrollLockVelocity < SCROLL_LOCK_MIN_VELOCITY) {
        scrollLockVelocity := 0
        scrollLockAccum := 0
        scrollLockTimerRunning := false
        SetTimer(ScrollLockTick, 0)
        return
    }

    scrollLockAccum += scrollLockVelocity

    ; Output half of accumulation, capped at 5
    if (scrollLockAccum >= 1) {
        outputAmount := Min(5, Max(1, Integer(scrollLockAccum / 2)))
        scrollLockAccum -= outputAmount
        MouseClick("WheelUp",,, outputAmount)
    } else if (scrollLockAccum <= -1) {
        outputAmount := Min(5, Max(1, Integer(-scrollLockAccum / 2)))
        scrollLockAccum += outputAmount
        MouseClick("WheelDown",,, outputAmount)
    }
}

ReleaseScrollLock() {
    global scrollLockDir, scrollLockVelocity, scrollLockAccum, scrollLockTimerRunning

    scrollLockDir := 0
    scrollLockVelocity := 0
    scrollLockAccum := 0
    scrollLockTimerRunning := false
    SetTimer(ScrollLockTick, 0)
}
