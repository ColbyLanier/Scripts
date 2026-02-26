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
    IDLE_TO_BREAK_TIMEOUT_MS,
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
    def test_silence_earns_quarter_rate(self):
        """60s of work_silence → 15_000ms break earned (parity with music)."""
        engine = make_engine(0)
        advance(engine, 0, 60)
        assert engine.accumulated_break_ms == 15_000

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
        """Switch from silence to music, verify rates are same (parity)."""
        engine = make_engine(0)
        advance(engine, 0, 60)  # 15s break earned (silence = 1/4 rate)
        changed, _ = engine.set_mode(TimerMode.WORK_MUSIC, is_automatic=False, now_mono_ms=60_000)
        assert changed
        advance(engine, 60_000, 60)  # 15s more break (music = 1/4 rate)
        assert engine.accumulated_break_ms == 30_000

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
        # Advance 10s in silence (should earn 2500ms break at 1/4 rate)
        changed, _ = engine.set_mode(TimerMode.WORK_MUSIC, is_automatic=False, now_mono_ms=10_000)
        assert engine.accumulated_break_ms == 2_500


# ---- Video penalty ----

class TestVideoPenalty:
    def test_video_decreases_break(self):
        """Accumulate break, switch to video, verify break decreases."""
        engine = make_engine(0)
        advance(engine, 0, 60)  # 15_000ms break (silence at 1/4 rate)
        engine.set_mode(TimerMode.WORK_VIDEO, is_automatic=False, now_mono_ms=60_000)
        advance(engine, 60_000, 60)  # -15_000ms (video penalty)
        assert engine.accumulated_break_ms == 0

    def test_scrolling_decreases_break_at_gaming_rate(self):
        """Scrolling (Twitter) drains break at -30 min/hr, same as gaming."""
        engine = make_engine(0)
        advance(engine, 0, 60)  # 15_000ms break (silence at 1/4 rate)
        engine.set_mode(TimerMode.WORK_SCROLLING, is_automatic=False, now_mono_ms=60_000)
        advance(engine, 60_000, 60)  # -30_000ms (scrolling penalty) → 15k backlog
        assert engine.accumulated_break_ms == 0
        assert engine.break_backlog_ms == 15_000

    def test_gaming_decreases_break_faster(self):
        """Gaming penalty is twice video."""
        engine = make_engine(0)
        advance(engine, 0, 60)  # 15_000ms break (silence at 1/4 rate)
        engine.set_mode(TimerMode.WORK_GAMING, is_automatic=False, now_mono_ms=60_000)
        advance(engine, 60_000, 60)  # -30_000ms (gaming penalty) → 15k backlog
        assert engine.accumulated_break_ms == 0
        assert engine.break_backlog_ms == 15_000  # overshoot into backlog


# ---- Break consumption ----

