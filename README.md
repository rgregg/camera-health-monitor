# Camera Health Monitor

Automatically detects and reboots Reolink cameras whose RTSP service has crashed while the camera remains otherwise online.

Runs as a Docker container alongside [Frigate](https://frigate.video/). Discovers cameras dynamically from Frigate's config — no manual camera list to maintain.

## How It Works

1. Fetches the camera list from Frigate's API (parses go2rtc stream URLs for camera IPs)
2. Checks RTSP port 554 on each unique camera IP via TCP connect
3. If RTSP is down, retries once after 5 seconds to rule out transient blips
4. If still down, reboots the camera via the Reolink HTTP API
5. Tracks reboots per camera — sends a Home Assistant notification if a camera is rebooted 3+ times in an hour (persistent failure)

## Configuration

All configuration is via environment variables:

| Variable | Description | Default |
|---|---|---|
| `FRIGATE_URL` | Frigate API base URL | `http://frigate:5000` |
| `CAMERA_USER` | Reolink camera username | (required) |
| `CAMERA_PASSWORD` | Reolink camera password | (required) |
| `CHECK_INTERVAL` | Seconds between check cycles | `120` |
| `HA_URL` | Home Assistant URL for notifications | (optional) |
| `HA_TOKEN` | HA long-lived access token | (optional) |
| `REBOOT_THRESHOLD` | Reboots in 1 hour before HA alert | `3` |

## Deployment

```bash
# Clone the repo
git clone https://github.com/rgregg/camera-health-monitor.git
cd camera-health-monitor

# Create .env file with your credentials
cat > .env <<EOF
CAMERA_USER=admin
CAMERA_PASSWORD=your_camera_password
HA_URL=http://homeassistant.local:8123
HA_TOKEN=your_long_lived_access_token
EOF

# Start the container
docker compose up -d
```

The container needs network access to both the Frigate API and your camera IPs. By default it joins the `frigate_default` Docker network. If your setup is different, adjust the network config in `docker-compose.yml` or use `network_mode: "host"`.

## Logs

```bash
docker logs -f camera-health-monitor
```

Example output:
```
2026-04-08 17:20:38 [INFO] Camera health monitor starting
2026-04-08 17:20:38 [INFO] Frigate URL: http://frigate:5000
2026-04-08 17:20:38 [INFO] Check interval: 120s
2026-04-08 17:20:38 [INFO] Discovered 13 cameras across 12 unique IPs
2026-04-08 17:20:41 [INFO] Cycle complete: 12/12 healthy
```

## Requirements

- Python 3.12+ (included in Docker image)
- No pip dependencies — stdlib only
- Reolink cameras with HTTP API access
- Frigate with go2rtc streams configured
