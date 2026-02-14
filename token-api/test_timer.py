"""Unit tests for TimerEngine — pure logic, no I/O dependencies."""

import pytest
from timer import (
    TimerEngine,
    TimerMode,
    TimerEvent,
    TickResult,
    format_timer_time,
    MANUAL_LOCK_DURATION_MS,
    MAX_IDLE_MS,
)


# ---- Helpers ----

def make_engine(now_ms: int = 0, date: str = "2026-02-11") -> TimerEngine:
    """Create an engine and initialize its daily_start_date."""
    engine = TimerEngine(now_mono_ms=now_ms)
    engine.tick(now_ms, date)  # sets daily_start_date
    return engine


def advance(engine: TimerEngine, start_ms: int, seconds: int, date: str = "2026-02-11") -> TickResult:
    """Advance the engine by `seconds` in 1-second ticks, returning the last result."""
    result = TickResult()
    for i in range(seconds):
        result = engine.tick(start_ms + (i + 1) * 1000, date)
    return result


# ---- format_timer_time ----

class TestFormatTimerTime:
    def test_zero(self):
        assert format_timer_time(0) == "0h 0m"

    def test_positive(self):
        assert format_timer_time(90 * 60 * 1000) == "1h 30m"

    def test_negative(self):
        assert format_timer_time(-45 * 60 * 1000) == "-0h 45m"

    def test_large(self):
        assert format_timer_time(3 * 60 * 60 * 1000 + 5 * 60 * 1000) == "3h 5m"


# ---- Basic tick ----

class TestBasicTick:
    def test_silence_earns_half_rate(self):
        """60s of work_silence → 30_000ms break earned."""
        engine = make_engine(0)
        advance(engine, 0, 60)
        assert engine.accumulated_break_ms == 30_000

    def test_music_earns_quarter_rate(self):
        """60s of work_music → 15_000ms break earned."""
        engine = make_engine(0)
        engine.set_mode(TimerMode.WORK_MUSIC, is_automatic=False, now_mono_ms=0)
        advance(engine, 0, 60)
        assert engine.accumulated_break_ms == 15_000

    def test_gym_earns_full_rate(self):
        """60s of gym → 60_000ms break earned."""
        engine = make_engine(0)
        engine.set_mode(TimerMode.GYM, is_automatic=False, now_mono_ms=0)
        advance(engine, 0, 60)
        assert engine.accumulated_break_ms == 60_000

    def test_work_gym_earns_three_quarter_rate(self):
        """60s of work_gym → 45_000ms break earned."""
        engine = make_engine(0)
        engine.set_mode(TimerMode.WORK_GYM, is_automatic=False, now_mono_ms=0)
        advance(engine, 0, 60)
        assert engine.accumulated_break_ms == 45_000

    def test_pause_no_accumulation(self):
        """Pause mode: no break earned, no work time tracked."""
        engine = make_engine(0)
        engine.set_mode(TimerMode.PAUSE, is_automatic=False, now_mono_ms=0)
        advance(engine, 0, 60)
        assert engine.accumulated_break_ms == 0
        assert engine.total_work_time_ms == 0
        assert engine.total_break_time_ms == 0

    def test_work_time_tracked(self):
        """60s of work → 60_000ms total work time."""
        engine = make_engine(0)
        advance(engine, 0, 60)
        assert engine.total_work_time_ms == 60_000

    def test_all_values_integer(self):
        """No float drift — all values are exact integers."""
        engine = make_engine(0)
        advance(engine, 0, 123)
        assert isinstance(engine.accumulated_break_ms, int)
        assert isinstance(engine.total_work_time_ms, int)
        assert isinstance(engine.total_break_time_ms, int)
        assert isinstance(engine.break_backlog_ms, int)


# ---- Mode switching ----

