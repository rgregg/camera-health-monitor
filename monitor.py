#!/usr/bin/env python3
"""Camera health monitor — detects and reboots Reolink cameras with crashed RTSP."""

import json
import logging
import os
import re
import socket
import time
from urllib.parse import quote
from urllib.request import Request, urlopen
from urllib.error import URLError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("camera-monitor")

# --- Configuration from environment ---

FRIGATE_URL = os.environ.get("FRIGATE_URL", "http://frigate:5000").rstrip("/")
CAMERA_USER = os.environ["CAMERA_USER"]
CAMERA_PASSWORD = os.environ["CAMERA_PASSWORD"]
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "120"))
HA_URL = os.environ.get("HA_URL", "").rstrip("/")
HA_TOKEN = os.environ.get("HA_TOKEN", "")
# Which HA service receives alerts. Defaults to the catch-all notify.notify; set to
# "ticker/notify" to route through the ticker integration (then HA_NOTIFY_CATEGORY is
# required — e.g. "Frigate" at home, "Cabin" at the cabin).
HA_NOTIFY_SERVICE = os.environ.get("HA_NOTIFY_SERVICE", "notify/notify").strip("/")
HA_NOTIFY_CATEGORY = os.environ.get("HA_NOTIFY_CATEGORY", "").strip()
REBOOT_THRESHOLD = int(os.environ.get("REBOOT_THRESHOLD", "3"))
RTSP_PORT = 554
RTSP_TIMEOUT = 3  # seconds
REBOOT_COOLDOWN = 180  # seconds — skip reboot if last reboot was < 3 min ago
MEMORY_THRESHOLD = float(os.environ.get("MEMORY_THRESHOLD", "90"))  # percent
INFLUXDB_URL = os.environ.get("INFLUXDB_URL", "").rstrip("/")
INFLUXDB_TOKEN = os.environ.get("INFLUXDB_TOKEN", "")
INFLUXDB_ORG = os.environ.get("INFLUXDB_ORG", "smart_home")
INFLUXDB_BUCKET = os.environ.get("INFLUXDB_BUCKET", "frigate-health")

# Auto-restart Frigate when its stats/recording engine wedges (stats freeze and
# recordings silently stop while the container stays "up"). Uses Frigate's own
# /api/restart, so the monitor needs no host/docker access. Guarded by a cooldown and a
# rolling 24h cap so a persistent problem can't turn into a restart loop.
AUTO_RESTART_FROZEN = os.environ.get("AUTO_RESTART_FROZEN", "true").lower() in (
    "1", "true", "yes", "on",
)
RESTART_COOLDOWN = int(os.environ.get("RESTART_COOLDOWN", "900"))  # min seconds between restarts
MAX_RESTARTS_PER_DAY = int(os.environ.get("MAX_RESTARTS_PER_DAY", "4"))
_restart_history = []      # timestamps of auto-restarts within the last 24h
_last_restart_time = 0.0


def fetch_camera_ips():
    """Fetch Frigate config and return {ip: [camera_names]} mapping."""
    url = f"{FRIGATE_URL}/api/config"
    try:
        with urlopen(url, timeout=10) as resp:
            config = json.loads(resp.read())
    except (URLError, OSError, json.JSONDecodeError) as e:
        log.error("Failed to fetch Frigate config from %s: %s", url, e)
        return {}

    streams = config.get("go2rtc", {}).get("streams", {})
    ip_pattern = re.compile(r"[/@](\d+\.\d+\.\d+\.\d+)[:/]")

    ip_to_cameras = {}
    for name, sources in streams.items():
        for source in sources:
            if isinstance(source, str):
                match = ip_pattern.search(source)
                if match:
                    ip = match.group(1)
                    ip_to_cameras.setdefault(ip, []).append(name)
                    break  # one IP per camera name is enough

    log.info(
        "Discovered %d cameras across %d unique IPs",
        sum(len(v) for v in ip_to_cameras.values()),
        len(ip_to_cameras),
    )
    return ip_to_cameras


def check_rtsp(ip):
    """Return True if RTSP port 554 is accepting connections."""
    try:
        with socket.create_connection((ip, RTSP_PORT), timeout=RTSP_TIMEOUT):
            return True
    except (socket.timeout, ConnectionRefusedError, OSError):
        return False


