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
import uuid
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
    "wg-quick@wg-pi5",
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


# ---------------------------------------------------------------------------
# USB wifi uplink adapter selection -- live, not install-time. Any number of
# adapters can be plugged in, unplugged, or swapped at any time; which one
# (if any) is the actual uplink is a runtime choice reconciled against
# nftables/sysctl here, not a name baked into a config file by install.sh.
# ---------------------------------------------------------------------------
UPLINK_STATE_PATH = Path("/etc/pi5-router/uplink-if.json")


def _ifindex(iface):
    try:
        return int(Path(f"/sys/class/net/{iface}/ifindex").read_text().strip())
    except (OSError, ValueError):
        return 10**9  # unknown -- sort last, never picked as the "first plugged in" default


def _is_usb_iface(iface):
    try:
        devpath = str(Path(f"/sys/class/net/{iface}/device").resolve())
    except OSError:
        return False
    return "/usb" in devpath


def list_uplink_wifi_candidates(runtime):
    """Every wifi device NetworkManager currently sees that isn't the onboard
    AP radio -- evaluated live on every call, not an install-time snapshot.
    Same USB-vs-onboard sysfs heuristic install.sh uses to find the AP radio;
    keep the two in sync if this changes."""
    ap_if = runtime.get("AP_WIFI_IF", "")
    out, rc = run(["nmcli", "-t", "-f", "DEVICE,TYPE", "device", "status"])
    if rc != 0:
        return []
    candidates = []
    for line in out.splitlines():
        parts = line.split(":")
        if len(parts) < 2:
            continue
        dev, typ = parts[0], parts[1]
        if typ != "wifi" or dev == ap_if:
            continue
        if _is_usb_iface(dev):
            candidates.append(dev)
    # Lower ifindex = detected earlier by the kernel -- the "first plugged
    # in" tiebreak for the default when the user hasn't chosen one.
    candidates.sort(key=_ifindex)
    return candidates


def _iface_description(iface):
    out, rc = run(["nmcli", "-t", "-f", "GENERAL.VENDOR,GENERAL.PRODUCT", "device", "show", iface])
    vendor = product = None
    for line in out.splitlines():
        key, _, val = line.partition(":")
        val = val.strip()
        if val and val != "--":
            if key == "GENERAL.VENDOR":
                vendor = val
            elif key == "GENERAL.PRODUCT":
                product = val
    return " ".join(filter(None, [vendor, product])) or None


