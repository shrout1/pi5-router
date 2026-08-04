#!/usr/bin/env python3
# pi5-router status dashboard backend.
# Binds only to 127.0.0.1 and AP_IP -- never 0.0.0.0 -- so there is no
# listening socket at all on the hotel-facing interfaces, same posture as
# sshd's ListenAddress and xrdp's port=tcp://<ip>:<port> restriction.
import fcntl
import json
import re
import socket
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, available_timezones

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.serving import run_simple

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

CONF_PATH = Path("/etc/pi5-router/router.conf")
RUNTIME_ENV_PATH = Path("/etc/pi5-router/runtime.env")
SPEEDTEST_RESULT_PATH = Path("/var/lib/pi5-router/speedtest.json")
LEASES_PATH = Path("/var/lib/misc/dnsmasq.leases")

# Same lock file the NM dispatcher script (90-pi5-router-speedtest) uses --
# shared so "running" reflects a test fired automatically on uplink-up, not
# just one started from this dashboard's own button.
SPEEDTEST_LOCK_PATH = Path("/run/pi5-router-speedtest.lock")

SERVICE_UNITS = [
    "hostapd",
    "dnsmasq",
    "nftables",
    "ssh",
    "xrdp",
    "xrdp-sesman",
    "avahi-daemon",
    "rpcbind.socket",
    "pi5-router-dashboard",
]

_KV_RE = re.compile(r'^([A-Z_][A-Z0-9_]*)=["\']?(.*?)["\']?\s*(?:#.*)?$')


def _parse_kv_file(path):
    values = {}
    if not path.exists():
        return values
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        m = _KV_RE.match(line)
        if m:
            values[m.group(1)] = m.group(2)
    return values


def load_config():
    return _parse_kv_file(CONF_PATH), _parse_kv_file(RUNTIME_ENV_PATH)


def run(cmd, timeout=5):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return out.stdout.strip(), out.returncode
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return "", 1


def run_capture(cmd, timeout=10):
    """Like run(), but also returns stderr -- for callers that want nmcli's
    actual error text (e.g. wrong password) instead of just a return code."""
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return out.stdout.strip(), out.stderr.strip(), out.returncode
    except subprocess.TimeoutExpired:
        return "", "timed out", 1
    except (FileNotFoundError, OSError) as e:
        return "", str(e), 1


def systemctl_is_active(unit):
    out, _ = run(["systemctl", "is-active", unit])
    return out or "unknown"


def get_services_status():
    return {unit: systemctl_is_active(unit) for unit in SERVICE_UNITS}


def get_login_sessions():
    ids_out, rc = run(["loginctl", "list-sessions", "--no-legend"])
    if rc != 0:
        return []

    sessions = []
    for line in ids_out.splitlines():
        parts = line.split()
        if not parts:
            continue
        session_id = parts[0]

        # Labeled KEY=value output (not --value) so parsing is safe even
        # when a property is unset -- an unset property is silently omitted
        # from --value's plain output, which would shift every subsequent
        # line and corrupt positional parsing.
        out, rc = run(
            [
                "loginctl", "show-session", session_id,
                "-p", "Name", "-p", "Type", "-p", "Remote", "-p", "RemoteHost",
                "-p", "Service", "-p", "State", "-p", "TTY", "-p", "Timestamp",
            ]
        )
        if rc != 0:
            continue
        info = {}
        for prop_line in out.splitlines():
            key, sep, value = prop_line.partition("=")
            if sep:
                info[key] = value

        service = info.get("Service", "")
        if service == "systemd-user":
            continue  # internal companion session, not a real login

        service_l = service.lower()
        if "sshd" in service_l:
            connection = "ssh"
        elif "xrdp" in service_l:
            connection = "rdp"
        elif "lightdm" in service_l or service_l in ("login", "getty"):
            connection = "local"
        else:
            connection = service or "unknown"

        sessions.append(
            {
                "id": session_id,
                "user": info.get("Name", ""),
                "connection": connection,
                "source": info.get("RemoteHost") or None,
                "tty": info.get("TTY") or None,
                "since": info.get("Timestamp") or None,
                "state": info.get("State", ""),
            }
        )
    return sessions


