"""Tests for the auto-restart decision logic (cooldown, daily cap, enable flag)."""

import os

# monitor.py reads these required env vars at import time.
os.environ.setdefault("CAMERA_USER", "test")
os.environ.setdefault("CAMERA_PASSWORD", "test")

import monitor  # noqa: E402


def _reset(enabled=True, cooldown=900, cap=4):
    """Reset the module-level restart state and knobs before each test."""
    monitor.AUTO_RESTART_FROZEN = enabled
    monitor.RESTART_COOLDOWN = cooldown
    monitor.MAX_RESTARTS_PER_DAY = cap
    monitor._restart_history[:] = []
    monitor._last_restart_time = 0.0


# --- _can_auto_restart ---


def test_allowed_when_fresh():
    _reset()
    allowed, why = monitor._can_auto_restart(now=10_000)
    assert allowed is True
    assert why == ""


def test_blocked_when_disabled():
    _reset(enabled=False)
    allowed, why = monitor._can_auto_restart(now=10_000)
    assert allowed is False
    assert "disabled" in why


def test_blocked_within_cooldown():
    _reset(cooldown=900)
    monitor._record_restart(now=10_000)
    allowed, why = monitor._can_auto_restart(now=10_000 + 300)  # 5 min later
    assert allowed is False
    assert "cooldown" in why


def test_allowed_after_cooldown():
    _reset(cooldown=900)
    monitor._record_restart(now=10_000)
    allowed, _ = monitor._can_auto_restart(now=10_000 + 901)
    assert allowed is True


def test_blocked_at_daily_cap():
    _reset(cooldown=0, cap=4)  # cooldown=0 isolates the cap check
    for i in range(4):
        monitor._record_restart(now=10_000 + i)
    allowed, why = monitor._can_auto_restart(now=10_100)
    assert allowed is False
    assert "cap" in why


def test_cap_ignores_restarts_older_than_24h():
    _reset(cooldown=0, cap=4)
    # Four restarts, all more than 24h before `now` — should be pruned from the window.
    for i in range(4):
        monitor._record_restart(now=1_000 + i)
    allowed, _ = monitor._can_auto_restart(now=1_000 + 86_401)
    assert allowed is True


# --- _record_restart pruning ---


def test_record_restart_prunes_old_entries():
    _reset()
    monitor._record_restart(now=1_000)          # ancient
    monitor._record_restart(now=1_000 + 90_000)  # >24h later
    # Only the recent one survives the prune keyed off the latest `now`.
    assert monitor._restart_history == [1_000 + 90_000]