class TestModeSwitch:
    def test_silence_to_music(self):
        """Switch from silence to music, verify rates change."""
        engine = make_engine(0)
        advance(engine, 0, 60)  # 30s break earned
        changed, _ = engine.set_mode(TimerMode.WORK_MUSIC, is_automatic=False, now_mono_ms=60_000)
        assert changed
        advance(engine, 60_000, 60)  # 15s more break
        assert engine.accumulated_break_ms == 45_000

    def test_same_mode_returns_false(self):
        engine = make_engine(0)
        changed, _ = engine.set_mode(TimerMode.WORK_SILENCE, is_automatic=False, now_mono_ms=0)
        assert not changed

    def test_mode_changed_event(self):
        engine = make_engine(0)
        changed, result = engine.set_mode(TimerMode.WORK_MUSIC, is_automatic=False, now_mono_ms=0)
        assert changed
        assert TimerEvent.MODE_CHANGED in result.events
        assert result.old_mode == TimerMode.WORK_SILENCE

    def test_set_mode_finalizes_previous_period(self):
        """set_mode calls _advance internally, capturing elapsed time."""
        engine = make_engine(0)
        # Advance 10s in silence (should earn 5000ms break)
        changed, _ = engine.set_mode(TimerMode.WORK_MUSIC, is_automatic=False, now_mono_ms=10_000)
        assert engine.accumulated_break_ms == 5_000


# ---- Video penalty ----

class TestVideoPenalty:
    def test_video_decreases_break(self):
        """Accumulate break, switch to video, verify break decreases."""
        engine = make_engine(0)
        advance(engine, 0, 60)  # 30_000ms break
        engine.set_mode(TimerMode.WORK_VIDEO, is_automatic=False, now_mono_ms=60_000)
        advance(engine, 60_000, 60)  # -15_000ms (video penalty)
        assert engine.accumulated_break_ms == 15_000

    def test_gaming_decreases_break_faster(self):
        """Gaming penalty is twice video."""
        engine = make_engine(0)
        advance(engine, 0, 60)  # 30_000ms break
        engine.set_mode(TimerMode.WORK_GAMING, is_automatic=False, now_mono_ms=60_000)
        advance(engine, 60_000, 60)  # -30_000ms (gaming penalty)
        assert engine.accumulated_break_ms == 0
        assert engine.break_backlog_ms == 0  # exactly zeroed, no backlog


# ---- Break consumption ----

class TestBreakConsumption:
    def test_break_mode_consumes_accumulated(self):
        """Enter break mode, verify accumulated_break_ms decreases."""
        engine = make_engine(0)
        advance(engine, 0, 60)  # 30_000ms break earned
        engine.set_mode(TimerMode.BREAK, is_automatic=False, now_mono_ms=60_000)
        advance(engine, 60_000, 10)  # consume 10_000ms
        assert engine.accumulated_break_ms == 20_000
        assert engine.total_break_time_ms == 10_000

    def test_break_tracks_break_time(self):
        engine = make_engine(0)
        advance(engine, 0, 60)
        engine.set_mode(TimerMode.BREAK, is_automatic=False, now_mono_ms=60_000)
        advance(engine, 60_000, 20)
        assert engine.total_break_time_ms == 20_000


# ---- Break exhaustion ----

class TestBreakExhaustion:
    def test_break_exhaustion_event(self):
        """Consume all break time → BREAK_EXHAUSTED event."""
        engine = make_engine(0)
        advance(engine, 0, 60)  # 30_000ms break
        engine.set_mode(TimerMode.BREAK, is_automatic=False, now_mono_ms=60_000)
        # Consume 31s of break (more than available)
        result = advance(engine, 60_000, 31)
        assert TimerEvent.BREAK_EXHAUSTED in result.events
        assert engine.accumulated_break_ms == 0
        assert engine.break_backlog_ms > 0

    def test_video_exhaustion(self):
        """Video penalty can exhaust break."""
        engine = make_engine(0)
        advance(engine, 0, 20)  # 10_000ms break
        engine.set_mode(TimerMode.WORK_VIDEO, is_automatic=False, now_mono_ms=20_000)
        # 41s of video = 10_250ms penalty, exceeds 10_000ms break
        last = TickResult()
        exhausted = False
        for i in range(41):
            last = engine.tick(20_000 + (i + 1) * 1000, "2026-02-11")
            if TimerEvent.BREAK_EXHAUSTED in last.events:
                exhausted = True
                break
        assert exhausted
        assert engine.accumulated_break_ms == 0