def get_ap_status(conf, runtime):
    ap_if = runtime.get("AP_WIFI_IF", "")
    status = {
        "ssid": conf.get("AP_SSID", ""),
        "band": conf.get("AP_BAND", ""),
        "channel": conf.get("AP_CHANNEL", ""),
        "ip": conf.get("AP_IP", ""),
        "interface": ap_if,
        "hostapd": systemctl_is_active("hostapd"),
        "client_count": 0,
    }
    if ap_if:
        out, rc = run(["iw", "dev", ap_if, "station", "dump"])
        if rc == 0:
            status["client_count"] = len(re.findall(r"^Station ", out, re.MULTILINE))
    return status


_AP_SSID_MAX_BYTES = 32
HOSTAPD_CONF_PATH = Path("/etc/hostapd/hostapd.conf")


def update_ap_credentials(ssid, passphrase):
    ssid = (ssid or "").strip()
    if not ssid:
        return False, "SSID is required"
    if len(ssid.encode("utf-8")) > _AP_SSID_MAX_BYTES:
        return False, f"SSID must be at most {_AP_SSID_MAX_BYTES} bytes"
    if re.search(r"[\r\n#]", ssid):
        return False, "SSID can't contain newlines or '#'"
    if passphrase and not (8 <= len(passphrase) <= 63):
        return False, "Passphrase must be 8-63 characters (WPA2 requirement)"

    # Blank passphrase means "keep the current one" -- never echo the
    # existing value back to the client to prefill it, so this is the only
    # way to change just the SSID.
    def _apply(path, ssid_pattern, ssid_line, pass_pattern, pass_line):
        if not path.exists():
            return
        text = path.read_text()
        if re.search(ssid_pattern, text, re.MULTILINE):
            text = re.sub(ssid_pattern, ssid_line, text, flags=re.MULTILINE)
        else:
            text += f"\n{ssid_line}\n"
        if passphrase:
            if re.search(pass_pattern, text, re.MULTILINE):
                text = re.sub(pass_pattern, pass_line, text, flags=re.MULTILINE)
            else:
                text += f"\n{pass_line}\n"
        path.write_text(text)

    _apply(
        CONF_PATH,
        r"^AP_SSID=.*$", f'AP_SSID="{ssid}"',
        r"^AP_PASSPHRASE=.*$", f'AP_PASSPHRASE="{passphrase}"',
    )
    _apply(
        HOSTAPD_CONF_PATH,
        r"^ssid=.*$", f"ssid={ssid}",
        r"^wpa_passphrase=.*$", f"wpa_passphrase={passphrase}",
    )

    _, rc = run(["systemctl", "restart", "hostapd"], timeout=15)
    if rc != 0:
        return False, "config updated but hostapd failed to restart -- check `systemctl status hostapd`"
    return True, None


def _operstate(iface):
    try:
        return Path(f"/sys/class/net/{iface}/operstate").read_text().strip()
    except OSError:
        return "unknown"


def _default_route_iface():
    out, rc = run(["ip", "route", "show", "default"])
    if rc != 0:
        return None
    for line in out.splitlines():
        m = re.search(r"\bdev (\S+)", line)
        if m:
            return m.group(1)
    return None


def _iface_ipv4(iface):
    out, rc = run(["ip", "-4", "-o", "addr", "show", "dev", iface])
    if rc != 0:
        return None
    m = re.search(r"inet (\S+)", out)
    return m.group(1) if m else None


def _wifi_ssid(iface):
    out, rc = run(["iw", "dev", iface, "link"])
    if rc != 0:
        return None
    m = re.search(r"^\s*SSID:\s*(.+)$", out, re.MULTILINE)
    return m.group(1).strip() if m else None


def _iface_gateway(iface):
    # The next hop this interface's own default route points at -- i.e.
    # the hotel's (or whatever upstream's) router, one hop across the
    # link, before any further NAT/routing on their side.
    out, rc = run(["ip", "route", "show", "default", "dev", iface])
    if rc != 0:
        return None
    m = re.search(r"\bvia (\S+)", out)
    return m.group(1) if m else None


