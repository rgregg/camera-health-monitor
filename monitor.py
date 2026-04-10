#!/usr/bin/env python3
"""Camera health monitor — detects and reboots Reolink cameras with crashed RTSP."""

import json
import logging
import os
import re
import socket
import time
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
REBOOT_THRESHOLD = int(os.environ.get("REBOOT_THRESHOLD", "3"))
RTSP_PORT = 554
RTSP_TIMEOUT = 3  # seconds
REBOOT_COOLDOWN = 180  # seconds — skip reboot if last reboot was < 3 min ago
MEMORY_THRESHOLD = float(os.environ.get("MEMORY_THRESHOLD", "90"))  # percent


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
    payload = json.dumps({
        "message": message,
        "title": f"Camera Health Alert: {names}",
    }).encode()

    url = f"{HA_URL}/api/services/notify/notify"
    req = Request(url, data=payload, method="POST")
    req.add_header("Authorization", f"Bearer {HA_TOKEN}")
    req.add_header("Content-Type", "application/json")

    try:
        with urlopen(req, timeout=10) as resp:
            log.info("HA notification sent for %s (%s)", names, ip)
    except (URLError, OSError) as e:
        log.warning("Failed to send HA notification: %s", e)


def check_system_memory():
    """Check Frigate system stats and alert if memory usage is too high."""
    url = f"{FRIGATE_URL}/api/stats"
    try:
        with urlopen(url, timeout=10) as resp:
            stats = json.loads(resp.read())
    except (URLError, OSError, json.JSONDecodeError) as e:
        log.warning("Failed to fetch Frigate stats: %s", e)
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


def _fmt_bytes(b):
    """Format bytes as human-readable string."""
    for unit in ("B", "KB", "MB", "GB"):
        if abs(b) < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"


def _send_system_alert(title, message):
    """Send a system-level alert to Home Assistant."""
    if not HA_URL or not HA_TOKEN:
        return

    payload = json.dumps({"message": message, "title": title}).encode()
    url = f"{HA_URL}/api/services/notify/notify"
    req = Request(url, data=payload, method="POST")
    req.add_header("Authorization", f"Bearer {HA_TOKEN}")
    req.add_header("Content-Type", "application/json")

    try:
        with urlopen(req, timeout=10) as resp:
            log.info("HA system alert sent: %s", title)
    except (URLError, OSError) as e:
        log.warning("Failed to send HA system alert: %s", e)


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
        "HA notifications: %s (threshold: %d reboots/hour)",
        "enabled" if HA_URL else "disabled",
        REBOOT_THRESHOLD,
    )
    log.info("Memory alert threshold: %.0f%%", MEMORY_THRESHOLD)

    while True:
        ip_to_cameras = fetch_camera_ips()
        if ip_to_cameras:
            run_check_cycle(ip_to_cameras)
        else:
            log.warning("No cameras discovered — will retry next cycle")
        check_system_memory()
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