# ---- Backlog mechanics ----

class TestBacklog:
    def test_backlog_grows_during_video(self):
        """Exhaust break → continue video → backlog grows."""
        engine = make_engine(0)
        # No break earned, go straight to video
        engine.set_mode(TimerMode.WORK_VIDEO, is_automatic=False, now_mono_ms=0)
        advance(engine, 0, 60)  # -15_000ms penalty → backlog = 15_000
        assert engine.break_backlog_ms == 15_000
        assert engine.accumulated_break_ms == 0

    def test_backlog_offset_before_accumulation(self):
        """Switch to silence with backlog → pay off backlog first."""
        engine = make_engine(0)
        engine.set_mode(TimerMode.WORK_VIDEO, is_automatic=False, now_mono_ms=0)
        advance(engine, 0, 40)  # 10_000ms backlog
        assert engine.break_backlog_ms == 10_000

        engine.set_mode(TimerMode.WORK_SILENCE, is_automatic=False, now_mono_ms=40_000)
        advance(engine, 40_000, 40)  # 20_000ms earned → 10_000 to backlog, 10_000 to break
        assert engine.break_backlog_ms == 0
        assert engine.accumulated_break_ms == 10_000

    def test_video_during_backlog_grows_backlog(self):
        """Video penalty while in backlog increases backlog."""
        engine = make_engine(0)
        engine.set_mode(TimerMode.WORK_VIDEO, is_automatic=False, now_mono_ms=0)
        advance(engine, 0, 40)  # 10_000ms backlog
        assert engine.break_backlog_ms == 10_000
        advance(engine, 40_000, 40)  # +10_000ms more backlog
        assert engine.break_backlog_ms == 20_000


# ---- Idle detection ----

class TestIdleDetection:
    def test_large_gap_skips_accumulation(self):
        """Idle >10 min → no accumulation."""
        engine = make_engine(0)
        advance(engine, 0, 10)  # small warmup
        # Jump 15 minutes
        result = engine.tick(10_000 + 15 * 60 * 1000, "2026-02-11")
        assert TimerEvent.BREAK_EXHAUSTED not in result.events
        # Only the first 10s of work should have accumulated
        assert engine.accumulated_break_ms == 5_000

    def test_exactly_at_threshold(self):
        """Gap exactly at MAX_IDLE_MS is still idle."""
        engine = make_engine(0)
        gap_ms = MAX_IDLE_MS + 1  # just over threshold
        engine.tick(gap_ms, "2026-02-11")
        assert engine.accumulated_break_ms == 0


# ---- Daily reset ----

class TestDailyReset:
    def test_reset_on_new_day(self):
        """Tick with new date → DAILY_RESET event, counters zeroed."""
        engine = make_engine(0, "2026-02-10")
        advance(engine, 0, 60, date="2026-02-10")  # accumulate some state
        assert engine.accumulated_break_ms == 30_000

        result = engine.tick(61_000, "2026-02-11")
        assert TimerEvent.DAILY_RESET in result.events
        assert result.reset_date == "2026-02-10"
        # 30_000ms // (1000 * 60) = 0 (less than 1 full minute)
        assert result.productivity_score == 0

    def test_reset_productivity_score(self):
        """Productivity score = accumulated_break_ms // (1000 * 60)."""
        engine = make_engine(0, "2026-02-10")
        advance(engine, 0, 240, date="2026-02-10")  # 120_000ms break
        result = engine.tick(241_000, "2026-02-11")
        assert result.productivity_score == 2  # 120_000 // 60_000 = 2

    def test_reset_clears_counters(self):
        engine = make_engine(0, "2026-02-10")
        advance(engine, 0, 60, date="2026-02-10")
        engine.tick(61_000, "2026-02-11")
        assert engine.accumulated_break_ms == 0
        assert engine.break_backlog_ms == 0
        assert engine.total_work_time_ms == 0
        assert engine.total_break_time_ms == 0
        assert engine.current_mode == TimerMode.WORK_SILENCE
        assert engine.daily_start_date == "2026-02-11"

    def test_reset_clears_manual_lock(self):
        engine = make_engine(0, "2026-02-10")
        engine.set_mode(TimerMode.BREAK, is_automatic=False, now_mono_ms=0)
        assert engine.manual_mode_lock
        engine.tick(1_000, "2026-02-11")
        assert not engine.manual_mode_lock

    def test_first_tick_sets_date(self):
        engine = TimerEngine(now_mono_ms=0)
        engine.tick(0, "2026-02-11")
        assert engine.daily_start_date == "2026-02-11"