def get_uplink_status(runtime):
    eth_if = runtime.get("ETH_IF", "")
    usb_if = runtime.get("USB_WIFI_IF", "")
    active_if = _default_route_iface()

    interfaces = {}
    for label, iface in (("ethernet", eth_if), ("wifi", usb_if)):
        if not iface:
            continue
        info = {
            "interface": iface,
            "state": _operstate(iface),
            "active": iface == active_if,
            "ip": _iface_ipv4(iface),
            "gateway": _iface_gateway(iface),
        }
        if label == "wifi":
            info["ssid"] = _wifi_ssid(iface)
        interfaces[label] = info

    active_label = next((label for label, info in interfaces.items() if info["active"]), None)

    network_name = None
    if active_label == "wifi":
        network_name = interfaces["wifi"].get("ssid")
    elif active_label == "ethernet":
        network_name = "Wired"

    return {
        "interfaces": interfaces,
        "active": active_label,
        "active_interface": active_if,
        "network_name": network_name,
    }


_NMCLI_UNESCAPED_COLON_RE = re.compile(r"(?<!\\):")


def _nmcli_unescape(value):
    return value.replace("\\:", ":").replace("\\\\", "\\")


def get_wifi_networks(runtime):
    """Scan for networks visible to the USB wifi uplink adapter -- never the
    onboard AP radio, which hostapd owns and NetworkManager doesn't manage."""
    usb_if = runtime.get("USB_WIFI_IF", "")
    if not usb_if:
        return []
    out, rc = run(
        [
            "nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY,IN-USE",
            "device", "wifi", "list", "ifname", usb_if, "--rescan", "yes",
        ],
        timeout=20,
    )
    if rc != 0:
        return []

    best = {}
    for line in out.splitlines():
        if not line:
            continue
        parts = [_nmcli_unescape(p) for p in _NMCLI_UNESCAPED_COLON_RE.split(line)]
        if len(parts) < 4:
            continue
        ssid, signal, security, in_use = parts[0], parts[1], parts[2], parts[3]
        if not ssid:
            continue
        try:
            signal_i = int(signal)
        except ValueError:
            signal_i = 0
        existing = best.get(ssid)
        if existing is None or signal_i > existing["signal"]:
            best[ssid] = {
                "ssid": ssid,
                "signal": signal_i,
                "secured": bool(security),
                "in_use": in_use == "*",
            }
    return sorted(best.values(), key=lambda n: n["signal"], reverse=True)


def connect_wifi(runtime, ssid, password):
    usb_if = runtime.get("USB_WIFI_IF", "")
    if not usb_if:
        return False, "no wifi uplink interface detected"
    if not ssid:
        return False, "network name is required"

    cmd = ["nmcli", "device", "wifi", "connect", ssid, "ifname", usb_if]
    if password:
        cmd += ["password", password]

    out, err, rc = run_capture(cmd, timeout=30)
    if rc != 0:
        return False, err or out or "connection failed"
    return True, None


# Scan+associate+DHCP for a hotel guest network can take 15-20+ seconds.
# Blocking the HTTP request for that whole time risks the browser aborting
# the stalled fetch before the response arrives, even though the backend
# finishes successfully underneath it -- same failure mode the speed test
# had, fixed the same way: return immediately, run it in the background,
# and let the dashboard's regular polling reveal the real outcome.
_wifi_connect_state = {"status": "idle", "ssid": None, "message": None}
_wifi_connect_state_lock = threading.Lock()


def _wifi_connect_bg(runtime, ssid, password):
    ok, message = connect_wifi(runtime, ssid, password)
    with _wifi_connect_state_lock:
        _wifi_connect_state.update({"status": "ok" if ok else "error", "ssid": ssid, "message": message})


def start_wifi_connect(runtime, ssid, password):
    with _wifi_connect_state_lock:
        if _wifi_connect_state["status"] == "connecting":
            return False
        _wifi_connect_state.update({"status": "connecting", "ssid": ssid, "message": None})
    threading.Thread(target=_wifi_connect_bg, args=(runtime, ssid, password), daemon=True).start()
    return True


def get_wifi_connect_state():
    with _wifi_connect_state_lock:
        return dict(_wifi_connect_state)


_wan_ip_cache = {"value": None, "ts": 0.0}
_WAN_IP_TTL = 60


def get_wan_ip():
    now = time.time()
    if _wan_ip_cache["value"] and now - _wan_ip_cache["ts"] < _WAN_IP_TTL:
        return _wan_ip_cache["value"]
    out, rc = run(["curl", "-s", "--max-time", "3", "https://api.ipify.org"], timeout=4)
    if rc == 0 and re.match(r"^\d{1,3}(\.\d{1,3}){3}$", out):
        _wan_ip_cache["value"] = out
        _wan_ip_cache["ts"] = now
        return out
    return None