def reboot_camera(ip):
    """Login to Reolink camera and send reboot command. Returns True on success."""
    login_payload = json.dumps([{
        "cmd": "Login",
        "param": {
            "User": {"userName": CAMERA_USER, "password": CAMERA_PASSWORD}
        },
    }]).encode()

    try:
        # Login
        login_url = f"http://{ip}/api.cgi?cmd=Login"
        req = Request(login_url, data=login_payload, method="POST")
        req.add_header("Content-Type", "application/json")
        with urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
        token = result[0]["value"]["Token"]["name"]

        # Reboot
        reboot_url = f"http://{ip}/api.cgi?cmd=Reboot&token={token}"
        reboot_payload = json.dumps([{"cmd": "Reboot", "param": {}}]).encode()
        req = Request(reboot_url, data=reboot_payload, method="POST")
        req.add_header("Content-Type", "application/json")
        with urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())

        if result[0].get("code") == 0 or result[0].get("value", {}).get("rspCode") == 200:
            return True
        log.warning("Unexpected reboot response from %s: %s", ip, result)
        return False

    except (URLError, OSError, KeyError, IndexError, json.JSONDecodeError) as e:
        log.warning("Failed to reboot camera at %s: %s", ip, e)
        return False


# In-memory reboot tracking: {ip: [timestamp, timestamp, ...]}
reboot_history = {}
# Last reboot time per IP for cooldown: {ip: timestamp}
last_reboot_time = {}


def record_reboot(ip):
    """Record a reboot event and prune old entries."""
    now = time.time()
    last_reboot_time[ip] = now
    reboot_history.setdefault(ip, []).append(now)
    # Prune entries older than 1 hour
    cutoff = now - 3600
    reboot_history[ip] = [t for t in reboot_history[ip] if t > cutoff]


def is_in_cooldown(ip):
    """Return True if camera was rebooted less than REBOOT_COOLDOWN seconds ago."""
    last = last_reboot_time.get(ip, 0)
    return (time.time() - last) < REBOOT_COOLDOWN


def should_notify(ip):
    """Return True if camera has hit the reboot threshold in the last hour."""
    count = len(reboot_history.get(ip, []))
    return count == REBOOT_THRESHOLD  # notify once when threshold is first reached


def send_ha_notification(ip, camera_names):
    """Send a notification to Home Assistant about a persistently failing camera."""
    if not HA_URL or not HA_TOKEN:
        return

    names = ", ".join(camera_names)
    count = len(reboot_history.get(ip, []))
    message = (
        f"Camera {names} ({ip}) has been rebooted {count} times in the last hour. "
        f"This may indicate a hardware or firmware problem that needs manual attention."
    )
    _send_system_alert(title=f"Camera Health Alert: {names}", message=message)


def fetch_frigate_stats():
    """Fetch Frigate stats once per cycle. Returns dict or None on failure."""
    url = f"{FRIGATE_URL}/api/stats"
    try:
        with urlopen(url, timeout=10) as resp:
            return json.loads(resp.read())
    except (URLError, OSError, json.JSONDecodeError) as e:
        log.warning("Failed to fetch Frigate stats: %s", e)
        return None


def write_influxdb(lines):
    """Write line protocol data to InfluxDB. lines is a list of strings."""
    if not INFLUXDB_URL or not INFLUXDB_TOKEN:
        return

    url = (
        f"{INFLUXDB_URL}/api/v2/write"
        f"?org={quote(INFLUXDB_ORG)}&bucket={quote(INFLUXDB_BUCKET)}&precision=s"
    )
    body = "\n".join(lines).encode()
    req = Request(url, data=body, method="POST")
    req.add_header("Authorization", f"Token {INFLUXDB_TOKEN}")
    req.add_header("Content-Type", "text/plain")

    try:
        with urlopen(req, timeout=10):
            log.debug("Wrote %d points to InfluxDB", len(lines))
    except (URLError, OSError) as e:
        log.warning("Failed to write to InfluxDB: %s", e)


def _escape_tag(value):
    """Escape special characters in InfluxDB tag values."""
    return str(value).replace(" ", "\\ ").replace(",", "\\,").replace("=", "\\=")


