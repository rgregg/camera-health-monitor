"""Tests for stats-frozen detection, and for why it stays GLOBAL rather than per-camera.

Background (home, 2026-08-15): `front_door`'s Frigate process hung with byte-identical
stats while the other 12 cameras kept reporting normally, and the all-camera fingerprint
never flagged it. The obvious fix — fingerprint each camera independently — was tried and
measured against the live server, and it does not work:

    distinct fingerprint values per camera over 12 polls
      porch                1 distinct -> {'5.1/5.1/0.0/0.0': 12}
      carport_side_wide    1 distinct -> {'5.1/5.1/0.0/0.0': 12}
      backyard             2 distinct -> {'5.1/5.1/0.0/0.0': 8, '5.0/5.0/0.0/0.0': 4}

`camera_fps`/`process_fps` are rolling averages rounded to one decimal, and
`detection_fps`/`skipped_fps` sit at 0.0 on an idle camera — so a healthy camera has only
1-3 possible fingerprints and frequently repeats one for many consecutive polls. Sampling
at both 10s and 15s intervals, 11-12 of 13 healthy cameras would have been flagged frozen.
A camera that is genuinely stable is INDISTINGUISHABLE from one that is stuck.

So the frozen check stays global (all cameras stuck at once — a real stats-engine wedge,
and unlikely by chance because any one camera moving resets it), and per-camera detection
is handled by check_recording_gaps(), which tests the thing that actually matters and has
no such ambiguity. See test_recording_gaps.py.
"""

import os

# monitor.py reads these required env vars at import time.
os.environ.setdefault("CAMERA_USER", "test")
os.environ.setdefault("CAMERA_PASSWORD", "test")

import monitor  # noqa: E402


def _cam(camera_fps, process_fps=5.0, detection_fps=0.0, skipped_fps=0.0):
    return {
        "camera_fps": camera_fps,
        "process_fps": process_fps,
        "detection_fps": detection_fps,
        "skipped_fps": skipped_fps,
    }


def setup_function():
    monitor._stats_signature["sig"] = None
    monitor._stats_signature["count"] = 0
    monitor._stats_frozen_alert_sent["active"] = False
    monitor.STATS_FROZEN_CYCLES = 3


def _capture_alerts(monkeypatch):
    sent = []
    monkeypatch.setattr(monitor, "_send_system_alert",
                        lambda title, message: sent.append((title, message)))
    return sent


# --- the false-positive guard (why this check is not per-camera) ---


def test_stable_healthy_camera_is_not_flagged(monkeypatch):
    """A camera repeating one fingerprint is normal — porch did exactly this for 12
    straight polls while recording perfectly."""
    _capture_alerts(monkeypatch)
    for cycle in range(10):
        frozen = monitor.check_stats_frozen({"cameras": {
            "porch": _cam(5.1, 5.1),                  # never changes
            "backyard": _cam(5.0 + cycle * 0.1),      # moves
        }})
    assert frozen is False


def test_single_wedged_camera_does_not_trip_the_global_check(monkeypatch):
    """Documents the real limitation: this check CANNOT see one hung camera. That is
    check_recording_gaps()'s job — see test_recording_gaps.py."""
    _capture_alerts(monkeypatch)
    for cycle in range(10):
        frozen = monitor.check_stats_frozen({"cameras": {
            "front_door": _cam(104.0, 100.0, 0.0, 3.8),   # hung, identical every cycle
            "backyard": _cam(5.0 + cycle * 0.1),
        }})
    assert frozen is False


# --- what it does detect: a whole-engine freeze ---


def test_detects_whole_engine_freeze(monkeypatch):
    _capture_alerts(monkeypatch)
    stats = {"cameras": {"a": _cam(5.0), "b": _cam(5.1)}}
    for _ in range(monitor.STATS_FROZEN_CYCLES):
        frozen = monitor.check_stats_frozen(stats)
    assert frozen is True


def test_no_detection_before_debounce_cycles(monkeypatch):
    _capture_alerts(monkeypatch)
    stats = {"cameras": {"a": _cam(5.0), "b": _cam(5.1)}}
    for _ in range(monitor.STATS_FROZEN_CYCLES - 1):
        frozen = monitor.check_stats_frozen(stats)
    assert frozen is False


def test_detector_movement_also_counts_as_alive(monkeypatch):
    _capture_alerts(monkeypatch)
    for cycle in range(6):
        frozen = monitor.check_stats_frozen({
            "cameras": {"a": _cam(5.0)},
            "detectors": {"onnx_0": {"inference_speed": 9.9 + cycle * 0.1}},
        })
    assert frozen is False


def test_alerts_once_per_episode(monkeypatch):
    sent = _capture_alerts(monkeypatch)
    stats = {"cameras": {"a": _cam(5.0), "b": _cam(5.1)}}
    for _ in range(8):
        monitor.check_stats_frozen(stats)
    assert len(sent) == 1


def test_none_stats_is_not_frozen(monkeypatch):
    _capture_alerts(monkeypatch)
    assert monitor.check_stats_frozen(None) is False
