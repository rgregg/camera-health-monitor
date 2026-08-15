"""Tests for recording-gap detection.

The most direct backstop for the 2026-08-15 front_door incident: whatever the fps or
stats signature look like, a camera that has written no recording segment in a long
time is broken. Frigate writes ~10s segments, so a healthy camera's newest segment is
always seconds old.
"""

import os

# monitor.py reads these required env vars at import time.
os.environ.setdefault("CAMERA_USER", "test")
os.environ.setdefault("CAMERA_PASSWORD", "test")

import monitor  # noqa: E402


def setup_function():
    monitor._recording_gap_counts.clear()
    monitor._recording_gap_alert_sent.clear()
    monitor.RECORDING_GAP_SECONDS = 900
    monitor.RECORDING_GAP_CYCLES = 2


def _capture_alerts(monkeypatch):
    sent = []
    monkeypatch.setattr(monitor, "_send_system_alert",
                        lambda title, message: sent.append((title, message)))
    return sent


# --- the core signal ---


def test_detects_camera_with_stale_recordings(monkeypatch):
    _capture_alerts(monkeypatch)
    ages = {"front_door": 40_000, "backyard": 9}   # front_door ~11h stale
    for _ in range(monitor.RECORDING_GAP_CYCLES):
        stale = monitor.check_recording_gaps(ages)
    assert stale == ["front_door"]


def test_fresh_recordings_never_flagged(monkeypatch):
    _capture_alerts(monkeypatch)
    for _ in range(6):
        stale = monitor.check_recording_gaps({"front_door": 5, "backyard": 11})
    assert stale == []


def test_missing_recordings_counts_as_a_gap(monkeypatch):
    _capture_alerts(monkeypatch)
    # None = the recordings query returned nothing at all for this camera.
    for _ in range(monitor.RECORDING_GAP_CYCLES):
        stale = monitor.check_recording_gaps({"front_door": None})
    assert stale == ["front_door"]


def test_age_just_under_threshold_is_healthy(monkeypatch):
    _capture_alerts(monkeypatch)
    for _ in range(4):
        stale = monitor.check_recording_gaps({"front_door": 899})
    assert stale == []


# --- debounce, alerting, recovery ---


def test_no_alert_before_debounce_cycles(monkeypatch):
    sent = _capture_alerts(monkeypatch)
    monitor.check_recording_gaps({"front_door": 40_000})  # cycle 1 of 2
    assert sent == []


def test_alerts_once_per_episode(monkeypatch):
    sent = _capture_alerts(monkeypatch)
    for _ in range(8):
        monitor.check_recording_gaps({"front_door": 40_000})
    assert len(sent) == 1
    assert "front_door" in sent[0][1]


def test_recovery_clears_state_and_allows_future_alert(monkeypatch):
    sent = _capture_alerts(monkeypatch)
    for _ in range(monitor.RECORDING_GAP_CYCLES):
        monitor.check_recording_gaps({"front_door": 40_000})   # episode 1 -> alert
    monitor.check_recording_gaps({"front_door": 7})            # recovered
    for _ in range(monitor.RECORDING_GAP_CYCLES):
        monitor.check_recording_gaps({"front_door": 40_000})   # episode 2 -> alert
    assert len(sent) == 2


def test_transient_gap_then_recovery_never_alerts(monkeypatch):
    sent = _capture_alerts(monkeypatch)
    monitor.check_recording_gaps({"front_door": 40_000})  # 1 cycle
    monitor.check_recording_gaps({"front_door": 6})       # recovered
    assert sent == []


def test_empty_input_is_safe(monkeypatch):
    _capture_alerts(monkeypatch)
    assert monitor.check_recording_gaps({}) == []


# --- age extraction from the Frigate recordings payload ---


def test_recording_age_from_segments():
    segments = [
        {"start_time": 1000, "end_time": 1010},
        {"start_time": 1010, "end_time": 1020},
    ]
    assert monitor._recording_age(segments, now=1030) == 10


def test_recording_age_is_none_without_segments():
    assert monitor._recording_age([], now=1030) is None