def collect_and_write_metrics(stats, frozen=False):
    """Collect metrics from Frigate stats and write to InfluxDB."""
    if not INFLUXDB_URL or not INFLUXDB_TOKEN or stats is None:
        return

    now = int(time.time())
    lines = []

    # Detector metrics
    for name, info in stats.get("detectors", {}).items():
        inference = info.get("inference_speed", 0)
        pid = info.get("pid", 0)
        healthy = 1 if (inference < DETECTOR_INFERENCE_THRESHOLD and pid > 0) else 0
        lines.append(
            f"frigate_detector,detector={_escape_tag(name)} "
            f"inference_speed={inference},pid={pid}i,healthy={healthy}i {now}"
        )

    # Camera metrics
    for cam, info in stats.get("cameras", {}).items():
        cam_fps = info.get("camera_fps", 0)
        proc_fps = info.get("process_fps", 0)
        det_fps = info.get("detection_fps", 0)
        skipped_fps = info.get("skipped_fps", 0)
        det_enabled = 1 if info.get("detection_enabled", False) else 0
        lines.append(
            f"frigate_camera,camera={_escape_tag(cam)} "
            f"camera_fps={cam_fps},process_fps={proc_fps},"
            f"detection_fps={det_fps},skipped_fps={skipped_fps},"
            f"detection_enabled={det_enabled}i {now}"
        )

    # System metrics
    service = stats.get("service", {})
    mem = service.get("memory", {})
    mem_used = mem.get("used", 0)
    mem_total = mem.get("total", 1)
    mem_pct = (mem_used / mem_total) * 100 if mem_total > 0 else 0
    uptime = service.get("uptime", 0)

    gpu = stats.get("gpu_usages", {})
    gpu_util = 0
    gpu_mem = 0
    for _gpu_name, usage in gpu.items():
        gpu_util = float(usage.get("gpu", "0").rstrip(" %") or 0)
        gpu_mem = float(usage.get("mem", "0").rstrip(" %") or 0)
        break  # first GPU

    lines.append(
        f"frigate_system memory_used={mem_used},memory_total={mem_total},"
        f"memory_pct={mem_pct:.1f},uptime={uptime}i,"
        f"gpu_util={gpu_util},gpu_mem={gpu_mem},stats_frozen={1 if frozen else 0}i {now}"
    )

    write_influxdb(lines)


def check_system_memory(stats):
    """Check Frigate system stats and alert if memory usage is too high."""
    if stats is None:
        return

    mem = stats.get("service", {}).get("memory", {})
    used = mem.get("used", 0)
    total = mem.get("total", 1)
    pct = (used / total) * 100 if total > 0 else 0

    if pct >= MEMORY_THRESHOLD:
        if not _memory_alert_sent.get("active"):
            log.warning("Memory usage critical: %.1f%% (%s / %s)",
                        pct, _fmt_bytes(used), _fmt_bytes(total))
            _send_system_alert(
                title="Frigate Memory Alert",
                message=(
                    f"Frigate server memory usage is at {pct:.1f}% "
                    f"({_fmt_bytes(used)} / {_fmt_bytes(total)}). "
                    f"This may cause OOM issues and degraded performance."
                ),
            )
            _memory_alert_sent["active"] = True
    else:
        if _memory_alert_sent.get("active"):
            log.info("Memory usage recovered: %.1f%%", pct)
            _memory_alert_sent["active"] = False
        else:
            log.debug("Memory usage: %.1f%%", pct)


_memory_alert_sent = {"active": False}
_detector_alert_sent = {"active": False}

# Inference time above this (in ms) indicates a hung/failed detector
DETECTOR_INFERENCE_THRESHOLD = float(os.environ.get("DETECTOR_INFERENCE_THRESHOLD", "1000"))

# Consecutive cycles of byte-identical Frigate stats before flagging the stats engine frozen
STATS_FROZEN_CYCLES = int(os.environ.get("STATS_FROZEN_CYCLES", "3"))
_stats_frozen_alert_sent = {"active": False}
_stats_signature = {"sig": None, "count": 0}

# A camera whose capture thread wedged on a dead RTSP session burst-reads the empty
# restream: camera_fps AND skipped_fps both spike far above the normal ~5-7 fps while
# Frigate serves a "No frames received" placeholder. Requiring both high avoids flagging
# a camera that is genuinely busy but still processing frames.
WEDGED_FPS_THRESHOLD = float(os.environ.get("WEDGED_FPS_THRESHOLD", "30"))
# Consecutive cycles matching the burst-read signature before alerting (debounces the
# transient fps spikes seen right after a Frigate restart).
WEDGED_CYCLES = int(os.environ.get("WEDGED_CYCLES", "2"))
_wedged_counts = {}       # camera -> consecutive cycles matching the signature
_wedged_alert_sent = {}   # camera -> alert already sent for the current episode


