"""Timer engine v2 — layered composite model, pure logic, no I/O.

All time values are integer milliseconds. Time source is injected via
now_mono_ms parameters for deterministic testing.

State model: three independent layers compose into 6 effective modes.
  Activity:     working | distraction   (from AHK/phone detection)
  Productivity: active | inactive       (from Claude instances / work actions)
  Manual:       None | BREAK | SLEEPING (user-initiated overrides)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Activity(str, Enum):
    WORKING = "working"
    DISTRACTION = "distraction"


class TimerMode(str, Enum):
    WORKING = "working"
    MULTITASKING = "multitasking"
    DISTRACTED = "distracted"
    IDLE = "idle"
    BREAK = "break"
    SLEEPING = "sleeping"


class TimerEvent(Enum):
    BREAK_EXHAUSTED = "break_exhausted"
    IDLE_TIMEOUT = "idle_timeout"
    DISTRACTION_TIMEOUT = "distraction_timeout"
    DAILY_RESET = "daily_reset"
    MODE_CHANGED = "mode_changed"


@dataclass
class TickResult:
    events: list[TimerEvent] = field(default_factory=list)
    old_mode: TimerMode | None = None
    productivity_score: int | None = None
    reset_date: str | None = None


# Break rates as (numerator, denominator) — integer rational arithmetic.
# break_delta_ms = elapsed_ms * numerator // denominator
BREAK_RATE_TABLE: dict[TimerMode, tuple[int, int]] = {
    TimerMode.WORKING: (1, 1),        # +60 min/hr
    TimerMode.MULTITASKING: (0, 1),   # neutral
    TimerMode.IDLE: (0, 1),           # neutral
    TimerMode.DISTRACTED: (-1, 1),    # -60 min/hr (penalty)
    TimerMode.BREAK: (-1, 1),         # -60 min/hr (consuming break)
    TimerMode.SLEEPING: (0, 1),       # neutral
}

# Timeouts
IDLE_TIMEOUT_FROM_WORKING_MS = 7_200_000       # 2 hours
IDLE_TIMEOUT_FROM_MULTITASKING_MS = 120_000    # 2 minutes
DISTRACTION_TIMEOUT_MS = 600_000               # 10 minutes (scrolling/gaming only)
GYM_BOUNTY_MS = 1_800_000                      # 30 minutes

MAX_IDLE_MS = 10 * 60 * 1000                   # 10 min gap detection
MANUAL_LOCK_DURATION_MS = 20 * 60 * 1000       # 20 minutes
DEFAULT_BREAK_BUFFER_MS = 5 * 60 * 1000        # 5 min starting break on reset

# Legacy compat — old code may import these
IDLE_TO_BREAK_TIMEOUT_MS = IDLE_TIMEOUT_FROM_WORKING_MS


def format_timer_time(ms: int) -> str:
    """Format milliseconds as 'Xh Ym' string."""
    is_negative = ms < 0
    abs_ms = abs(ms)
    hours = abs_ms // (1000 * 60 * 60)
    minutes = (abs_ms % (1000 * 60 * 60)) // (1000 * 60)
    sign = "-" if is_negative else ""
    return f"{sign}{hours}h {minutes}m"


class TimerEngine:
    """Encapsulates all timer state and logic.

    Pure computation — no I/O, no globals, deterministically testable.
    State is three independent layers that compose into an effective mode.
    """

    def __init__(self, now_mono_ms: int, reset_hour: int = 7):
        # Layer state
        self._activity: Activity = Activity.WORKING
        self._productivity_active: bool = True
        self._manual_mode: TimerMode | None = None  # BREAK or SLEEPING

        # Counters
        self._total_work_time_ms: int = 0
        self._total_break_time_ms: int = 0
        self._accumulated_break_ms: int = 0
        self._break_backlog_ms: int = 0

        # Timing
        self._daily_start_date: str | None = None
        self._last_tick_ms: int = now_mono_ms
        self._reset_hour: int = reset_hour

        # Manual mode lock (blocks auto-resume from break/sleeping)
        self._manual_mode_lock: bool = False
        self._manual_mode_lock_until_ms: int | None = None

        # Idle tracking
        self._idle_entered_ms: int | None = None
        self._idle_timeout_ms: int = IDLE_TIMEOUT_FROM_WORKING_MS
        self._idle_timeout_exempt: bool = False

        # Distraction tracking
        self._distraction_started_ms: int | None = None
        self._distraction_is_scrolling_gaming: bool = False

    # ---- Read-only properties ----

    @property
    def effective_mode(self) -> TimerMode:
        """Derive effective mode from layers (priority order, top wins)."""
        # 1. Manual override
        if self._manual_mode is not None:
            return self._manual_mode

        # 2. Inactive + distraction → BREAK
        if not self._productivity_active and self._activity == Activity.DISTRACTION:
            return TimerMode.BREAK

        # 3-4. Active + distraction
        if self._productivity_active and self._activity == Activity.DISTRACTION:
            if (self._distraction_is_scrolling_gaming
                    and self._distraction_started_ms is not None
                    and self._last_tick_ms - self._distraction_started_ms >= DISTRACTION_TIMEOUT_MS):
                return TimerMode.DISTRACTED  # 3. scrolling/gaming ≥10min
            return TimerMode.MULTITASKING    # 4. distraction <10min or video

        # 5. Inactive + working → IDLE
        if not self._productivity_active and self._activity == Activity.WORKING:
            return TimerMode.IDLE

        # 6. Active + working → WORKING
        return TimerMode.WORKING

    @property
    def current_mode(self) -> TimerMode:
        """Alias for effective_mode (backward compat)."""
        return self.effective_mode

    @property
    def accumulated_break_ms(self) -> int:
        return self._accumulated_break_ms

    @property
    def break_backlog_ms(self) -> int:
        return self._break_backlog_ms

    @property
    def manual_mode_lock(self) -> bool:
        return self._manual_mode_lock

    @property
    def total_work_time_ms(self) -> int:
        return self._total_work_time_ms

    @property
    def total_break_time_ms(self) -> int:
        return self._total_break_time_ms

    @property
    def daily_start_date(self) -> str | None:
        return self._daily_start_date

    @property
    def idle_timeout_exempt(self) -> bool:
        return self._idle_timeout_exempt

    @idle_timeout_exempt.setter
    def idle_timeout_exempt(self, value: bool) -> None:
        self._idle_timeout_exempt = value

    @property
    def activity(self) -> Activity:
        return self._activity

    @property
    def productivity_active(self) -> bool:
        return self._productivity_active

    @property
    def manual_mode(self) -> TimerMode | None:
        return self._manual_mode

    @property
    def idle_timeout_ms(self) -> int:
        return self._idle_timeout_ms

    @property
    def distraction_started_ms(self) -> int | None:
        return self._distraction_started_ms

    # ---- Layer mutation methods ----

    def set_activity(self, activity: Activity, is_scrolling_gaming: bool, now_mono_ms: int) -> TickResult:
        """Update the activity layer. Called by AHK/phone detection."""
        old_mode = self.effective_mode
        result = self._advance(now_mono_ms)

        if activity == Activity.DISTRACTION:
            if self._activity != Activity.DISTRACTION:
                # Entering distraction — start timer
                self._distraction_started_ms = now_mono_ms
                self._distraction_is_scrolling_gaming = is_scrolling_gaming
            elif is_scrolling_gaming and not self._distraction_is_scrolling_gaming:
                # Upgrading from video to scrolling/gaming — reset timer
                self._distraction_started_ms = now_mono_ms
                self._distraction_is_scrolling_gaming = True
            elif not is_scrolling_gaming and self._distraction_is_scrolling_gaming:
                # Downgrading from scrolling/gaming to video — clear distraction timer
                self._distraction_is_scrolling_gaming = False
                # Don't reset start time, just clear the scrolling flag
        else:
            # Back to working — clear distraction state
            self._distraction_started_ms = None
            self._distraction_is_scrolling_gaming = False

        self._activity = activity

        new_mode = self.effective_mode
        if new_mode != old_mode:
            result.events.append(TimerEvent.MODE_CHANGED)
            result.old_mode = old_mode
        return result

    def set_productivity(self, active: bool, now_mono_ms: int) -> TickResult:
        """Update the productivity layer. Called by Claude activity / work actions."""
        old_mode = self.effective_mode
        result = self._advance(now_mono_ms)

        was_active = self._productivity_active

        if active and not was_active:
            # Becoming active — clear idle state
            self._productivity_active = active
            self._idle_entered_ms = None
        elif not active and was_active:
            # Becoming inactive — parameterize idle timeout based on CURRENT mode
            # (before changing productivity, so effective_mode still reflects active state)
            if old_mode in (TimerMode.MULTITASKING, TimerMode.DISTRACTED):
                self._idle_timeout_ms = IDLE_TIMEOUT_FROM_MULTITASKING_MS
            else:
                self._idle_timeout_ms = IDLE_TIMEOUT_FROM_WORKING_MS
            self._productivity_active = active
            self._idle_entered_ms = now_mono_ms
        else:
            self._productivity_active = active

        new_mode = self.effective_mode
        if new_mode != old_mode:
            result.events.append(TimerEvent.MODE_CHANGED)
            result.old_mode = old_mode
        return result

    def enter_break(self, now_mono_ms: int) -> tuple[bool, TickResult]:
        """Manual break entry. Returns (changed, result)."""
        if self._manual_mode == TimerMode.BREAK:
            return False, TickResult()

        old_mode = self.effective_mode
        result = self._advance(now_mono_ms)

        self._manual_mode = TimerMode.BREAK
        self._manual_mode_lock = True
        self._manual_mode_lock_until_ms = now_mono_ms + MANUAL_LOCK_DURATION_MS

        new_mode = self.effective_mode
        if new_mode != old_mode:
            result.events.append(TimerEvent.MODE_CHANGED)
            result.old_mode = old_mode
        return True, result

    def enter_sleeping(self, now_mono_ms: int) -> tuple[bool, TickResult]:
        """Manual sleeping entry. Returns (changed, result)."""
        if self._manual_mode == TimerMode.SLEEPING:
            return False, TickResult()

        old_mode = self.effective_mode
        result = self._advance(now_mono_ms)

        self._manual_mode = TimerMode.SLEEPING
        self._manual_mode_lock = True
        self._manual_mode_lock_until_ms = now_mono_ms + MANUAL_LOCK_DURATION_MS

        new_mode = self.effective_mode
        if new_mode != old_mode:
            result.events.append(TimerEvent.MODE_CHANGED)
            result.old_mode = old_mode
        return True, result

    def resume(self, now_mono_ms: int) -> tuple[bool, TickResult]:
        """Exit manual mode (break/sleeping). Returns (changed, result)."""
        if self._manual_mode is None:
            return False, TickResult()

        old_mode = self.effective_mode
        result = self._advance(now_mono_ms)

        self._manual_mode = None
        self._manual_mode_lock = False
        self._manual_mode_lock_until_ms = None

        new_mode = self.effective_mode
        if new_mode != old_mode:
            result.events.append(TimerEvent.MODE_CHANGED)
            result.old_mode = old_mode
        return True, result

    def apply_gym_bounty(self, now_mono_ms: int) -> TickResult:
        """Grant +30 min break on gym exit."""
        result = self._advance(now_mono_ms)
        self._apply_break_delta(GYM_BOUNTY_MS, result)
        return result

    # ---- Tick ----

    def tick(self, now_mono_ms: int, today_date: str, current_hour: int | None = None) -> TickResult:
        """Main tick: check daily reset, then advance counters."""
        reset_result = self._check_daily_reset(now_mono_ms, today_date, current_hour)
        if reset_result is not None:
            return reset_result

        # Auto-switch from sleeping to working at reset hour
        if (current_hour is not None
                and current_hour >= self._reset_hour
                and self._manual_mode == TimerMode.SLEEPING):
            old_mode = self.effective_mode
            result = self._advance(now_mono_ms)
            self._manual_mode = None
            self._manual_mode_lock = False
            self._manual_mode_lock_until_ms = None
            new_mode = self.effective_mode
            if new_mode != old_mode:
                result.events.append(TimerEvent.MODE_CHANGED)
                result.old_mode = old_mode
            return result

        return self._advance(now_mono_ms)

    # ---- Serialization ----

    def to_dict(self, now_mono_ms: int) -> dict:
        """Serialize state for DB persistence (snake_case keys)."""
        lock_remaining_ms = 0
        if self._manual_mode_lock and self._manual_mode_lock_until_ms is not None:
            lock_remaining_ms = max(0, self._manual_mode_lock_until_ms - now_mono_ms)

        idle_entered_elapsed_ms = 0
        if self._idle_entered_ms is not None:
            idle_entered_elapsed_ms = max(0, now_mono_ms - self._idle_entered_ms)

        distraction_elapsed_ms = 0
        if self._distraction_started_ms is not None:
            distraction_elapsed_ms = max(0, now_mono_ms - self._distraction_started_ms)

        return {
            "format_version": 2,
            # Layers
            "activity": self._activity.value,
            "productivity_active": self._productivity_active,
            "manual_mode": self._manual_mode.value if self._manual_mode else None,
            # Counters
            "total_work_time_ms": self._total_work_time_ms,
            "total_break_time_ms": self._total_break_time_ms,
            "accumulated_break_ms": self._accumulated_break_ms,
            "break_backlog_ms": self._break_backlog_ms,
            # Timing
            "daily_start_date": self._daily_start_date,
            "manual_mode_lock": self._manual_mode_lock,
            "manual_mode_lock_remaining_ms": lock_remaining_ms,
            # Idle
            "idle_entered_elapsed_ms": idle_entered_elapsed_ms,
            "idle_timeout_ms": self._idle_timeout_ms,
            "idle_timeout_exempt": self._idle_timeout_exempt,
            # Distraction
            "distraction_elapsed_ms": distraction_elapsed_ms,
            "distraction_is_scrolling_gaming": self._distraction_is_scrolling_gaming,
        }

    def to_export_dict(self) -> dict:
        """CamelCase dict for JSON file and API export."""
        return {
            "currentMode": self.effective_mode.value,
            "activity": self._activity.value,
            "productivityActive": self._productivity_active,
            "manualMode": self._manual_mode.value if self._manual_mode else None,
            "breakAvailableSeconds": round(self._accumulated_break_ms / 1000),
            "isInBacklog": self._break_backlog_ms > 0,
            "backlogSeconds": round(self._break_backlog_ms / 1000),
            "workTimeSeconds": round(self._total_work_time_ms / 1000),
            "breakUsedSeconds": round(self._total_break_time_ms / 1000),
        }

    def from_dict(self, data: dict, now_mono_ms: int) -> None:
        """Restore state from DB. Handles both v2 and legacy formats."""
        version = data.get("format_version", 1)

        if version >= 2:
            self._load_v2(data, now_mono_ms)
        else:
            self._load_legacy(data, now_mono_ms)

    def _load_v2(self, data: dict, now_mono_ms: int) -> None:
        """Load v2 format (layered model)."""
        self._activity = Activity(data.get("activity", "working"))
        self._productivity_active = data.get("productivity_active", True)
        manual = data.get("manual_mode")
        self._manual_mode = TimerMode(manual) if manual else None

        self._total_work_time_ms = int(data.get("total_work_time_ms", 0))
        self._total_break_time_ms = int(data.get("total_break_time_ms", 0))
        self._accumulated_break_ms = int(data.get("accumulated_break_ms", 0))
        self._break_backlog_ms = int(data.get("break_backlog_ms", 0))
        self._daily_start_date = data.get("daily_start_date")

        # Manual mode lock
        self._manual_mode_lock = data.get("manual_mode_lock", False)
        remaining = int(data.get("manual_mode_lock_remaining_ms", 0))
        if remaining > 0 and self._manual_mode_lock:
            self._manual_mode_lock_until_ms = now_mono_ms + remaining
        else:
            self._manual_mode_lock = False
            self._manual_mode_lock_until_ms = None

        # Idle state
        idle_elapsed = int(data.get("idle_entered_elapsed_ms", 0))
        if idle_elapsed > 0 and not self._productivity_active:
            self._idle_entered_ms = now_mono_ms - idle_elapsed
        else:
            self._idle_entered_ms = None
        self._idle_timeout_ms = int(data.get("idle_timeout_ms", IDLE_TIMEOUT_FROM_WORKING_MS))
        self._idle_timeout_exempt = data.get("idle_timeout_exempt", False)

        # Distraction state
        distraction_elapsed = int(data.get("distraction_elapsed_ms", 0))
        if distraction_elapsed > 0 and self._activity == Activity.DISTRACTION:
            self._distraction_started_ms = now_mono_ms - distraction_elapsed
        else:
            self._distraction_started_ms = None
        self._distraction_is_scrolling_gaming = data.get("distraction_is_scrolling_gaming", False)

        self._last_tick_ms = now_mono_ms

    def _load_legacy(self, data: dict, now_mono_ms: int) -> None:
        """Migrate v1 (flat mode) format to v2 layers."""
        old_mode = data.get("current_mode", "work_silence")

        # Map old mode → new layers
        if old_mode in ("work_silence", "work_music"):
            self._activity = Activity.WORKING
            self._productivity_active = True
            self._manual_mode = None
        elif old_mode == "work_video":
            self._activity = Activity.DISTRACTION
            self._productivity_active = True
            self._distraction_is_scrolling_gaming = False
            self._distraction_started_ms = now_mono_ms
            self._manual_mode = None
        elif old_mode in ("work_scrolling", "work_gaming"):
            self._activity = Activity.DISTRACTION
            self._productivity_active = True
            self._distraction_is_scrolling_gaming = True
            self._distraction_started_ms = now_mono_ms
            self._manual_mode = None
        elif old_mode == "idle":
            self._activity = Activity.WORKING
            self._productivity_active = False
            self._manual_mode = None
        elif old_mode == "break":
            self._activity = Activity.WORKING
            self._productivity_active = True
            self._manual_mode = TimerMode.BREAK
        elif old_mode == "pause":
            self._activity = Activity.WORKING
            self._productivity_active = False
            self._manual_mode = None
        elif old_mode in ("gym", "work_gym"):
            self._activity = Activity.WORKING
            self._productivity_active = True
            self._manual_mode = None
        elif old_mode == "sleeping":
            self._activity = Activity.WORKING
            self._productivity_active = True
            self._manual_mode = TimerMode.SLEEPING
        else:
            # Unknown mode — default to working
            self._activity = Activity.WORKING
            self._productivity_active = True
            self._manual_mode = None

        # Restore counters
        self._total_work_time_ms = int(data.get("total_work_time_ms", 0))
        self._total_break_time_ms = int(data.get("total_break_time_ms", 0))
        self._accumulated_break_ms = int(data.get("accumulated_break_ms", 0))
        self._break_backlog_ms = int(data.get("break_backlog_ms", 0))
        self._daily_start_date = data.get("daily_start_date")

        # Restore manual mode lock
        self._manual_mode_lock = data.get("manual_mode_lock", False)
        remaining = int(data.get("manual_mode_lock_remaining_ms", 0))
        if remaining > 0 and self._manual_mode_lock:
            self._manual_mode_lock_until_ms = now_mono_ms + remaining
        elif self._manual_mode_lock and data.get("manual_mode_lock_until"):
            import time as _time
            remaining_s = float(data["manual_mode_lock_until"]) - _time.time()
            if remaining_s > 0:
                self._manual_mode_lock_until_ms = now_mono_ms + int(remaining_s * 1000)
            else:
                self._manual_mode_lock = False
                self._manual_mode_lock_until_ms = None
        else:
            self._manual_mode_lock = False
            self._manual_mode_lock_until_ms = None

        # Restore idle state
        idle_elapsed = int(data.get("idle_entered_elapsed_ms", 0))
        if idle_elapsed > 0 and not self._productivity_active:
            self._idle_entered_ms = now_mono_ms - idle_elapsed
        else:
            self._idle_entered_ms = None
        self._idle_timeout_ms = IDLE_TIMEOUT_FROM_WORKING_MS
        self._idle_timeout_exempt = data.get("idle_timeout_exempt", False)

        self._last_tick_ms = now_mono_ms

    def force_daily_reset(self, now_mono_ms: int, today_date: str) -> TickResult:
        """Force a daily reset regardless of date. Used for scheduled reset."""
        productivity_score = max(0, self._accumulated_break_ms // (1000 * 60))

        result = TickResult()
        result.events.append(TimerEvent.DAILY_RESET)
        result.productivity_score = productivity_score
        result.reset_date = self._daily_start_date or today_date

        self._reset_state(now_mono_ms, today_date, with_buffer=False)
        return result

    # ---- Internal ----

    def _advance(self, now_mono_ms: int) -> TickResult:
        """Advance timer counters by elapsed time since last tick."""
        result = TickResult()
        elapsed_ms = now_mono_ms - self._last_tick_ms

        # Idle detection or no time elapsed
        if elapsed_ms > MAX_IDLE_MS or elapsed_ms <= 0:
            self._last_tick_ms = now_mono_ms
            return result

        mode = self.effective_mode

        if mode == TimerMode.WORKING:
            self._total_work_time_ms += elapsed_ms
            rate = BREAK_RATE_TABLE[mode]
            num, den = rate
            break_delta_ms = elapsed_ms * num // den
            self._apply_break_delta(break_delta_ms, result)

        elif mode == TimerMode.MULTITASKING:
            self._total_work_time_ms += elapsed_ms
            # 0:0 neutral — no break delta
            # Check if this tick crosses the distraction timeout (scrolling/gaming only)
            if (self._distraction_is_scrolling_gaming
                    and self._distraction_started_ms is not None):
                was_before = (self._last_tick_ms - elapsed_ms - self._distraction_started_ms) < DISTRACTION_TIMEOUT_MS
                is_after = (now_mono_ms - self._distraction_started_ms) >= DISTRACTION_TIMEOUT_MS
                if was_before and is_after:
                    result.events.append(TimerEvent.DISTRACTION_TIMEOUT)
                    result.events.append(TimerEvent.MODE_CHANGED)
                    result.old_mode = mode

        elif mode == TimerMode.DISTRACTED:
            self._total_work_time_ms += elapsed_ms
            rate = BREAK_RATE_TABLE[mode]
            num, den = rate
            break_delta_ms = elapsed_ms * num // den
            self._apply_break_delta(break_delta_ms, result)

        elif mode == TimerMode.IDLE:
            # No accumulation. Check idle timeout → auto-break.
            if (self._idle_entered_ms is not None
                    and not self._idle_timeout_exempt
                    and now_mono_ms - self._idle_entered_ms >= self._idle_timeout_ms):
                old_mode = self.effective_mode
                self._manual_mode = TimerMode.BREAK
                self._manual_mode_lock = True
                self._manual_mode_lock_until_ms = now_mono_ms + MANUAL_LOCK_DURATION_MS
                self._idle_entered_ms = None
                result.events.append(TimerEvent.IDLE_TIMEOUT)
                result.events.append(TimerEvent.MODE_CHANGED)
                result.old_mode = old_mode

        elif mode == TimerMode.BREAK:
            self._total_break_time_ms += elapsed_ms
            self._accumulated_break_ms -= elapsed_ms
            if self._accumulated_break_ms < 0:
                self._break_backlog_ms += abs(self._accumulated_break_ms)
                self._accumulated_break_ms = 0
                result.events.append(TimerEvent.BREAK_EXHAUSTED)

        # SLEEPING: no accumulation

        self._last_tick_ms = now_mono_ms
        return result

    def _check_daily_reset(self, now_mono_ms: int, today_date: str, current_hour: int | None = None) -> TickResult | None:
        """Check and perform daily reset. Returns TickResult if reset happened."""
        if self._daily_start_date is None:
            self._daily_start_date = today_date
            return None

        if self._daily_start_date == today_date:
            return None

        if current_hour is not None and current_hour < self._reset_hour:
            return None

        # Day changed (and past reset hour) — calculate productivity score and reset
        productivity_score = max(0, self._accumulated_break_ms // (1000 * 60))

        result = TickResult()
        result.events.append(TimerEvent.DAILY_RESET)
        result.productivity_score = productivity_score
        result.reset_date = self._daily_start_date

        self._reset_state(now_mono_ms, today_date, with_buffer=True)
        return result

    def _reset_state(self, now_mono_ms: int, today_date: str, with_buffer: bool) -> None:
        """Reset all state for a new day."""
        self._activity = Activity.WORKING
        self._productivity_active = True
        self._manual_mode = None
        self._total_work_time_ms = 0
        self._total_break_time_ms = 0
        self._accumulated_break_ms = DEFAULT_BREAK_BUFFER_MS if with_buffer else 0
        self._break_backlog_ms = 0
        self._daily_start_date = today_date
        self._last_tick_ms = now_mono_ms
        self._manual_mode_lock = False
        self._manual_mode_lock_until_ms = None
        self._idle_entered_ms = None
        self._idle_timeout_ms = IDLE_TIMEOUT_FROM_WORKING_MS
        self._distraction_started_ms = None
        self._distraction_is_scrolling_gaming = False

    def _apply_break_delta(self, break_delta_ms: int, result: TickResult) -> None:
        """Apply break time change. Handles backlog offset and exhaustion."""
        if self._break_backlog_ms > 0:
            if break_delta_ms >= self._break_backlog_ms:
                remaining = break_delta_ms - self._break_backlog_ms
                self._break_backlog_ms = 0
                self._accumulated_break_ms += remaining
            else:
                self._break_backlog_ms -= break_delta_ms
        else:
            self._accumulated_break_ms += break_delta_ms
            if self._accumulated_break_ms < 0:
                self._break_backlog_ms += abs(self._accumulated_break_ms)
                self._accumulated_break_ms = 0
                result.events.append(TimerEvent.BREAK_EXHAUSTED)
