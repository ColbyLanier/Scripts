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
    WORK_GAMING = "work_gaming"
    WORK_GYM = "work_gym"
    GYM = "gym"
    BREAK = "break"
    PAUSE = "pause"


class TimerEvent(Enum):
    BREAK_EXHAUSTED = "break_exhausted"
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
    TimerMode.WORK_SILENCE: (1, 2),   # +30 min/hr
    TimerMode.WORK_MUSIC: (1, 4),     # +15 min/hr
    TimerMode.WORK_VIDEO: (-1, 4),    # -15 min/hr
    TimerMode.WORK_GAMING: (-1, 2),   # -30 min/hr
    TimerMode.WORK_GYM: (3, 4),       # +45 min/hr
    TimerMode.GYM: (1, 1),            # +60 min/hr
}

MAX_IDLE_MS = 10 * 60 * 1000  # 10 minutes
MANUAL_LOCK_DURATION_MS = 30 * 60 * 1000  # 30 minutes


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

    def __init__(self, now_mono_ms: int):
        self._current_mode: TimerMode = TimerMode.WORK_SILENCE
        self._total_work_time_ms: int = 0
        self._total_break_time_ms: int = 0
        self._accumulated_break_ms: int = 0
        self._break_backlog_ms: int = 0
        self._daily_start_date: str | None = None
        self._last_tick_ms: int = now_mono_ms
        self._manual_mode_lock: bool = False
        self._manual_mode_lock_until_ms: int | None = None

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

    # ---- Core methods ----

    def tick(self, now_mono_ms: int, today_date: str) -> TickResult:
        """Main tick: check daily reset, then advance counters."""
        reset_result = self._check_daily_reset(now_mono_ms, today_date)
        if reset_result is not None:
            return reset_result
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

        # Block automatic switches during manual lock
        if is_automatic and mode.value.startswith("work_") and self._manual_mode_lock:
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

        return {
            "current_mode": self._current_mode.value,
            "total_work_time_ms": self._total_work_time_ms,
            "total_break_time_ms": self._total_break_time_ms,
            "accumulated_break_ms": self._accumulated_break_ms,
            "break_backlog_ms": self._break_backlog_ms,
            "daily_start_date": self._daily_start_date,
            "manual_mode_lock": self._manual_mode_lock,
            "manual_mode_lock_remaining_ms": lock_remaining_ms,
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

    def _check_daily_reset(self, now_mono_ms: int, today_date: str) -> TickResult | None:
        """Check and perform daily reset. Returns TickResult if reset happened."""
        if self._daily_start_date is None:
            self._daily_start_date = today_date
            return None

        if self._daily_start_date == today_date:
            return None

        # Day changed — calculate productivity score and reset
        productivity_score = max(0, self._accumulated_break_ms // (1000 * 60))

        result = TickResult()
        result.events.append(TimerEvent.DAILY_RESET)
        result.productivity_score = productivity_score
        result.reset_date = self._daily_start_date

        self._current_mode = TimerMode.WORK_SILENCE
        self._total_work_time_ms = 0
        self._total_break_time_ms = 0
        self._accumulated_break_ms = 0
        self._break_backlog_ms = 0
        self._daily_start_date = today_date
        self._last_tick_ms = now_mono_ms
        self._manual_mode_lock = False
        self._manual_mode_lock_until_ms = None

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