def _fmt_bytes(b):
    """Format bytes as human-readable string."""
    for unit in ("B", "KB", "MB", "GB"):
        if abs(b) < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"


def _send_system_alert(title, message):
    """Send an alert to Home Assistant via the configured notify service.

    Routes through HA_NOTIFY_SERVICE (default notify.notify). When targeting the ticker
    integration, HA_NOTIFY_CATEGORY is included as the required `category` field. The
    payload is kept flat — the ticker service itself wraps `data` for the underlying
    notifier, so double-wrapping here would break it."""
    if not HA_URL or not HA_TOKEN:
        return

    body = {"title": title, "message": message}
    if HA_NOTIFY_CATEGORY:
        body["category"] = HA_NOTIFY_CATEGORY
    payload = json.dumps(body).encode()
    url = f"{HA_URL}/api/services/{HA_NOTIFY_SERVICE}"
    req = Request(url, data=payload, method="POST")
    req.add_header("Authorization", f"Bearer {HA_TOKEN}")
    req.add_header("Content-Type", "application/json")

    try:
        with urlopen(req, timeout=10) as resp:
            log.info("HA alert sent via %s: %s", HA_NOTIFY_SERVICE, title)
    except (URLError, OSError) as e:
        log.warning("Failed to send HA alert via %s: %s", HA_NOTIFY_SERVICE, e)


def _can_auto_restart(now):
    """Return (allowed, reason_if_blocked) for issuing an auto-restart at time `now`."""
    if not AUTO_RESTART_FROZEN:
        return False, "auto-restart disabled"
    since = now - _last_restart_time
    if since < RESTART_COOLDOWN:
        return False, f"in cooldown ({int(RESTART_COOLDOWN - since)}s left)"
    recent = [t for t in _restart_history if t > now - 86400]
    if len(recent) >= MAX_RESTARTS_PER_DAY:
        return False, f"daily cap reached ({MAX_RESTARTS_PER_DAY}/24h)"
    return True, ""


def _record_restart(now):
    """Record an auto-restart at `now` and prune history older than 24h."""
    global _last_restart_time
    _last_restart_time = now
    _restart_history.append(now)
    _restart_history[:] = [t for t in _restart_history if t > now - 86400]


def restart_frigate(reason):
    """Ask Frigate to restart itself via /api/restart to clear a software wedge. This
    restarts the Frigate app (detectors, capture, recording) without touching the
    container, so no host/docker access is needed. Returns True if accepted."""
    url = f"{FRIGATE_URL}/api/restart"
    req = Request(url, data=b"", method="POST")
    try:
        with urlopen(req, timeout=15) as resp:
            ok = 200 <= resp.status < 300
    except (URLError, OSError) as e:
        log.error("Frigate /api/restart failed (%s): %s", reason, e)
        return False
    log.warning("Requested Frigate restart (%s)", reason)
    return ok


def maybe_auto_restart(reason, now=None):
    """Auto-restart Frigate for `reason`, honoring the enable flag, cooldown and daily
    cap. Alerts either way and returns True if a restart was issued."""
    now = time.time() if now is None else now
    allowed, why = _can_auto_restart(now)
    if not allowed:
        log.info("Not auto-restarting Frigate (%s): %s", reason, why)
        return False

    _record_restart(now)
    ok = restart_frigate(reason)
    count = len([t for t in _restart_history if t > now - 86400])
    _send_system_alert(
        title="Frigate Auto-Restart",
        message=(
            f"camera-health-monitor {'restarted' if ok else 'tried to restart'} Frigate "
            f"automatically: {reason}. Restart {count}/{MAX_RESTARTS_PER_DAY} in the last "
            f"24h. If this keeps happening, Frigate needs manual attention."
        ),
    )
    # Require a fresh run of frozen cycles before another restart can trigger.
    _stats_signature["sig"] = None
    _stats_signature["count"] = 0
    return ok