def _parse_leases():
    leases = {}
    if not LEASES_PATH.exists():
        return leases
    for line in LEASES_PATH.read_text().splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        mac, ip, hostname = parts[1], parts[2], parts[3]
        leases[mac.lower()] = {"ip": ip, "hostname": None if hostname == "*" else hostname}
    return leases


def get_clients(runtime):
    ap_if = runtime.get("AP_WIFI_IF", "")
    if not ap_if:
        return []
    out, rc = run(["iw", "dev", ap_if, "station", "dump"])
    if rc != 0:
        return []
    leases = _parse_leases()
    clients = []
    current = None
    for raw_line in out.splitlines():
        m = re.match(r"^Station (\S+) \(on ", raw_line)
        if m:
            if current:
                clients.append(current)
            mac = m.group(1).lower()
            lease = leases.get(mac, {})
            current = {
                "mac": mac,
                "ip": lease.get("ip"),
                "hostname": lease.get("hostname"),
                "connected_seconds": None,
                "rx_bytes": None,
                "tx_bytes": None,
            }
            continue
        if current is None:
            continue
        line = raw_line.strip()
        if line.startswith("connected time:"):
            m = re.search(r"(\d+) seconds", line)
            if m:
                current["connected_seconds"] = int(m.group(1))
        elif line.startswith("rx bytes:"):
            m = re.search(r"(\d+)", line)
            if m:
                current["rx_bytes"] = int(m.group(1))
        elif line.startswith("tx bytes:"):
            m = re.search(r"(\d+)", line)
            if m:
                current["tx_bytes"] = int(m.group(1))
    if current:
        clients.append(current)
    return clients


def _speedtest_is_running():
    try:
        SPEEDTEST_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(SPEEDTEST_LOCK_PATH, "a+") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(fh, fcntl.LOCK_UN)
            return False
    except OSError:
        return False