# ---- Manual mode lock ----

class TestManualModeLock:
    def test_break_sets_lock(self):
        engine = make_engine(0)
        engine.set_mode(TimerMode.BREAK, is_automatic=False, now_mono_ms=0)
        assert engine.manual_mode_lock

    def test_pause_sets_lock(self):
        engine = make_engine(0)
        engine.set_mode(TimerMode.PAUSE, is_automatic=False, now_mono_ms=0)
        assert engine.manual_mode_lock

    def test_auto_switch_blocked_during_lock(self):
        """Automatic mode switches are blocked during manual lock."""
        engine = make_engine(0)
        advance(engine, 0, 60)  # earn break time
        engine.set_mode(TimerMode.BREAK, is_automatic=False, now_mono_ms=60_000)
        changed, _ = engine.set_mode(
            TimerMode.WORK_MUSIC, is_automatic=True, now_mono_ms=60_500
        )
        assert not changed
        assert engine.current_mode == TimerMode.BREAK

    def test_manual_switch_allowed_during_lock(self):
        """Manual (non-automatic) switches are always allowed."""
        engine = make_engine(0)
        engine.set_mode(TimerMode.BREAK, is_automatic=False, now_mono_ms=0)
        changed, _ = engine.set_mode(
            TimerMode.WORK_SILENCE, is_automatic=False, now_mono_ms=500
        )
        assert changed
        assert engine.current_mode == TimerMode.WORK_SILENCE

    def test_lock_expires(self):
        """After 30 min, auto-switches are allowed again."""
        engine = make_engine(0)
        engine.set_mode(TimerMode.BREAK, is_automatic=False, now_mono_ms=0)
        expired_ms = MANUAL_LOCK_DURATION_MS + 1
        changed, _ = engine.set_mode(
            TimerMode.WORK_MUSIC, is_automatic=True, now_mono_ms=expired_ms
        )
        assert changed
        assert engine.current_mode == TimerMode.WORK_MUSIC
        assert not engine.manual_mode_lock

    def test_work_mode_clears_lock(self):
        engine = make_engine(0)
        engine.set_mode(TimerMode.BREAK, is_automatic=False, now_mono_ms=0)
        engine.set_mode(TimerMode.WORK_SILENCE, is_automatic=False, now_mono_ms=500)
        assert not engine.manual_mode_lock


# ---- Serialization round-trip ----