class TestBreakConsumption:
    def test_break_mode_consumes_accumulated(self):
        """Enter break mode, verify accumulated_break_ms decreases."""
        engine = make_engine(0)
        advance(engine, 0, 60)  # 15_000ms break earned (silence at 1/4 rate)
        engine.set_mode(TimerMode.BREAK, is_automatic=False, now_mono_ms=60_000)
        advance(engine, 60_000, 10)  # consume 10_000ms
        assert engine.accumulated_break_ms == 5_000
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
        advance(engine, 0, 60)  # 15_000ms break (silence at 1/4 rate)
        engine.set_mode(TimerMode.BREAK, is_automatic=False, now_mono_ms=60_000)
        # Consume 16s of break (more than 15s available)
        result = advance(engine, 60_000, 16)
        assert TimerEvent.BREAK_EXHAUSTED in result.events
        assert engine.accumulated_break_ms == 0
        assert engine.break_backlog_ms > 0

    def test_video_exhaustion(self):
        """Video penalty can exhaust break."""
        engine = make_engine(0)
        advance(engine, 0, 20)  # 5_000ms break (silence at 1/4 rate)
        engine.set_mode(TimerMode.WORK_VIDEO, is_automatic=False, now_mono_ms=20_000)
        # 21s of video = 5_250ms penalty, exceeds 5_000ms break
        last = TickResult()
        exhausted = False
        for i in range(21):
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
        advance(engine, 40_000, 80)  # 80s silence at 1/4 = 20_000ms earned → 10k backlog, 10k break
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
        # Only the first 10s of work should have accumulated (at 1/4 rate)
        assert engine.accumulated_break_ms == 2_500

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
        assert engine.accumulated_break_ms == 15_000

        result = engine.tick(61_000, "2026-02-11")
        assert TimerEvent.DAILY_RESET in result.events
        assert result.reset_date == "2026-02-10"
        # 15_000ms // (1000 * 60) = 0 (less than 1 full minute)
        assert result.productivity_score == 0

    def test_reset_productivity_score(self):
        """Productivity score = accumulated_break_ms // (1000 * 60)."""
        engine = make_engine(0, "2026-02-10")
        advance(engine, 0, 240, date="2026-02-10")  # 60_000ms break (240s * 1/4)
        result = engine.tick(241_000, "2026-02-11")
        assert result.productivity_score == 1  # 60_000 // 60_000 = 1

    def test_reset_clears_counters(self):
        engine = make_engine(0, "2026-02-10")
        advance(engine, 0, 60, date="2026-02-10")
        engine.tick(61_000, "2026-02-11")
        from timer import DEFAULT_BREAK_BUFFER_MS
        assert engine.accumulated_break_ms == DEFAULT_BREAK_BUFFER_MS  # 5 min starting buffer
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
        assert d["breakAvailableSeconds"] == 15  # 15_000ms = 15s


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
        # 999 ticks of 100ms each = 99_900ms total, * 1/4 = 24_975ms
        assert engine.accumulated_break_ms == 24_975

    def test_initial_mode_is_work_silence(self):
        engine = TimerEngine(now_mono_ms=0)
        assert engine.current_mode == TimerMode.WORK_SILENCE

    def test_break_exhaustion_exact_zero(self):
        """When break hits exactly 0 (no overshoot), no backlog created."""
        engine = make_engine(0)
        # Earn exactly 5_000ms break (20s silence at 1/4 rate)
        advance(engine, 0, 20)
        assert engine.accumulated_break_ms == 5_000
        # Consume exactly 5_000ms in break mode
        engine.set_mode(TimerMode.BREAK, is_automatic=False, now_mono_ms=20_000)
        advance(engine, 20_000, 5)
        assert engine.accumulated_break_ms == 0
        assert engine.break_backlog_ms == 0


# ---- IDLE mode ----

