"""Timer engine — pure logic, no I/O.

All time values are integer milliseconds. Time source is injected via
now_mono_ms parameters for deterministic testing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class TimerMode(str, Enum):
    WORK_SILENCE = "work_silence"
    WORK_MUSIC = "work_music"
    WORK_VIDEO = "work_video"
    WORK_SCROLLING = "work_scrolling"
    WORK_GAMING = "work_gaming"
    WORK_GYM = "work_gym"
    GYM = "gym"
    IDLE = "idle"
    BREAK = "break"
    PAUSE = "pause"
    SLEEPING = "sleeping"


class TimerEvent(Enum):
    BREAK_EXHAUSTED = "break_exhausted"
    IDLE_TIMEOUT = "idle_timeout"
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
    TimerMode.WORK_SILENCE: (1, 4),   # +15 min/hr (parity with music)
    TimerMode.WORK_MUSIC: (1, 4),     # +15 min/hr
    TimerMode.WORK_VIDEO: (-1, 4),    # -15 min/hr
    TimerMode.WORK_SCROLLING: (-1, 2),  # -30 min/hr (same as gaming)
    TimerMode.WORK_GAMING: (-1, 2),   # -30 min/hr
    TimerMode.WORK_GYM: (3, 4),       # +45 min/hr
    TimerMode.GYM: (1, 1),            # +60 min/hr
    TimerMode.IDLE: (0, 1),             # 0 min/hr - neutral, no accumulation
    TimerMode.SLEEPING: (0, 1),       # 0 min/hr - neutral, doesn't impact break
}

MAX_IDLE_MS = 10 * 60 * 1000  # 10 minutes
IDLE_TO_BREAK_TIMEOUT_MS = 15 * 60 * 1000  # 15 minutes idle → auto-break
MANUAL_LOCK_DURATION_MS = 20 * 60 * 1000  # 20 minutes
DEFAULT_BREAK_BUFFER_MS = 5 * 60 * 1000   # 5 minutes - starting break on reset


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
    """

    def __init__(self, now_mono_ms: int, reset_hour: int = 9):
        self._current_mode: TimerMode = TimerMode.WORK_SILENCE
        self._total_work_time_ms: int = 0
        self._total_break_time_ms: int = 0
        self._accumulated_break_ms: int = 0
        self._break_backlog_ms: int = 0
        self._daily_start_date: str | None = None
        self._last_tick_ms: int = now_mono_ms
        self._manual_mode_lock: bool = False
        self._manual_mode_lock_until_ms: int | None = None
        self._idle_entered_ms: int | None = None
        self._idle_timeout_exempt: bool = False
        self._reset_hour: int = reset_hour  # Hour (0-23) when daily reset happens

    # ---- Read-only properties ----

    @property
    def current_mode(self) -> TimerMode:
        return self._current_mode

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

    # ---- Core methods ----

    def tick(self, now_mono_ms: int, today_date: str, current_hour: int | None = None) -> TickResult:
        """Main tick: check daily reset, then advance counters.
        
        Args:
            now_mono_ms: Monotonic timestamp in milliseconds
            today_date: Today's date as YYYY-MM-DD string
            current_hour: Current hour (0-23). If provided along with date change,
                         only resets if hour >= reset_hour (default 9).
        """
        reset_result = self._check_daily_reset(now_mono_ms, today_date, current_hour)
        if reset_result is not None:
            return reset_result
        
        # Auto-switch from sleeping to work at reset hour
        if (current_hour is not None 
            and current_hour >= self._reset_hour 
            and self._current_mode == TimerMode.SLEEPING):
            return self.set_mode(TimerMode.WORK_SILENCE, is_automatic=True, now_mono_ms=now_mono_ms)[1]
        
        return self._advance(now_mono_ms)

    def set_mode(
        self, mode: TimerMode, is_automatic: bool, now_mono_ms: int
    ) -> tuple[bool, TickResult]:
        """Change timer mode.

        Returns (changed, tick_result). The tick_result includes events
        from finalizing the previous period plus MODE_CHANGED.
        """
        if self._current_mode == mode:
            return False, TickResult()

        # Block automatic switches during manual lock (work_* and idle)
        if is_automatic and (mode.value.startswith("work_") or mode == TimerMode.IDLE) and self._manual_mode_lock:
            if (
                self._manual_mode_lock_until_ms is not None
                and now_mono_ms < self._manual_mode_lock_until_ms
            ):
                return False, TickResult()
            # Lock expired
            self._manual_mode_lock = False
            self._manual_mode_lock_until_ms = None

        # Finalize current period
        result = self._advance(now_mono_ms)

        old_mode = self._current_mode
        self._current_mode = mode

        # Idle state tracking
        if mode == TimerMode.IDLE:
            self._idle_entered_ms = now_mono_ms
        else:
            self._idle_entered_ms = None

        # Lock management
        if mode.value.startswith("work_"):
            self._manual_mode_lock = False
            self._manual_mode_lock_until_ms = None
        elif mode in (TimerMode.BREAK, TimerMode.PAUSE):
            self._manual_mode_lock = True
            self._manual_mode_lock_until_ms = now_mono_ms + MANUAL_LOCK_DURATION_MS

        result.events.append(TimerEvent.MODE_CHANGED)
        result.old_mode = old_mode
        return True, result

    # ---- Serialization ----

    def to_dict(self, now_mono_ms: int) -> dict:
        """Serialize state for DB persistence (snake_case keys)."""
        lock_remaining_ms = 0
        if self._manual_mode_lock and self._manual_mode_lock_until_ms is not None:
            lock_remaining_ms = max(0, self._manual_mode_lock_until_ms - now_mono_ms)

        idle_entered_remaining_ms = 0
        if self._idle_entered_ms is not None:
            idle_entered_remaining_ms = max(0, now_mono_ms - self._idle_entered_ms)

        return {
            "current_mode": self._current_mode.value,
            "total_work_time_ms": self._total_work_time_ms,
            "total_break_time_ms": self._total_break_time_ms,
            "accumulated_break_ms": self._accumulated_break_ms,
            "break_backlog_ms": self._break_backlog_ms,
            "daily_start_date": self._daily_start_date,
            "manual_mode_lock": self._manual_mode_lock,
            "manual_mode_lock_remaining_ms": lock_remaining_ms,
            "idle_entered_elapsed_ms": idle_entered_remaining_ms,
            "idle_timeout_exempt": self._idle_timeout_exempt,
        }

    def to_export_dict(self) -> dict:
        """CamelCase dict for JSON file and API export."""
        return {
            "currentMode": self._current_mode.value,
            "breakAvailableSeconds": round(self._accumulated_break_ms / 1000),
            "isInBacklog": self._break_backlog_ms > 0,
            "backlogSeconds": round(self._break_backlog_ms / 1000),
            "workTimeSeconds": round(self._total_work_time_ms / 1000),
            "breakUsedSeconds": round(self._total_break_time_ms / 1000),
        }

    def from_dict(self, data: dict, now_mono_ms: int) -> None:
        """Restore state from DB. Resets tick clock to now_mono_ms."""
        self._current_mode = TimerMode(data.get("current_mode", "work_silence"))
        self._total_work_time_ms = int(data.get("total_work_time_ms", 0))
        self._total_break_time_ms = int(data.get("total_break_time_ms", 0))
        self._accumulated_break_ms = int(data.get("accumulated_break_ms", 0))
        self._break_backlog_ms = int(data.get("break_backlog_ms", 0))
        self._daily_start_date = data.get("daily_start_date")
        self._manual_mode_lock = data.get("manual_mode_lock", False)

        # Restore lock: prefer new format (remaining_ms), fall back to old (epoch timestamp)
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
        if idle_elapsed > 0 and self._current_mode == TimerMode.IDLE:
            self._idle_entered_ms = now_mono_ms - idle_elapsed
        else:
            self._idle_entered_ms = None
        self._idle_timeout_exempt = data.get("idle_timeout_exempt", False)

        self._last_tick_ms = now_mono_ms

    # ---- Internal ----

    def _advance(self, now_mono_ms: int) -> TickResult:
        """Advance timer counters by elapsed time since last tick."""
        result = TickResult()
        elapsed_ms = now_mono_ms - self._last_tick_ms

        # Idle detection or no time elapsed
        if elapsed_ms > MAX_IDLE_MS or elapsed_ms <= 0:
            self._last_tick_ms = now_mono_ms
            return result

        mode = self._current_mode

        if mode.value.startswith("work_") or mode == TimerMode.GYM:
            self._total_work_time_ms += elapsed_ms
            rate = BREAK_RATE_TABLE.get(mode)
            if rate is not None:
                num, den = rate
                break_delta_ms = elapsed_ms * num // den
                self._apply_break_delta(break_delta_ms, result)

        elif mode == TimerMode.IDLE:
            # No accumulation. Check idle timeout → auto-break.
            if (self._idle_entered_ms is not None
                    and not self._idle_timeout_exempt
                    and now_mono_ms - self._idle_entered_ms >= IDLE_TO_BREAK_TIMEOUT_MS):
                self._idle_entered_ms = None
                self._current_mode = TimerMode.BREAK
                self._manual_mode_lock = True
                self._manual_mode_lock_until_ms = now_mono_ms + MANUAL_LOCK_DURATION_MS
                result.events.append(TimerEvent.IDLE_TIMEOUT)
                result.events.append(TimerEvent.MODE_CHANGED)
                result.old_mode = TimerMode.IDLE

        elif mode == TimerMode.BREAK:
            self._total_break_time_ms += elapsed_ms
            self._accumulated_break_ms -= elapsed_ms
            if self._accumulated_break_ms < 0:
                self._break_backlog_ms += abs(self._accumulated_break_ms)
                self._accumulated_break_ms = 0
                result.events.append(TimerEvent.BREAK_EXHAUSTED)

        # PAUSE: no accumulation

        self._last_tick_ms = now_mono_ms
        return result

    def _check_daily_reset(self, now_mono_ms: int, today_date: str, current_hour: int | None = None) -> TickResult | None:
        """Check and perform daily reset. Returns TickResult if reset happened.
        
        Resets at _reset_hour (default 9 AM) when date changes.
        """
        if self._daily_start_date is None:
            self._daily_start_date = today_date
            return None

        if self._daily_start_date == today_date:
            return None

        # Date changed - check if we're past the reset hour
        # If current_hour < reset_hour, we haven't hit the reset time yet today
        # (e.g., it's 7 AM but reset_hour is 9, so don't reset yet)
        if current_hour is not None and current_hour < self._reset_hour:
            # Haven't reached reset hour yet today — do NOT update daily_start_date.
            # Keeping it as yesterday ensures the reset fires once we reach reset_hour.
            return None

        # Day changed (and past reset hour) — calculate productivity score and reset
        productivity_score = max(0, self._accumulated_break_ms // (1000 * 60))

        result = TickResult()
        result.events.append(TimerEvent.DAILY_RESET)
        result.productivity_score = productivity_score
        result.reset_date = self._daily_start_date

        self._current_mode = TimerMode.WORK_SILENCE
        self._total_work_time_ms = 0
        self._total_break_time_ms = 0
        self._accumulated_break_ms = DEFAULT_BREAK_BUFFER_MS  # Start with default break
        self._break_backlog_ms = 0
        self._daily_start_date = today_date
        self._last_tick_ms = now_mono_ms
        self._manual_mode_lock = False
        self._manual_mode_lock_until_ms = None
        self._idle_entered_ms = None

        return result

    def force_daily_reset(self, now_mono_ms: int, today_date: str) -> TickResult:
        """Force a daily reset regardless of date. Used for the 9 AM scheduled reset."""
        productivity_score = max(0, self._accumulated_break_ms // (1000 * 60))

        result = TickResult()
        result.events.append(TimerEvent.DAILY_RESET)
        result.productivity_score = productivity_score
        result.reset_date = self._daily_start_date or today_date

        self._current_mode = TimerMode.WORK_SILENCE
        self._total_work_time_ms = 0
        self._total_break_time_ms = 0
        self._accumulated_break_ms = 0
        self._break_backlog_ms = 0
        self._daily_start_date = today_date
        self._last_tick_ms = now_mono_ms
        self._manual_mode_lock = False
        self._manual_mode_lock_until_ms = None
        self._idle_entered_ms = None

        return result

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