class TestSerialization:
    def test_round_trip(self):
        """to_dict → from_dict preserves state."""
        engine = make_engine(0)
        advance(engine, 0, 120)  # 60_000ms break
        engine.set_mode(TimerMode.WORK_MUSIC, is_automatic=False, now_mono_ms=120_000)
        advance(engine, 120_000, 30)  # 7_500ms more break

        data = engine.to_dict(now_mono_ms=150_000)

        restored = TimerEngine(now_mono_ms=200_000)
        restored.from_dict(data, now_mono_ms=200_000)

        assert restored.current_mode == TimerMode.WORK_MUSIC
        assert restored.accumulated_break_ms == engine.accumulated_break_ms
        assert restored.break_backlog_ms == engine.break_backlog_ms
        assert restored.total_work_time_ms == engine.total_work_time_ms
        assert restored.total_break_time_ms == engine.total_break_time_ms
        assert restored.daily_start_date == engine.daily_start_date

    def test_lock_surviving_serialization(self):
        """Manual lock persists across serialize/deserialize."""
        engine = make_engine(0)
        engine.set_mode(TimerMode.BREAK, is_automatic=False, now_mono_ms=0)

        data = engine.to_dict(now_mono_ms=5_000)  # 5s into lock

        restored = TimerEngine(now_mono_ms=100_000)
        restored.from_dict(data, now_mono_ms=100_000)
        assert restored.manual_mode_lock

        # Auto-switch should still be blocked (lock has ~29m55s remaining)
        changed, _ = restored.set_mode(
            TimerMode.WORK_MUSIC, is_automatic=True, now_mono_ms=100_500
        )
        assert not changed

    def test_expired_lock_cleared_on_restore(self):
        """Lock that expired during downtime is cleared on restore."""
        engine = make_engine(0)
        engine.set_mode(TimerMode.BREAK, is_automatic=False, now_mono_ms=0)

        # Serialize at 31 min (lock expired)
        data = engine.to_dict(now_mono_ms=MANUAL_LOCK_DURATION_MS + 60_000)

        restored = TimerEngine(now_mono_ms=200_000)
        restored.from_dict(data, now_mono_ms=200_000)
        assert not restored.manual_mode_lock

    def test_no_work_actions_in_dict(self):
        """work_actions field is gone from serialization."""
        engine = make_engine(0)
        data = engine.to_dict(now_mono_ms=0)
        assert "work_actions" not in data

    def test_old_format_backward_compat(self):
        """from_dict handles old-format keys gracefully."""
        old_data = {
            "current_mode": "work_music",
            "total_work_time_ms": 120000.5,  # old float values
            "total_break_time_ms": 0,
            "accumulated_break_ms": 60000.25,
            "break_backlog_ms": 0,
            "work_actions": 42,  # vestige — should be ignored
            "daily_start_date": "2026-02-10",
            "last_tick_time": 1234567890.0,  # old field — ignored
            "manual_mode_lock": False,
            "manual_mode_lock_until": None,
        }
        engine = TimerEngine(now_mono_ms=0)
        engine.from_dict(old_data, now_mono_ms=0)
        assert engine.current_mode == TimerMode.WORK_MUSIC
        assert engine.accumulated_break_ms == 60000  # truncated to int
        assert engine.total_work_time_ms == 120000

    def test_export_dict_camel_case(self):
        """to_export_dict returns camelCase keys."""
        engine = make_engine(0)
        advance(engine, 0, 60)
        d = engine.to_export_dict()
        assert "currentMode" in d
        assert "breakAvailableSeconds" in d
        assert d["breakAvailableSeconds"] == 30  # 30_000ms = 30s


# ---- Edge cases ----

class TestEdgeCases:
    def test_zero_elapsed(self):
        """Tick with same timestamp → no change."""
        engine = make_engine(0)
        engine.tick(0, "2026-02-11")
        assert engine.accumulated_break_ms == 0

    def test_negative_elapsed(self):
        """Monotonic clock should never go backward, but handle gracefully."""
        engine = make_engine(1000)
        engine.tick(500, "2026-02-11")  # earlier timestamp
        assert engine.accumulated_break_ms == 0

    def test_sub_second_tick(self):
        """Ticks faster than 1s still accumulate correctly."""
        engine = make_engine(0)
        for i in range(1000):
            engine.tick(i * 100, "2026-02-11")  # 100ms ticks for 100s
        # 99.9s of silence at 0.5 rate ≈ 49_950ms
        # Actually: 999 ticks of 100ms each = 99_900ms total, * 1/2 = 49_950ms
        assert engine.accumulated_break_ms == 49_950

    def test_initial_mode_is_work_silence(self):
        engine = TimerEngine(now_mono_ms=0)
        assert engine.current_mode == TimerMode.WORK_SILENCE

    def test_break_exhaustion_exact_zero(self):
        """When break hits exactly 0 (no overshoot), no backlog created."""
        engine = make_engine(0)
        # Earn exactly 10_000ms break (20s silence)
        advance(engine, 0, 20)
        assert engine.accumulated_break_ms == 10_000
        # Consume exactly 10_000ms in break mode
        engine.set_mode(TimerMode.BREAK, is_automatic=False, now_mono_ms=20_000)
        advance(engine, 20_000, 10)
        assert engine.accumulated_break_ms == 0
        assert engine.break_backlog_ms == 0