def check_detectors(stats):
    """Check Frigate detector health and alert if any are failed/hung."""
    if stats is None:
        return

    detectors = stats.get("detectors", {})
    if not detectors:
        log.warning("No detectors found in Frigate stats")
        return

    failed = []
    for name, info in detectors.items():
        inference = info.get("inference_speed", 0)
        pid = info.get("pid", 0)
        if inference > DETECTOR_INFERENCE_THRESHOLD or pid == 0:
            failed.append(f"{name} (inference={inference:.0f}ms, pid={pid})")

    if failed:
        if not _detector_alert_sent.get("active"):
            names = ", ".join(failed)
            log.warning("Detector(s) unhealthy: %s", names)
            _send_system_alert(
                title="Frigate Detector Alert",
                message=(
                    f"Frigate detector(s) appear failed: {names}. "
                    f"Object detection may be offline. "
                    f"A Frigate restart is likely needed."
                ),
            )
            _detector_alert_sent["active"] = True
    else:
        if _detector_alert_sent.get("active"):
            log.info("All detectors recovered")
            _detector_alert_sent["active"] = False
        else:
            log.debug("All %d detectors healthy", len(detectors))


def _stats_fingerprint(stats):
    """Signature of the volatile per-camera/detector metrics that SHOULD change every
    cycle. Excludes uptime/memory (always advance) so an identical signature means
    Frigate's stats are genuinely stuck, not just stable."""
    parts = []
    for cam in sorted(stats.get("cameras", {})):
        info = stats["cameras"][cam]
        parts.append(
            f"{cam}:{info.get('camera_fps')}:{info.get('process_fps')}:"
            f"{info.get('detection_fps')}:{info.get('skipped_fps')}"
        )
    for det in sorted(stats.get("detectors", {})):
        parts.append(f"{det}:{stats['detectors'][det].get('inference_speed')}")
    return "|".join(parts)


def check_stats_frozen(stats):
    """Detect a wedged Frigate stats engine: byte-identical camera/detector metrics
    across STATS_FROZEN_CYCLES consecutive cycles (uptime keeps advancing, but the
    real metrics never move). Alerts via HA and returns True while frozen."""
    if stats is None:
        return False

    sig = _stats_fingerprint(stats)
    if sig and sig == _stats_signature["sig"]:
        _stats_signature["count"] += 1
    else:
        _stats_signature["sig"] = sig
        _stats_signature["count"] = 1

    frozen = _stats_signature["count"] >= STATS_FROZEN_CYCLES

    if frozen:
        if not _stats_frozen_alert_sent.get("active"):
            mins = (_stats_signature["count"] * CHECK_INTERVAL) // 60
            log.warning(
                "Frigate stats frozen — identical metrics for %d cycles (~%dm)",
                _stats_signature["count"], mins,
            )
            _send_system_alert(
                title="Frigate Stats Frozen",
                message=(
                    f"Frigate /api/stats has returned byte-identical camera/detector "
                    f"metrics for {_stats_signature['count']} cycles (~{mins} min) — its "
                    f"stats engine appears wedged (cameras may still be recording). "
                    f"A Frigate restart usually clears it."
                ),
            )
            _stats_frozen_alert_sent["active"] = True
    else:
        if _stats_frozen_alert_sent.get("active"):
            log.info("Frigate stats updating again")
            _stats_frozen_alert_sent["active"] = False

    return frozen


def _is_wedged(info, threshold):
    """True if a camera's stats match the burst-read signature of a wedged capture
    thread: both camera_fps and skipped_fps at/above threshold."""
    return (info.get("camera_fps", 0) >= threshold
            and info.get("skipped_fps", 0) >= threshold)


def check_wedged_cameras(stats):
    """Detect cameras whose capture thread wedged on a dead RTSP session and are
    burst-reading the empty restream. Alerts once per episode (after WEDGED_CYCLES
    consecutive cycles) and clears state on recovery. Notify-only; the fix is a
    Frigate restart."""
    if stats is None:
        return

    newly_wedged = []
    for cam, info in stats.get("cameras", {}).items():
        if _is_wedged(info, WEDGED_FPS_THRESHOLD):
            _wedged_counts[cam] = _wedged_counts.get(cam, 0) + 1
            if (_wedged_counts[cam] >= WEDGED_CYCLES
                    and not _wedged_alert_sent.get(cam)):
                newly_wedged.append(cam)
                _wedged_alert_sent[cam] = True
        else:
            if _wedged_alert_sent.get(cam):
                log.info("Camera %s recovered from wedged state", cam)
            _wedged_counts[cam] = 0
            _wedged_alert_sent[cam] = False

    if newly_wedged:
        names = ", ".join(sorted(newly_wedged))
        mins = (WEDGED_CYCLES * CHECK_INTERVAL) // 60
        log.warning("Camera(s) wedged (burst-reading): %s", names)
        _send_system_alert(
            title="Frigate Camera Wedged",
            message=(
                f"Frigate camera(s) appear wedged: {names}. Their capture thread is "
                f"burst-reading an empty restream (camera_fps/skipped_fps >= "
                f"{WEDGED_FPS_THRESHOLD:.0f} for ~{mins} min) and showing a 'No frames "
                f"received' placeholder while the camera itself is healthy. "
                f"A Frigate restart is needed to reconnect."
            ),
        )