class TestIdleMode:
    def test_idle_no_accumulation(self):
        """IDLE mode: no break earned, no work time tracked."""
        engine = make_engine(0)
        # Earn some break first to verify it doesn't change
        advance(engine, 0, 40)  # 10_000ms break
        assert engine.accumulated_break_ms == 10_000
        work_before = engine.total_work_time_ms

        engine.set_mode(TimerMode.IDLE, is_automatic=True, now_mono_ms=40_000)
        advance(engine, 40_000, 60)
        assert engine.accumulated_break_ms == 10_000  # unchanged
        assert engine.total_work_time_ms == work_before  # unchanged
        assert engine.total_break_time_ms == 0

    def test_idle_timeout_triggers_break(self):
        """After 15 min of IDLE → auto-transition to BREAK + IDLE_TIMEOUT event."""
        engine = make_engine(0)
        engine.set_mode(TimerMode.IDLE, is_automatic=True, now_mono_ms=0)

        # Advance to just before timeout (14 min 59 sec) — should stay IDLE
        timeout_secs = IDLE_TO_BREAK_TIMEOUT_MS // 1000
        advance(engine, 0, timeout_secs - 1)
        assert engine.current_mode == TimerMode.IDLE

        # One more second → timeout
        result = engine.tick((timeout_secs) * 1000, "2026-02-11")
        assert engine.current_mode == TimerMode.BREAK
        assert TimerEvent.IDLE_TIMEOUT in result.events
        assert TimerEvent.MODE_CHANGED in result.events
        assert result.old_mode == TimerMode.IDLE

    def test_idle_timeout_exempt(self):
        """Stays IDLE past 15 min when exempt (gym/campus)."""
        engine = make_engine(0)
        engine.idle_timeout_exempt = True
        engine.set_mode(TimerMode.IDLE, is_automatic=True, now_mono_ms=0)

        timeout_secs = IDLE_TO_BREAK_TIMEOUT_MS // 1000
        result = advance(engine, 0, timeout_secs + 60)  # 16 minutes
        assert engine.current_mode == TimerMode.IDLE
        assert TimerEvent.IDLE_TIMEOUT not in result.events

    def test_idle_to_work_manual(self):
        """Manual switch from IDLE to WORK_SILENCE works."""
        engine = make_engine(0)
        engine.set_mode(TimerMode.IDLE, is_automatic=True, now_mono_ms=0)
        changed, result = engine.set_mode(TimerMode.WORK_SILENCE, is_automatic=False, now_mono_ms=5_000)
        assert changed
        assert engine.current_mode == TimerMode.WORK_SILENCE
        assert TimerEvent.MODE_CHANGED in result.events

    def test_idle_blocked_by_manual_lock(self):
        """Auto-IDLE blocked during BREAK/PAUSE lock."""
        engine = make_engine(0)
        advance(engine, 0, 60)  # earn break
        engine.set_mode(TimerMode.BREAK, is_automatic=False, now_mono_ms=60_000)
        assert engine.manual_mode_lock
        changed, _ = engine.set_mode(TimerMode.IDLE, is_automatic=True, now_mono_ms=61_000)
        assert not changed
        assert engine.current_mode == TimerMode.BREAK

    def test_idle_serialization(self):
        """Round-trip preserves idle state."""
        engine = make_engine(0)
        engine.idle_timeout_exempt = True
        engine.set_mode(TimerMode.IDLE, is_automatic=True, now_mono_ms=10_000)
        # Advance 5 seconds in IDLE
        advance(engine, 10_000, 5)

        data = engine.to_dict(now_mono_ms=15_000)
        assert data["idle_entered_elapsed_ms"] == 5_000
        assert data["idle_timeout_exempt"] is True

        restored = TimerEngine(now_mono_ms=100_000)
        restored.from_dict(data, now_mono_ms=100_000)
        assert restored.current_mode == TimerMode.IDLE
        assert restored.idle_timeout_exempt is True
        # idle_entered_ms should be restored relative to new now
        # After restoring, 5s elapsed means idle_entered = 100_000 - 5_000 = 95_000
        # So timeout would fire at 95_000 + 900_000 = 995_000
        # Advance to just before that
        advance(restored, 100_000, 60)  # 1 min, still far from timeout
        assert restored.current_mode == TimerMode.IDLE  # still idle (exempt)

    def test_idle_daily_reset_clears(self):
        """Daily reset clears idle state."""
        engine = make_engine(0, "2026-02-10")
        engine.set_mode(TimerMode.IDLE, is_automatic=True, now_mono_ms=0)
        engine.tick(1_000, "2026-02-11")  # trigger reset
        assert engine.current_mode == TimerMode.WORK_SILENCE

    def test_idle_timeout_emits_mode_changed(self):
        """Both IDLE_TIMEOUT and MODE_CHANGED events are emitted."""
        engine = make_engine(0)
        engine.set_mode(TimerMode.IDLE, is_automatic=True, now_mono_ms=0)
        timeout_secs = IDLE_TO_BREAK_TIMEOUT_MS // 1000
        result = advance(engine, 0, timeout_secs)
        assert TimerEvent.IDLE_TIMEOUT in result.events
        assert TimerEvent.MODE_CHANGED in result.events

    def test_idle_break_rate_zero(self):
        """Confirm IDLE has (0, 1) in rate table — neutral."""
        from timer import BREAK_RATE_TABLE
        assert BREAK_RATE_TABLE[TimerMode.IDLE] == (0, 1)