def _load_uplink_state():
    try:
        return json.loads(UPLINK_STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {"selected": None}


def _save_uplink_state(selected):
    UPLINK_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    UPLINK_STATE_PATH.write_text(json.dumps({"selected": selected}))


def get_active_uplink_wifi_if(runtime, candidates=None):
    if candidates is None:
        candidates = list_uplink_wifi_candidates(runtime)
    if not candidates:
        return None
    chosen = _load_uplink_state().get("selected")
    return chosen if chosen in candidates else candidates[0]


def get_uplink_wifi_adapters(runtime):
    candidates = list_uplink_wifi_candidates(runtime)
    active = get_active_uplink_wifi_if(runtime, candidates)
    return {
        "active": active,
        "auto": _load_uplink_state().get("selected") is None,
        "candidates": [{"interface": c, "label": _iface_description(c) or c} for c in candidates],
    }


_uplink_ipv6_disabled = set()


def apply_uplink_selection(runtime):
    """Reconciles live system state with whichever USB wifi adapter is
    currently selected: disables IPv6 on every candidate (sysctl.conf.tmpl
    only covers ETH_IF/AP_WIFI_IF -- see its comment), keeps exactly the
    active one (plus ETH_IF/VPN_IF) in the nftables `uplinks` sets, and
    disconnects any other candidate so it can't quietly win the default
    route out from under the selected one."""
    candidates = list_uplink_wifi_candidates(runtime)
    active = get_active_uplink_wifi_if(runtime, candidates)

    for c in candidates:
        if c not in _uplink_ipv6_disabled:
            run(["sysctl", "-w", f"net.ipv6.conf.{c}.disable_ipv6=1"])
            _uplink_ipv6_disabled.add(c)

    members = [i for i in (runtime.get("ETH_IF", ""), VPN_IF, active) if i]
    elements = "{ " + ", ".join(f'"{m}"' for m in members) + " }"
    for family, table in (("inet", "filter"), ("ip", "nat")):
        run(["nft", "flush", "set", family, table, "uplinks"])
        if members:
            run(["nft", "add", "element", family, table, "uplinks", elements])

    for c in candidates:
        if c != active:
            run(["nmcli", "device", "disconnect", c])

    return candidates, active


_last_uplink_signature = None


def maybe_reapply_uplink_selection(runtime):
    """Cheap to call on every poll -- only actually touches nft/sysctl/nmcli
    when the candidate list or active interface changed since last checked,
    so a hotplug or a dashboard selection takes effect within one poll cycle
    without churning the firewall on every request."""
    global _last_uplink_signature
    candidates = list_uplink_wifi_candidates(runtime)
    active = get_active_uplink_wifi_if(runtime, candidates)
    signature = (tuple(candidates), active)
    if signature != _last_uplink_signature:
        apply_uplink_selection(runtime)
        _last_uplink_signature = signature
    return candidates, active


def select_uplink_wifi_adapter(runtime, iface):
    """iface falsy clears the preference back to auto (first-detected)."""
    global _last_uplink_signature
    candidates = list_uplink_wifi_candidates(runtime)
    if iface and iface not in candidates:
        return False, "adapter not currently present"
    _save_uplink_state(iface or None)
    apply_uplink_selection(runtime)
    active = get_active_uplink_wifi_if(runtime, candidates)
    _last_uplink_signature = (tuple(candidates), active)
    return True, None


def get_uplink_status(runtime):
    eth_if = runtime.get("ETH_IF", "")
    usb_if = get_active_uplink_wifi_if(runtime)
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


def get_wifi_networks(iface):
    """Scan for networks visible to the given wifi uplink adapter -- never
    the onboard AP radio, which hostapd owns and NetworkManager doesn't
    manage. `iface` is whichever adapter is currently the active uplink
    choice (see get_active_uplink_wifi_if), not a fixed install-time name."""
    if not iface:
        return []
    out, rc = run(
        [
            "nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY,IN-USE",
            "device", "wifi", "list", "ifname", iface, "--rescan", "yes",
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


def connect_wifi(iface, ssid, password):
    if not iface:
        return False, "no wifi uplink adapter selected"
    if not ssid:
        return False, "network name is required"

    cmd = ["nmcli", "device", "wifi", "connect", ssid, "ifname", iface]
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


def _wifi_connect_bg(iface, ssid, password):
    ok, message = connect_wifi(iface, ssid, password)
    with _wifi_connect_state_lock:
        _wifi_connect_state.update({"status": "ok" if ok else "error", "ssid": ssid, "message": message})


def start_wifi_connect(iface, ssid, password):
    with _wifi_connect_state_lock:
        if _wifi_connect_state["status"] == "connecting":
            return False
        _wifi_connect_state.update({"status": "connecting", "ssid": ssid, "message": None})
    threading.Thread(target=_wifi_connect_bg, args=(iface, ssid, password), daemon=True).start()
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


# pi5-router-dashboard.service runs as root (see install.sh step 12), same
# trust boundary as the AP-credential and clock endpoints above -- no extra
# sudo/polkit wiring needed for systemctl reboot/poweroff.
_POWER_ACTIONS = {"reboot": "reboot", "shutdown": "poweroff"}
_POWER_DELAY_SECONDS = 5


def _power_action_bg(systemctl_verb):
    # Delayed so the client gets a real HTTP response and can show a
    # countdown before the box actually goes down, rather than the request
    # itself getting cut off mid-flight by the reboot/poweroff.
    time.sleep(_POWER_DELAY_SECONDS)
    run(["systemctl", systemctl_verb], timeout=10)


def trigger_power_action(action):
    verb = _POWER_ACTIONS.get(action)
    if not verb:
        return False
    threading.Thread(target=_power_action_bg, args=(verb,), daemon=True).start()
    return True


# ---------------------------------------------------------------------------
# VPN (WireGuard) -- connect/disconnect a tunnel, and choose whose gateway-
# bound traffic gets routed through it. Client-to-client traffic on the AP
# subnet never touches any of this: hostapd relays it directly (ap_isolate is
# off), so it stays off this box's routing table entirely regardless of mode.
# ---------------------------------------------------------------------------
VPN_IF = "wg-pi5"
# Table number doubles as the fwmark value -- both are arbitrary but have to
# agree with each other and with the `ip rule` that PostUp/PreDown install
# below; 51820 (WireGuard's default port) is just a memorable choice, not
# otherwise significant.
VPN_TABLE = 51820
VPN_FWMARK = 51820
# WG_CONF_PATH is the one file wg-quick@wg-pi5 actually reads -- the fixed
# `wg-pi5` interface name is what nftables/policy-routing already expect, so
# only one config can ever be "live" at a time. VPN_LIBRARY_DIR holds every
# *saved* config regardless of which (if any) is currently live; "activating"
# one copies its content into WG_CONF_PATH and (re)connects. This split is
# what lets the dashboard offer a named, switchable list instead of one
# anonymous config.
WG_CONF_PATH = Path("/etc/wireguard/wg-pi5.conf")
VPN_LIBRARY_DIR = Path("/etc/wireguard/pi5-router-vpn-library")
VPN_LIBRARY_INDEX_PATH = Path("/etc/pi5-router/vpn-library.json")
VPN_MODE_STATE_PATH = Path("/etc/pi5-router/vpn-mode.json")

VPN_PROVIDERS = {
    "protonvpn": {
        "label": "ProtonVPN",
        "protocol": "wireguard",
        "help_url": "https://account.protonvpn.com/downloads",
        "help": [
            "Open ProtonVPN's WireGuard configuration page (link above) in a new tab.",
            "Pick a server or location, then download the .conf file it generates.",
            "Open that file in a text editor, copy everything, and paste it below.",
        ],
        "fields": [
            {
                "name": "config",
                "label": "WireGuard configuration",
                "type": "textarea",
                "placeholder": "Paste the whole .conf file here",
            },
        ],
    },
    "openvpn": {
        "label": "OpenVPN",
        "protocol": "openvpn",
        "help": [
            "Get a unified .ovpn bundle from your OpenVPN server -- ca/cert/key/tls-crypt "
            "inline, not referencing separate files (e.g. one generated by home-base's "
            "dashboard, if that's what's running your server).",
            "Open it in a text editor, copy everything, and paste it below.",
        ],
        "fields": [
            {
                "name": "config",
                "label": "OpenVPN configuration (.ovpn)",
                "type": "textarea",
                "placeholder": "Paste the whole .ovpn file here",
            },
        ],
    },
}


def get_vpn_providers():
    return [
        {
            "id": pid,
            "label": p["label"],
            "fields": p["fields"],
            "help": p.get("help", []),
            "help_url": p.get("help_url"),
        }
        for pid, p in VPN_PROVIDERS.items()
    ]


_WG_ROUTING_KEYS_RE = re.compile(r"(?i)^(table|postup|predown|dns)\s*=")
# WireGuard keys are fixed-size (32-byte Curve25519 keys), so base64 always
# comes out to exactly 43 content characters plus one '=' padding char --
# not a range, an exact format. A generated real key confirms this
# (`wg genkey`): e.g. "OOHrBk+n5W3rtzoYFv1aJpNJoJojFqNVi2UKDZs6BHI=".
_WG_KEY_RE = re.compile(r"^[A-Za-z0-9+/]{43}=$")


def _wg_sections(raw):
    """Returns (interface_block, peer_block) -- the raw text following the
    first [Interface]/[Peer] header up to the next section header or EOF."""
    def _section(name):
        m = re.search(rf"(?im)^\[{name}\]\s*$(.*?)(?=^\[|\Z)", raw, re.DOTALL)
        return m.group(1) if m else ""
    return _section("interface"), _section("peer")


def _wg_value(block, key):
    m = re.search(rf"(?im)^\s*{key}\s*=\s*(.+?)\s*$", block)
    return m.group(1).strip() if m else None


def _validate_wg_config(raw):
    """Structural validation with a specific, actionable message per failure
    -- the paste-a-config step is the single most intimidating part of
    setting this up, so a generic "invalid config" error just adds to that.
    This is not cryptographic validation (it can't tell you the private key
    is *wrong*, only that it's missing or malformed-looking); wg-quick itself
    is still the real authority once a config is actually activated."""
    if not raw.strip():
        return False, "Paste a WireGuard configuration first."

    has_interface = re.search(r"(?im)^\[interface\]\s*$", raw)
    has_peer = re.search(r"(?im)^\[peer\]\s*$", raw)
    if not has_interface and not has_peer:
        return False, (
            "That doesn't look like a WireGuard config -- no [Interface] or "
            "[Peer] section found. Make sure you copied the whole file."
        )
    if not has_interface:
        return False, "Missing the [Interface] section -- looks like only part of the file was pasted."
    if not has_peer:
        return False, "Missing the [Peer] section -- looks like only part of the file was pasted."

    interface_block, peer_block = _wg_sections(raw)

    private_key = _wg_value(interface_block, "PrivateKey")
    if not private_key:
        return False, "Missing PrivateKey in [Interface] -- check you copied the whole file."
    if not _WG_KEY_RE.match(private_key):
        return False, "PrivateKey in [Interface] doesn't look like a valid WireGuard key -- check for a copy/paste error."

    if not _wg_value(interface_block, "Address"):
        return False, "Missing Address in [Interface]."

    public_key = _wg_value(peer_block, "PublicKey")
    if not public_key:
        return False, "Missing PublicKey in [Peer]."
    if not _WG_KEY_RE.match(public_key):
        return False, "PublicKey in [Peer] doesn't look like a valid WireGuard key -- check for a copy/paste error."

    endpoint = _wg_value(peer_block, "Endpoint")
    if not endpoint or ":" not in endpoint:
        return False, "Missing or malformed Endpoint in [Peer] (expected host:port)."

    if not _wg_value(peer_block, "AllowedIPs"):
        return False, "Missing AllowedIPs in [Peer]."

    return True, None


# ---------------------------------------------------------------------------
# OpenVPN client -- a second, independent VPN protocol alongside WireGuard.
# Only one tunnel (of either protocol) is ever active at a time; see
# _active_vpn_protocol/connect_vpn/disconnect_vpn below for how the two
# share the same VPN_TABLE/VPN_FWMARK policy-routing setup without
# conflicting.
# ---------------------------------------------------------------------------
OVPN_IF = "tun-pi5"
OVPN_CONF_NAME = "pi5-client"
OVPN_CONF_PATH = Path("/etc/openvpn/client/pi5-client.conf")
OVPN_HOOKS_DIR = Path("/opt/pi5-router/openvpn-hooks")

_OVPN_ROUTING_KEYS_RE = re.compile(r"(?i)^(route-nopull|script-security|up|down|dev)\s")


def _ovpn_block(raw, tag):
    m = re.search(rf"(?is)<{tag}>(.*?)</{tag}>", raw)
    return m.group(1).strip() if m else None


def _validate_ovpn_config(raw):
    """Same reasoning as _validate_wg_config: specific, actionable errors
    rather than one generic failure. Only validates the unified-bundle
    style (inline <ca>/<cert>/<key>, matching what home-base's dashboard
    generates) -- a config referencing external file paths for its
    credentials isn't supported, since there'd be nowhere for those
    referenced files to come from."""
    if not raw.strip():
        return False, "Paste an OpenVPN configuration first."

    if not re.search(r"(?im)^client\s*$", raw):
        return False, "Missing a 'client' line -- this doesn't look like an OpenVPN *client* config."

    if not re.search(r"(?im)^remote\s+\S+\s+\d+", raw):
        return False, "Missing a 'remote <host> <port>' line."

    if _ovpn_block(raw, "ca") is None:
        return False, "Missing an inline <ca>...</ca> block -- external file references aren't supported, paste the unified bundle."
    if _ovpn_block(raw, "cert") is None:
        return False, "Missing an inline <cert>...</cert> block."
    if _ovpn_block(raw, "key") is None:
        return False, "Missing an inline <key>...</key> block."

    return True, None


def _inject_ovpn_routing(conf_text):
    """Strips any pre-existing route-nopull/script-security/up/down/dev
    directives the pasted config already has and installs our own --
    route-nopull is the critical one: without it, whatever the server
    pushes (routes, possibly a full redirect-gateway) gets applied
    automatically, which could silently replace this box's own default
    route exactly like the ProtonVPN kill-switch/DNS-hijack failure mode
    found while building the VPN pane originally, just via OpenVPN instead
    of NetworkManager. The up/down scripts (static, installed once by
    install.sh -- see templates/openvpn-client-up.sh.tmpl) install a route
    in a dedicated table instead, the same approach WireGuard's own
    Table=/PostUp/PreDown take."""
    lines = [
        line for line in conf_text.splitlines() if not _OVPN_ROUTING_KEYS_RE.match(line.strip())
    ]
    prefix = [
        f"dev {OVPN_IF}",
        "route-nopull",
        "script-security 2",
        f"up {OVPN_HOOKS_DIR}/up.sh",
        f"down {OVPN_HOOKS_DIR}/down.sh",
    ]
    return "\n".join(prefix + lines) + "\n"


def _inject_wg_routing(conf_text):
    """Strip any Table/PostUp/PreDown the pasted config already has and
    install our own. Routes must land in a dedicated table, never `main` /
    the implicit "auto" wg-quick otherwise uses for a 0.0.0.0/0 peer -- auto
    would replace this box's own default route, taking the Pi itself off its
    real uplink rather than just routing the opted-in AP clients.

    Also strips any DNS= line. wg-quick only shells out to the `resolvconf`
    command when a config has one -- which isn't installed here (and
    shouldn't be: this is a split-tunnel, and letting the tunnel's DNS
    become the box's system-wide resolver is exactly the kind of
    system-wide-hijack-via-DNS failure mode that broke ProtonVPN's own
    official client during testing, just self-inflicted instead of
    theirs). Matches the documented limitation that DNS lookups don't
    follow the VPN mark."""
    lines = conf_text.splitlines()
    kept = []
    in_interface = False
    for line in lines:
        stripped = line.strip()
        if stripped.lower() == "[interface]":
            in_interface = True
        elif stripped.startswith("["):
            in_interface = False
        if in_interface and _WG_ROUTING_KEYS_RE.match(stripped):
            continue
        kept.append(line)

    result = []
    for line in kept:
        result.append(line)
        if line.strip().lower() == "[interface]":
            result.append(f"Table = {VPN_TABLE}")
            result.append(f"PostUp = ip rule add fwmark {VPN_FWMARK} table {VPN_TABLE}")
            result.append(f"PreDown = ip rule del fwmark {VPN_FWMARK} table {VPN_TABLE}")
    return "\n".join(result) + "\n"


def _load_vpn_library():
    try:
        data = json.loads(VPN_LIBRARY_INDEX_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        data = {}
    data.setdefault("configs", [])
    data.setdefault("active", None)
    return data


def _save_vpn_library(data):
    VPN_LIBRARY_INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    VPN_LIBRARY_INDEX_PATH.write_text(json.dumps(data))


def _library_config_path(config_id):
    return VPN_LIBRARY_DIR / f"{config_id}.conf"


def add_vpn_config(label, provider, fields):
    if provider not in VPN_PROVIDERS:
        return False, f"unknown provider: {provider}", None
    label = (label or "").strip()
    if not label:
        return False, 'A name for this config is required (e.g. "Tokyo").', None
    if len(label) > 40:
        return False, "Name is too long (max 40 characters).", None

    protocol = VPN_PROVIDERS[provider]["protocol"]
    raw = (fields.get("config") or "").strip()
    if protocol == "wireguard":
        ok, message = _validate_wg_config(raw)
        if not ok:
            return False, message, None
        content = _inject_wg_routing(raw)
    elif protocol == "openvpn":
        ok, message = _validate_ovpn_config(raw)
        if not ok:
            return False, message, None
        content = _inject_ovpn_routing(raw)
    else:
        return False, f"unsupported protocol: {protocol}", None

    config_id = uuid.uuid4().hex[:8]
    VPN_LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    path = _library_config_path(config_id)
    path.write_text(content)
    path.chmod(0o600)

    library = _load_vpn_library()
    library["configs"].append({"id": config_id, "label": label, "provider": provider, "protocol": protocol})
    _save_vpn_library(library)
    return True, None, config_id


def delete_vpn_config(config_id):
    library = _load_vpn_library()
    if not any(c["id"] == config_id for c in library["configs"]):
        return False, "config not found"

    if library.get("active") == config_id:
        disconnect_vpn()
        library["active"] = None

    library["configs"] = [c for c in library["configs"] if c["id"] != config_id]
    _save_vpn_library(library)

    path = _library_config_path(config_id)
    if path.exists():
        path.unlink()
    return True, None


# (protocol, systemd unit, live config path) for each supported backend --
# connect/disconnect/status all key off of this rather than duplicating the
# protocol dispatch in each function.
_VPN_BACKENDS = {
    "wireguard": (f"wg-quick@{VPN_IF}", WG_CONF_PATH),
    "openvpn": (f"openvpn-client@{OVPN_CONF_NAME}", OVPN_CONF_PATH),
}


def activate_vpn_config(config_id):
    """Makes a saved config the live tunnel: stops whatever's currently
    running (if anything, regardless of protocol), copies the selected
    config into the right live path for its own protocol, and reconnects.
    Only one config -- of either protocol -- can ever be live at a time,
    since both share the same VPN_TABLE/VPN_FWMARK policy-routing setup."""
    library = _load_vpn_library()
    entry = next((c for c in library["configs"] if c["id"] == config_id), None)
    if not entry:
        return False, "config not found"

    src = _library_config_path(config_id)
    if not src.exists():
        return False, "saved config file is missing"

    protocol = entry.get("protocol", "wireguard")  # older library entries predate multi-protocol support
    if protocol not in _VPN_BACKENDS:
        return False, f"unsupported protocol: {protocol}"

    ok, message = disconnect_vpn()
    if not ok:
        return False, message or "failed to stop the current tunnel before switching"

    _, dest_path = _VPN_BACKENDS[protocol]
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_text(src.read_text())
    dest_path.chmod(0o600)

    library["active"] = config_id
    _save_vpn_library(library)

    return connect_vpn()


def _active_vpn_protocol():
    library = _load_vpn_library()
    entry = next((c for c in library["configs"] if c["id"] == library.get("active")), None)
    return entry.get("protocol", "wireguard") if entry else None


def connect_vpn():
    protocol = _active_vpn_protocol()
    if protocol not in _VPN_BACKENDS:
        return False, "no VPN configuration saved yet"
    unit, conf_path = _VPN_BACKENDS[protocol]
    if not conf_path.exists():
        return False, "no VPN configuration saved yet"
    _, err, rc = run_capture(["systemctl", "enable", "--now", unit], timeout=20)
    if rc != 0:
        return False, err or "failed to start VPN tunnel"
    return True, None


def disconnect_vpn():
    """Stops whichever backend is actually running, regardless of what the
    library's "active" bookkeeping says -- more robust than trusting that
    to always be in sync with real systemd state."""
    for unit, _ in _VPN_BACKENDS.values():
        if systemctl_is_active(unit) == "active":
            _, err, rc = run_capture(["systemctl", "disable", "--now", unit], timeout=20)
            if rc != 0:
                return False, err or f"failed to stop {unit}"
    return True, None
    return True, None


def _load_vpn_mode_state():
    try:
        return json.loads(VPN_MODE_STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {"mode": "off", "macs": []}


def _save_vpn_mode_state(mode, macs):
    VPN_MODE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    VPN_MODE_STATE_PATH.write_text(json.dumps({"mode": mode, "macs": macs}))


def apply_vpn_mode(runtime, mode, macs):
    """mode: "off" (no marking), "all" (every AP client), or "selected"
    (only the given MACs, resolved to their current DHCP-leased IP -- MACs
    rather than IPs so a stale lease doesn't silently keep routing the wrong
    address once it's been reassigned)."""
    if mode not in ("off", "all", "selected"):
        return False, "mode must be 'off', 'all', or 'selected'"
    ap_if = runtime.get("AP_WIFI_IF", "")
    if not ap_if:
        return False, "AP interface not detected"

    run(["nft", "flush", "chain", "ip", "vpn", "vpn_mark"])
    run(["nft", "flush", "set", "ip", "vpn", "vpn_clients"])

    if mode == "all":
        _, err, rc = run_capture(
            ["nft", "add", "rule", "ip", "vpn", "vpn_mark", "iifname", ap_if, "meta", "mark", "set", str(VPN_FWMARK)]
        )
        if rc != 0:
            return False, err or "failed to apply nftables rule"
    elif mode == "selected":
        leases = _parse_leases()
        ips = sorted({leases[m]["ip"] for m in {mac.lower() for mac in macs} if m in leases and leases[m].get("ip")})
        if ips:
            _, err, rc = run_capture(
                ["nft", "add", "element", "ip", "vpn", "vpn_clients", "{ " + ", ".join(ips) + " }"]
            )
            if rc != 0:
                return False, err or "failed to update client set"
            _, err, rc = run_capture(
                [
                    "nft", "add", "rule", "ip", "vpn", "vpn_mark",
                    "iifname", ap_if, "ip", "saddr", "@vpn_clients", "meta", "mark", "set", str(VPN_FWMARK),
                ]
            )
            if rc != 0:
                return False, err or "failed to apply nftables rule"

    _save_vpn_mode_state(mode, macs if mode == "selected" else [])
    return True, None


def _wg_status(status):
    out, rc = run(["wg", "show", VPN_IF, "dump"])
    if rc == 0:
        lines = out.splitlines()
        if len(lines) >= 2:
            # peer line: pubkey psk endpoint allowed-ips latest-hs rx tx keepalive
            peer = lines[1].split("\t")
            if len(peer) >= 7:
                status["endpoint"] = peer[2] if peer[2] != "(none)" else None
                status["latest_handshake"] = int(peer[4]) if peer[4].isdigit() and peer[4] != "0" else None
                status["rx_bytes"] = int(peer[5]) if peer[5].isdigit() else None
                status["tx_bytes"] = int(peer[6]) if peer[6].isdigit() else None
    status["tunnel_ip"] = _iface_ipv4(VPN_IF)


def _ovpn_status(status):
    # No wg-show equivalent for a quick external query -- endpoint/handshake/
    # rx/tx aren't surfaced for OpenVPN (would need parsing its own
    # periodic --status log, not set up here). The tun interface actually
    # existing with an assigned IP is a better "really connected" signal
    # than the systemd unit's own state anyway: the process can be running
    # (systemd "active") while still mid-handshake or retrying, well before
    # the interface comes up.
    status["tunnel_ip"] = _iface_ipv4(OVPN_IF)
    if status["tunnel_ip"] is None:
        status["connected"] = False


def get_vpn_status():
    library = _load_vpn_library()
    active_id = library.get("active")
    active_entry = next((c for c in library["configs"] if c["id"] == active_id), None)
    protocol = active_entry.get("protocol", "wireguard") if active_entry else None

    configured = active_entry is not None and protocol in _VPN_BACKENDS and _VPN_BACKENDS[protocol][1].exists()
    unit_state = systemctl_is_active(_VPN_BACKENDS[protocol][0]) if configured else "inactive"
    connected = unit_state == "active"

    status = {
        "provider": active_entry["provider"] if active_entry else None,
        "protocol": protocol,
        "label": active_entry["label"] if active_entry else None,
        "configured": configured,
        "connected": connected,
        "unit_state": unit_state,
        "configs": library["configs"],
        "active_config_id": active_id,
        "endpoint": None,
        "tunnel_ip": None,
        "latest_handshake": None,
        "rx_bytes": None,
        "tx_bytes": None,
    }

    if connected and protocol == "wireguard":
        _wg_status(status)
    elif connected and protocol == "openvpn":
        _ovpn_status(status)

    mode_state = _load_vpn_mode_state()
    status["mode"] = mode_state.get("mode", "off")
    status["selected_clients"] = mode_state.get("macs", [])
    return status


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
    # Cheap no-op unless a USB wifi adapter was plugged/unplugged or the
    # dashboard's selection changed since the last poll -- see
    # maybe_reapply_uplink_selection's docstring.
    maybe_reapply_uplink_selection(runtime)
    return jsonify(
        {
            "ap": get_ap_status(conf, runtime),
            "uplink": get_uplink_status(runtime),
            "uplink_wifi_adapters": get_uplink_wifi_adapters(runtime),
            "wifi_connect": get_wifi_connect_state(),
            "wan_ip": get_wan_ip(),
            "clients": get_clients(runtime),
            "sessions": get_login_sessions(),
            "services": get_services_status(),
            "speedtest": get_speedtest_result(),
            "clock": get_clock_status(),
            "vpn": get_vpn_status(),
            "ssh_port": conf.get("SSH_PORT"),
            "rdp_port": conf.get("RDP_PORT"),
        }
    )


@app.route("/api/vpn/providers")
def api_vpn_providers():
    return jsonify(get_vpn_providers())


@app.route("/api/vpn/configs", methods=["POST"])
def api_vpn_configs_add():
    payload = request.get_json(silent=True) or {}
    label = payload.get("label") or ""
    provider = payload.get("provider") or ""
    fields = payload.get("fields") or {}

    ok, message, config_id = add_vpn_config(label, provider, fields)
    if not ok:
        return jsonify({"status": "error", "message": message}), 400
    return jsonify({"status": "ok", "id": config_id, "vpn": get_vpn_status()})


@app.route("/api/vpn/configs/<config_id>", methods=["DELETE"])
def api_vpn_configs_delete(config_id):
    ok, message = delete_vpn_config(config_id)
    if not ok:
        return jsonify({"status": "error", "message": message}), 400
    return jsonify({"status": "ok", "vpn": get_vpn_status()})


@app.route("/api/vpn/configs/<config_id>/activate", methods=["POST"])
def api_vpn_configs_activate(config_id):
    ok, message = activate_vpn_config(config_id)
    if not ok:
        return jsonify({"status": "error", "message": message}), 400
    return jsonify({"status": "ok", "vpn": get_vpn_status()})


@app.route("/api/vpn/connect", methods=["POST"])
def api_vpn_connect():
    ok, message = connect_vpn()
    if not ok:
        return jsonify({"status": "error", "message": message}), 400
    return jsonify({"status": "ok", "vpn": get_vpn_status()})


@app.route("/api/vpn/disconnect", methods=["POST"])
def api_vpn_disconnect():
    ok, message = disconnect_vpn()
    if not ok:
        return jsonify({"status": "error", "message": message}), 400
    return jsonify({"status": "ok", "vpn": get_vpn_status()})


@app.route("/api/vpn/mode", methods=["POST"])
def api_vpn_mode():
    payload = request.get_json(silent=True) or {}
    mode = payload.get("mode") or ""
    macs = payload.get("macs") or []

    _, runtime = load_config()
    ok, message = apply_vpn_mode(runtime, mode, macs)
    if not ok:
        return jsonify({"status": "error", "message": message}), 400
    return jsonify({"status": "ok", "vpn": get_vpn_status()})


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
    iface = get_active_uplink_wifi_if(runtime)
    return jsonify(get_wifi_networks(iface))


@app.route("/api/wifi/connect", methods=["POST"])
def api_wifi_connect():
    payload = request.get_json(silent=True) or {}
    ssid = (payload.get("ssid") or "").strip()
    password = payload.get("password") or ""

    if not ssid:
        return jsonify({"status": "error", "message": "network name is required"}), 400

    _, runtime = load_config()
    iface = get_active_uplink_wifi_if(runtime)
    started = start_wifi_connect(iface, ssid, password)
    if not started:
        return jsonify({"status": "already_connecting"}), 409
    return jsonify({"status": "started"})


@app.route("/api/uplink/wifi_adapters")
def api_uplink_wifi_adapters():
    _, runtime = load_config()
    return jsonify(get_uplink_wifi_adapters(runtime))


@app.route("/api/uplink/wifi_adapter/select", methods=["POST"])
def api_uplink_wifi_adapter_select():
    payload = request.get_json(silent=True) or {}
    iface = payload.get("interface") or None

    _, runtime = load_config()
    ok, message = select_uplink_wifi_adapter(runtime, iface)
    if not ok:
        return jsonify({"status": "error", "message": message}), 400
    return jsonify({"status": "ok", "uplink_wifi_adapters": get_uplink_wifi_adapters(runtime)})


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


@app.route("/api/system/power", methods=["POST"])
def api_system_power():
    payload = request.get_json(silent=True) or {}
    action = payload.get("action")
    if action not in _POWER_ACTIONS:
        return jsonify({"status": "error", "message": "action must be 'reboot' or 'shutdown'"}), 400
    if not payload.get("confirm"):
        return jsonify({"status": "error", "message": "confirmation required"}), 400

    trigger_power_action(action)
    return jsonify({"status": "scheduled", "action": action, "delay_seconds": _POWER_DELAY_SECONDS})


def main():
    conf, runtime = load_config()
    port = int(conf.get("DASHBOARD_PORT", "8080"))
    ap_ip = conf.get("AP_IP", "172.24.1.1")

    # nftables.service re-renders /etc/nftables.conf from scratch on every
    # boot (the `vpn` table starts empty -- see nftables.conf.tmpl), so
    # whatever traffic mode was last chosen has to be re-applied here rather
    # than assumed to persist on its own.
    mode_state = _load_vpn_mode_state()
    if mode_state.get("mode", "off") != "off":
        apply_vpn_mode(runtime, mode_state.get("mode", "off"), mode_state.get("macs", []))

    # Likewise the nftables `uplinks` set starts with just ETH_IF/VPN_IF on
    # every boot -- reconcile it against whatever USB wifi adapter(s) are
    # actually plugged in right now before the dashboard starts serving,
    # rather than waiting for the first /api/status poll to notice.
    global _last_uplink_signature
    candidates, active = apply_uplink_selection(runtime)
    _last_uplink_signature = (tuple(candidates), active)

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