def get_speedtest_result():
    if _speedtest_is_running():
        return {"status": "running"}
    if not SPEEDTEST_RESULT_PATH.exists():
        return {"status": "never_run"}
    try:
        return json.loads(SPEEDTEST_RESULT_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {"status": "never_run"}


def _resolve_ip(hostname):
    if not hostname:
        return None
    try:
        return socket.gethostbyname(hostname)
    except OSError:
        return None


def _speedtest_failed(provider, provider_label, message):
    return {
        "status": "failed",
        "provider": provider,
        "provider_label": provider_label,
        "timestamp": time.time(),
        "message": message,
    }


def _run_ookla():
    out, rc = run(["speedtest-cli", "--json"], timeout=90)
    if rc != 0:
        return _speedtest_failed("ookla", "Ookla (Speedtest.net)", "speedtest-cli failed")
    data = json.loads(out)
    server = data.get("server") or {}
    host = (server.get("host") or "").split(":")[0] or None
    return {
        "status": "ok",
        "provider": "ookla",
        "provider_label": "Ookla (Speedtest.net)",
        "timestamp": time.time(),
        "download_mbps": round(data.get("download", 0) / 1_000_000, 2),
        "upload_mbps": round(data.get("upload", 0) / 1_000_000, 2),
        "ping_ms": round(data.get("ping", 0), 1),
        "sponsor": server.get("sponsor"),
        "server_name": server.get("name"),
        "server_host": host,
        "server_ip": _resolve_ip(host),
        "server_location": ", ".join(filter(None, [server.get("name"), server.get("country")])) or None,
        "message": None,
    }


# speed.cloudflare.com is anycast -- the resolved IP is *an* edge IP, not
# necessarily the exact one this particular request routed to.

_BROWSER_UA = "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"


def _run_cloudflare():
    # /__down and /__up 403 without a browser-like User-Agent (verified
    # live). With one, /__down's response headers directly carry the edge
    # PoP's city/country/colo -- no need to guess from a static IATA-code
    # table or hit the separate /cdn-cgi/trace endpoint at all.
    import urllib.request

    city = country = colo = None
    try:
        pings = []
        for _ in range(3):
            req = urllib.request.Request(
                "https://speed.cloudflare.com/__down?bytes=1000", headers={"User-Agent": _BROWSER_UA}
            )
            start = time.monotonic()
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
                city = resp.headers.get("city") or city
                country = resp.headers.get("country") or country
                colo = resp.headers.get("colo") or colo
            pings.append((time.monotonic() - start) * 1000)
        ping_ms = min(pings)

        down_bytes = 25_000_000
        req = urllib.request.Request(
            f"https://speed.cloudflare.com/__down?bytes={down_bytes}", headers={"User-Agent": _BROWSER_UA}
        )
        start = time.monotonic()
        with urllib.request.urlopen(req, timeout=30) as resp:
            actual = len(resp.read())
        down_elapsed = time.monotonic() - start
        download_mbps = round((actual * 8 / 1_000_000) / down_elapsed, 2)

        up_bytes = 10_000_000
        payload = b"0" * up_bytes
        req = urllib.request.Request(
            "https://speed.cloudflare.com/__up", data=payload, headers={"User-Agent": _BROWSER_UA}, method="POST"
        )
        start = time.monotonic()
        urllib.request.urlopen(req, timeout=30).read()
        up_elapsed = time.monotonic() - start
        upload_mbps = round((up_bytes * 8 / 1_000_000) / up_elapsed, 2)
    except Exception as e:
        return _speedtest_failed("cloudflare", "Cloudflare", f"cloudflare speed test failed: {e}")

    return {
        "status": "ok",
        "provider": "cloudflare",
        "provider_label": "Cloudflare",
        "timestamp": time.time(),
        "download_mbps": download_mbps,
        "upload_mbps": upload_mbps,
        "ping_ms": round(ping_ms, 1),
        "sponsor": "Cloudflare",
        "server_name": f"{city} ({colo})" if city else colo,
        "server_host": "speed.cloudflare.com",
        "server_ip": _resolve_ip("speed.cloudflare.com"),
        "server_location": ", ".join(filter(None, [city, country])) or None,
        "message": None,
    }


def _run_librespeed():
    out, rc = run(["librespeed-cli", "--json"], timeout=90)
    if rc != 0:
        return _speedtest_failed("librespeed", "LibreSpeed", "librespeed-cli failed")
    try:
        data = json.loads(out)[0]
    except (json.JSONDecodeError, IndexError, KeyError):
        return _speedtest_failed("librespeed", "LibreSpeed", "unexpected librespeed-cli output")

    from urllib.parse import urlparse

    server = data.get("server") or {}
    host = urlparse(server.get("url") or "").hostname
    return {
        "status": "ok",
        "provider": "librespeed",
        "provider_label": "LibreSpeed",
        "timestamp": time.time(),
        "download_mbps": data.get("download"),
        "upload_mbps": data.get("upload"),
        "ping_ms": data.get("ping"),
        "sponsor": None,
        "server_name": server.get("name"),
        "server_host": host,
        "server_ip": _resolve_ip(host),
        "server_location": server.get("name"),
        "message": None,
    }


def _run_fastcom():
    # No official API -- this scrapes the same undocumented token/endpoint
    # fast-cli and similar tools rely on. Confirmed live (2026-08-04) that
    # the token pattern below is NOT currently found in fast.com's bundle,
    # so this fails gracefully today; left as a genuine attempt (not a
    # stub) in case Netflix's client format reverts or a future maintainer
    # updates the pattern.
    import urllib.request
    from urllib.parse import urlparse

    def _get(url, extra_headers=None, timeout=10):
        req_headers = {"User-Agent": _BROWSER_UA}
        req_headers.update(extra_headers or {})
        return urllib.request.urlopen(urllib.request.Request(url, headers=req_headers), timeout=timeout)

    label = "fast.com"
    try:
        html = _get("https://fast.com/").read().decode()
        m = re.search(r'<script src="(/app-[^"]+\.js)"', html)
        if not m:
            raise ValueError("could not locate fast.com's app bundle")
        bundle = _get(f"https://fast.com{m.group(1)}").read().decode()
        token_match = re.search(r'token"\s*:\s*"([a-zA-Z0-9_-]+)"', bundle)
        if not token_match:
            raise ValueError("token not found in fast.com's app bundle (Netflix changed their client)")
        token = token_match.group(1)

        api_url = f"https://api.fast.com/netflix/speedtest/v2?https=true&token={token}&urlCount=3"
        api_data = json.loads(_get(api_url).read())
        targets = api_data.get("targets") or []
        if not targets:
            raise ValueError("fast.com API returned no test targets")

        target = targets[0]
        target_url = target["url"]
        location = target.get("location") or {}
        host = urlparse(target_url).hostname

        pings = []
        for _ in range(3):
            start = time.monotonic()
            _get(target_url, {"Range": "bytes=0-1"}).read()
            pings.append((time.monotonic() - start) * 1000)

        down_limit = 25_000_000
        total = 0
        start = time.monotonic()
        with _get(target_url, timeout=30) as resp:
            while total < down_limit:
                chunk = resp.read(1_000_000)
                if not chunk:
                    break
                total += len(chunk)
        elapsed = time.monotonic() - start
        download_mbps = round((total * 8 / 1_000_000) / elapsed, 2) if elapsed > 0 else None
    except Exception as e:
        return _speedtest_failed(
            "fastcom", label, f"fast.com's unofficial API is currently unavailable ({e})"
        )

    return {
        "status": "ok",
        "provider": "fastcom",
        "provider_label": label,
        "timestamp": time.time(),
        "download_mbps": download_mbps,
        "upload_mbps": None,  # fast.com's unofficial API doesn't expose an upload measurement here
        "ping_ms": round(min(pings), 1) if pings else None,
        "sponsor": None,
        "server_name": target.get("name"),
        "server_host": host,
        "server_ip": _resolve_ip(host),
        "server_location": ", ".join(filter(None, [location.get("city"), location.get("country")])) or None,
        "message": None,
    }


SPEEDTEST_PROVIDERS = {
    "ookla": (_run_ookla, "Ookla (Speedtest.net)"),
    "cloudflare": (_run_cloudflare, "Cloudflare"),
    "librespeed": (_run_librespeed, "LibreSpeed"),
    "fastcom": (_run_fastcom, "fast.com"),
}


def _run_speedtest_bg(provider):
    SPEEDTEST_RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SPEEDTEST_LOCK_PATH, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            run_fn, _ = SPEEDTEST_PROVIDERS.get(provider, SPEEDTEST_PROVIDERS["ookla"])
            result = run_fn()
            SPEEDTEST_RESULT_PATH.write_text(json.dumps(result))
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def trigger_speedtest(provider):
    if provider not in SPEEDTEST_PROVIDERS:
        return False
    if _speedtest_is_running():
        return False
    threading.Thread(target=_run_speedtest_bg, args=(provider,), daemon=True).start()
    return True


def _utc_offset_minutes(tz_name):
    try:
        offset = datetime.now(ZoneInfo(tz_name)).utcoffset()
    except Exception:
        return None
    if offset is None:
        return None
    return int(offset.total_seconds() // 60)


def _format_utc_offset_minutes(total_minutes):
    sign = "+" if total_minutes >= 0 else "-"
    hours, minutes = divmod(abs(total_minutes), 60)
    return f"UTC{sign}{hours}:{minutes:02d}" if minutes else f"UTC{sign}{hours}"


# Computed once and reused for the life of the process: the offset/ordering
# can't meaningfully change without the system clock or tzdata changing,
# and this dashboard has to keep working before the uplink ever reaches the
# internet, so there's nothing to "refresh" it against anyway.
_TIMEZONE_LIST_CACHE = None


def get_timezone_list():
    global _TIMEZONE_LIST_CACHE
    if _TIMEZONE_LIST_CACHE is None:
        entries = [(_utc_offset_minutes(name) or 0, name) for name in available_timezones()]
        # Descending by UTC offset (UTC+14 first, UTC-12 last), then
        # alphabetical among zones that share the same offset.
        entries.sort(key=lambda e: (-e[0], e[1]))
        _TIMEZONE_LIST_CACHE = [
            {"name": name, "offset": _format_utc_offset_minutes(minutes)} for minutes, name in entries
        ]
    return _TIMEZONE_LIST_CACHE


def get_clock_status():
    tz, _ = run(["timedatectl", "show", "-p", "Timezone", "--value"])
    ntp_synced, _ = run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
    return {
        "now": datetime.now().astimezone().isoformat(),
        "timezone": tz or None,
        "ntp_synchronized": ntp_synced == "yes",
    }


_clock_lock = threading.Lock()


def set_clock(date_str, time_str, tz):
    """date_str: "YYYY-MM-DD", time_str: "HH:MM:SS", tz: IANA zone name or None."""
    try:
        dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False, "invalid date/time"

    if tz and tz not in available_timezones():
        return False, "unknown timezone"

    with _clock_lock:
        if tz:
            _, rc = run(["timedatectl", "set-timezone", tz], timeout=10)
            if rc != 0:
                return False, "failed to set timezone"

        # timedatectl refuses manual set-time while NTP is managing the
        # clock, so drop it, set the time, then turn it back on -- once
        # the uplink clears the captive portal, timesyncd takes over and
        # corrects any drift on its own.
        run(["timedatectl", "set-ntp", "false"], timeout=10)
        _, rc = run(["timedatectl", "set-time", dt.strftime("%Y-%m-%d %H:%M:%S")], timeout=10)
        run(["timedatectl", "set-ntp", "true"], timeout=10)

    if rc != 0:
        return False, "failed to set time"
    return True, None


app = Flask(__name__, static_folder=None)


@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/<path:filename>")
def static_files(filename):
    return send_from_directory(STATIC_DIR, filename)


@app.route("/api/status")
def api_status():
    conf, runtime = load_config()
    return jsonify(
        {
            "ap": get_ap_status(conf, runtime),
            "uplink": get_uplink_status(runtime),
            "wifi_connect": get_wifi_connect_state(),
            "wan_ip": get_wan_ip(),
            "clients": get_clients(runtime),
            "sessions": get_login_sessions(),
            "services": get_services_status(),
            "speedtest": get_speedtest_result(),
            "clock": get_clock_status(),
            "ssh_port": conf.get("SSH_PORT"),
            "rdp_port": conf.get("RDP_PORT"),
        }
    )


@app.route("/api/speedtest/run", methods=["POST"])
def api_speedtest_run():
    payload = request.get_json(silent=True) or {}
    provider = payload.get("provider") or "ookla"
    if provider not in SPEEDTEST_PROVIDERS:
        return jsonify({"status": "error", "message": f"unknown provider: {provider}"}), 400
    started = trigger_speedtest(provider)
    return jsonify({"status": "started" if started else "already_running"})


@app.route("/api/timezones")
def api_timezones():
    return jsonify(get_timezone_list())


@app.route("/api/wifi/scan")
def api_wifi_scan():
    _, runtime = load_config()
    return jsonify(get_wifi_networks(runtime))


@app.route("/api/wifi/connect", methods=["POST"])
def api_wifi_connect():
    payload = request.get_json(silent=True) or {}
    ssid = (payload.get("ssid") or "").strip()
    password = payload.get("password") or ""

    if not ssid:
        return jsonify({"status": "error", "message": "network name is required"}), 400

    _, runtime = load_config()
    started = start_wifi_connect(runtime, ssid, password)
    if not started:
        return jsonify({"status": "already_connecting"}), 409
    return jsonify({"status": "started"})


@app.route("/api/clock/set", methods=["POST"])
def api_clock_set():
    payload = request.get_json(silent=True) or {}
    date_str = payload.get("date")
    time_str = payload.get("time")
    tz = payload.get("timezone") or None

    if not date_str or not time_str:
        return jsonify({"status": "error", "message": "date and time are required"}), 400

    ok, message = set_clock(date_str, time_str, tz)
    if not ok:
        return jsonify({"status": "error", "message": message}), 400
    return jsonify({"status": "ok", "clock": get_clock_status()})


@app.route("/api/ap/update", methods=["POST"])
def api_ap_update():
    payload = request.get_json(silent=True) or {}
    ssid = payload.get("ssid") or ""
    passphrase = payload.get("passphrase") or ""

    ok, message = update_ap_credentials(ssid, passphrase)
    if not ok:
        return jsonify({"status": "error", "message": message}), 400
    conf, runtime = load_config()
    return jsonify({"status": "ok", "ap": get_ap_status(conf, runtime)})


def main():
    conf, _ = load_config()
    port = int(conf.get("DASHBOARD_PORT", "8080"))
    ap_ip = conf.get("AP_IP", "172.24.1.1")

    threads = [
        threading.Thread(
            target=run_simple, args=("127.0.0.1", port, app), kwargs={"threaded": True}, daemon=True
        ),
        threading.Thread(
            target=run_simple, args=(ap_ip, port, app), kwargs={"threaded": True}, daemon=True
        ),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


if __name__ == "__main__":
    main()