def run_check_cycle(ip_to_cameras):
    """Run one health check cycle across all cameras."""
    healthy = 0
    rebooted = 0
    cooldown = 0
    failed = 0
    for ip, camera_names in ip_to_cameras.items():
        names = ", ".join(camera_names)

        if is_in_cooldown(ip):
            log.debug("Skipping %s (%s) — still in reboot cooldown", names, ip)
            cooldown += 1
            continue

        if check_rtsp(ip):
            log.debug("OK: %s (%s)", names, ip)
            healthy += 1
            continue

        # RTSP port is down — retry once after a short delay to rule out transient blip
        log.info("RTSP port closed on %s (%s) — retrying in 5s", names, ip)
        time.sleep(5)
        if check_rtsp(ip):
            log.info("RTSP recovered on retry for %s (%s) — skipping reboot", names, ip)
            continue

        log.warning("RTSP port still closed on %s (%s) — rebooting", names, ip)
        success = reboot_camera(ip)

        if success:
            log.info("Reboot command sent to %s (%s)", names, ip)
            record_reboot(ip)
            rebooted += 1
            if should_notify(ip):
                send_ha_notification(ip, camera_names)
        else:
            log.error("Failed to reboot %s (%s) — camera may be fully offline", names, ip)
            failed += 1

    total = len(ip_to_cameras)
    parts = [f"{healthy}/{total} healthy"]
    if rebooted:
        parts.append(f"{rebooted} rebooted")
    if cooldown:
        parts.append(f"{cooldown} in cooldown")
    if failed:
        parts.append(f"{failed} unreachable")
    log.info("Cycle complete: %s", ", ".join(parts))


def main():
    log.info("Camera health monitor starting")
    log.info("Frigate URL: %s", FRIGATE_URL)
    log.info("Check interval: %ds", CHECK_INTERVAL)
    log.info("Reboot cooldown: %ds", REBOOT_COOLDOWN)
    log.info(
        "HA notifications: %s via %s%s (threshold: %d reboots/hour)",
        "enabled" if HA_URL else "disabled",
        HA_NOTIFY_SERVICE,
        f" [category={HA_NOTIFY_CATEGORY}]" if HA_NOTIFY_CATEGORY else "",
        REBOOT_THRESHOLD,
    )
    log.info(
        "Auto-restart on stats-frozen: %s (cooldown %ds, cap %d/24h)",
        "enabled" if AUTO_RESTART_FROZEN else "disabled",
        RESTART_COOLDOWN, MAX_RESTARTS_PER_DAY,
    )
    log.info("Memory alert threshold: %.0f%%", MEMORY_THRESHOLD)
    log.info("Detector inference threshold: %.0f ms", DETECTOR_INFERENCE_THRESHOLD)
    log.info(
        "Stats-frozen alert: %d identical cycles (~%dm)",
        STATS_FROZEN_CYCLES, (STATS_FROZEN_CYCLES * CHECK_INTERVAL) // 60,
    )
    log.info(
        "Wedged-camera alert: camera_fps/skipped_fps >= %.0f for %d cycles (~%dm)",
        WEDGED_FPS_THRESHOLD, WEDGED_CYCLES, (WEDGED_CYCLES * CHECK_INTERVAL) // 60,
    )
    log.info(
        "InfluxDB: %s (org=%s, bucket=%s)",
        INFLUXDB_URL or "disabled",
        INFLUXDB_ORG,
        INFLUXDB_BUCKET,
    )

    while True:
        ip_to_cameras = fetch_camera_ips()
        if ip_to_cameras:
            run_check_cycle(ip_to_cameras)
        else:
            log.warning("No cameras discovered — will retry next cycle")
        stats = fetch_frigate_stats()
        check_system_memory(stats)
        check_detectors(stats)
        check_wedged_cameras(stats)
        frozen = check_stats_frozen(stats)
        if frozen:
            maybe_auto_restart("Frigate stats frozen (recording/stats engine wedged)")
        collect_and_write_metrics(stats, frozen)
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
