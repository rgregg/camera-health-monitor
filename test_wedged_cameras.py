"""Tests for wedged-camera (burst-read fps) detection."""

import os

# monitor.py reads these required env vars at import time.
os.environ.setdefault("CAMERA_USER", "test")
os.environ.setdefault("CAMERA_PASSWORD", "test")

import monitor  # noqa: E402


def _cam(camera_fps, skipped_fps):
    """Minimal Frigate camera stats dict."""
    return {"camera_fps": camera_fps, "skipped_fps": skipped_fps}


def _stats(**cameras):
    return {"cameras": cameras}


# --- _is_wedged predicate ---


def test_is_wedged_true_for_burst_read_signature():
    # The real failure: ~109 fps with ~107 skipped.
    assert monitor._is_wedged(_cam(109.5, 107.1), threshold=30) is True


def test_is_wedged_false_for_healthy_camera():
    # Normal cabin camera: ~5-7 fps, ~0 skipped.
    assert monitor._is_wedged(_cam(7.0, 0.1), threshold=30) is False


def test_is_wedged_false_when_high_fps_but_not_skipping():
    # A genuinely busy camera processing frames is not wedged.
    assert monitor._is_wedged(_cam(40.0, 0.0), threshold=30) is False


# --- check_wedged_cameras state machine ---


def setup_function():
    """Reset module state and config before each test."""
    monitor._wedged_counts.clear()
    monitor._wedged_alert_sent.clear()
    monitor.WEDGED_FPS_THRESHOLD = 30
    monitor.WEDGED_CYCLES = 2


def _capture_alerts(monkeypatch):
    sent = []
    monkeypatch.setattr(monitor, "_send_system_alert",
                        lambda title, message: sent.append((title, message)))
    return sent


def test_no_alert_before_debounce_cycles(monkeypatch):
    sent = _capture_alerts(monkeypatch)
    stats = _stats(kitchen=_cam(109.5, 107.1))
    monitor.check_wedged_cameras(stats)  # cycle 1 of 2
    assert sent == []


def test_alerts_once_debounce_reached(monkeypatch):
    sent = _capture_alerts(monkeypatch)
    stats = _stats(kitchen=_cam(109.5, 107.1))
    monitor.check_wedged_cameras(stats)  # cycle 1
    monitor.check_wedged_cameras(stats)  # cycle 2 -> alert
    assert len(sent) == 1
    assert "kitchen" in sent[0][1]


def test_does_not_realert_while_still_wedged(monkeypatch):
    sent = _capture_alerts(monkeypatch)
    stats = _stats(kitchen=_cam(109.5, 107.1))
    for _ in range(5):
        monitor.check_wedged_cameras(stats)
    assert len(sent) == 1  # exactly one alert for the episode


def test_recovery_clears_state_and_allows_future_alert(monkeypatch):
    sent = _capture_alerts(monkeypatch)
    wedged = _stats(kitchen=_cam(109.5, 107.1))
    healthy = _stats(kitchen=_cam(7.0, 0.1))
    monitor.check_wedged_cameras(wedged)   # cycle 1
    monitor.check_wedged_cameras(wedged)   # cycle 2 -> alert
    monitor.check_wedged_cameras(healthy)  # recovers, clears state
    monitor.check_wedged_cameras(wedged)   # new episode cycle 1
    monitor.check_wedged_cameras(wedged)   # new episode cycle 2 -> alert
    assert len(sent) == 2


def test_transient_spike_then_recovery_never_alerts(monkeypatch):
    sent = _capture_alerts(monkeypatch)
    monitor.check_wedged_cameras(_stats(kitchen=_cam(109.5, 107.1)))  # 1 cycle
    monitor.check_wedged_cameras(_stats(kitchen=_cam(7.0, 0.1)))      # recovered
    assert sent == []
