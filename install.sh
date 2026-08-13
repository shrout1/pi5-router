#!/usr/bin/env bash
#
# pi5-router installer.
#
# Turns this box into a hotel travel router:
#   - uplink out whichever of {wired ethernet, USB wifi} is up (NetworkManager
#     already prefers ethernet via route metric, untouched by this script)
#   - NATed access point on the onboard wifi radio for room devices
#   - SSH/RDP reachable only from the AP side
#   - silent (no scan responses, no mDNS chatter) on the hotel-facing side
#
# Safe to re-run: every step either checks before creating, or overwrites a
# file this script owns outright.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log() { echo "==> $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "must be run as root (sudo ./install.sh)"

# ---------------------------------------------------------------------------
# 1. Config
# ---------------------------------------------------------------------------
if [[ -f "$SCRIPT_DIR/router.conf" ]]; then
    log "loading router.conf"
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/router.conf"
else
    log "router.conf not found, using router.conf.example defaults"
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/router.conf.example"
fi

for v in AP_SSID AP_BAND AP_CHANNEL AP_SUBNET_PREFIX AP_IP \
         DHCP_RANGE_START DHCP_RANGE_END UPSTREAM_DNS SSH_PORT RDP_PORT \
         DASHBOARD_PORT; do
    [[ -n "${!v:-}" ]] || die "config variable $v is not set"
done

CONN_NAME="${AP_SSID}-AP"

# The desktop/RDP setup below targets the human user who owns this box, not
# root. sudo sets SUDO_USER; fall back to logname for the (unusual) case of a
# direct root login.
TARGET_USER="${SUDO_USER:-$(logname)}"
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
[[ -n "$TARGET_HOME" && -d "$TARGET_HOME" ]] || die "could not resolve home directory for $TARGET_USER"

# ---------------------------------------------------------------------------
# 1b. AP_PASSPHRASE -- a credential, handled separately from the vars above:
# generate/prompt rather than hard-fail, and persist it so re-runs don't ask
# again. router.conf.example ships this blank on purpose (no baked-in
# default credential).
# ---------------------------------------------------------------------------
if [[ -z "${AP_PASSPHRASE:-}" ]]; then
    if [[ -t 0 ]]; then
        for _ in 1 2 3; do
            read -rsp "AP_PASSPHRASE not set. Enter a wifi passphrase (8-63 chars), or press Enter to auto-generate one: " AP_PASSPHRASE
            echo
            [[ -z "$AP_PASSPHRASE" ]] && break
            (( ${#AP_PASSPHRASE} >= 8 && ${#AP_PASSPHRASE} <= 63 )) && break
            echo "Passphrase must be 8-63 characters (WPA2 requirement); try again or leave blank to auto-generate." >&2
            AP_PASSPHRASE=""
        done
    fi
    if [[ -z "$AP_PASSPHRASE" ]]; then
        AP_PASSPHRASE="$(openssl rand -hex 10)"
        log "generated a random AP_PASSPHRASE (written to router.conf)"
    fi
fi

CONF_FILE="$SCRIPT_DIR/router.conf"
[[ -f "$CONF_FILE" ]] || cp "$SCRIPT_DIR/router.conf.example" "$CONF_FILE"
if grep -q '^AP_PASSPHRASE=' "$CONF_FILE"; then
    sed -i "s/^AP_PASSPHRASE=.*/AP_PASSPHRASE=\"${AP_PASSPHRASE}\"/" "$CONF_FILE"
else
    echo "AP_PASSPHRASE=\"${AP_PASSPHRASE}\"" >> "$CONF_FILE"
fi
chown "$TARGET_USER":"$TARGET_USER" "$CONF_FILE"
chmod 0600 "$CONF_FILE"

# ---------------------------------------------------------------------------
# 2. Detect interfaces
# ---------------------------------------------------------------------------
log "detecting network interfaces"

ETH_IF="$(nmcli -t -f DEVICE,TYPE device status | awk -F: '$2=="ethernet"{print $1; exit}')"

# Only the AP radio is detected here. USB wifi uplink adapters are not: any
# number can be present, absent, or hot-swapped, and which one (if any) is
# actively used is a live, dashboard-managed choice (see
# dashboard/app.py:list_uplink_wifi_candidates / apply_uplink_selection), not
# an install-time fact worth freezing into a config file. The same
# USB-vs-onboard sysfs-path heuristic is used there; keep the two in sync if
# this changes.
AP_WIFI_IF=""
while IFS=: read -r dev type; do
    [[ "$type" == "wifi" ]] || continue
    devpath="$(readlink -f "/sys/class/net/$dev/device" 2>/dev/null || true)"
    if [[ "$devpath" != *"/usb"* ]]; then
        AP_WIFI_IF="$dev"
        break
    fi
done < <(nmcli -t -f DEVICE,TYPE device status)

[[ -n "$ETH_IF" ]]     || die "no ethernet interface detected"
[[ -n "$AP_WIFI_IF" ]] || die "no onboard wifi interface detected (expected the AP radio)"

log "  ethernet uplink : $ETH_IF"
log "  AP radio        : $AP_WIFI_IF"
log "  USB wifi uplink : chosen live from the dashboard's adapter picker -- none required to be present now"

# Fixed name, not detected -- this interface only exists once a VPN config is
# saved through the dashboard (see dashboard/app.py). Referencing it by name
# in nftables below is safe even before it exists; oifname/iifname match by
# string, not by live interface index.
VPN_IF="wg-pi5"

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
render() {
    # render <template> <outfile> KEY=value [KEY=value ...]
    local template="$1" outfile="$2"
    shift 2
    local content
    content="$(cat "$template")"
    local kv key val
    for kv in "$@"; do
        key="${kv%%=*}"
        val="${kv#*=}"
        content="${content//__${key}__/$val}"
    done
    local tmp
    tmp="$(mktemp)"
    printf '%s\n' "$content" > "$tmp"
    install -m 0644 "$tmp" "$outfile"
    rm -f "$tmp"
}

backup_once() {
    local f="$1"
    [[ -f "${f}.pi5-router.orig" ]] || cp -a "$f" "${f}.pi5-router.orig"
}

dns_server_lines() {
    local d line=""
    for d in $UPSTREAM_DNS; do
        line+="server=${d}"$'\n'
    done
    printf '%s' "${line%$'\n'}"
}

# ---------------------------------------------------------------------------
# 3. Packages
# ---------------------------------------------------------------------------
log "installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y dnsmasq nftables hostapd xfce4 lightdm xrdp network-manager-gnome \
    python3-flask speedtest-cli librespeed-cli wireguard wireguard-tools openvpn

# ---------------------------------------------------------------------------
# 4. AP interface — hostapd
# ---------------------------------------------------------------------------
# NetworkManager's own AP mode is implemented via wpa_supplicant, which only
# speaks 802.11n (HT) — it has no VHT/802.11ac support at all, capping
# throughput regardless of band/channel. hostapd's nl80211 driver supports
# full VHT80, so $AP_WIFI_IF is handed to hostapd instead: NetworkManager is
# told to ignore that interface entirely (it keeps managing $ETH_IF and any
# USB wifi uplink adapters exactly as before), and a small oneshot service
# assigns its static IP since NM no longer will.
log "handing $AP_WIFI_IF to hostapd (NetworkManager will ignore it)"

nmcli connection delete "$CONN_NAME" >/dev/null 2>&1 || true

mkdir -p /etc/NetworkManager/conf.d
cat > /etc/NetworkManager/conf.d/99-pi5-router-unmanaged.conf <<EOF
[keyfile]
unmanaged-devices=interface-name:${AP_WIFI_IF}
EOF
nmcli general reload >/dev/null

cat > /etc/systemd/system/pi5-router-ap-ip.service <<EOF
[Unit]
Description=Static IP for pi5-router AP interface (${AP_WIFI_IF})
After=sys-subsystem-net-devices-${AP_WIFI_IF}.device
BindsTo=sys-subsystem-net-devices-${AP_WIFI_IF}.device
Before=hostapd.service dnsmasq.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/ip link set ${AP_WIFI_IF} up
ExecStart=/usr/sbin/ip addr replace ${AP_IP}/${AP_SUBNET_PREFIX} dev ${AP_WIFI_IF}

[Install]
WantedBy=multi-user.target
EOF

# VHT80 needs a channel that's part of a clean 4-channel 80MHz block; map the
# supported non-DFS blocks, otherwise fall back to HT40/HT20 only.
VHT_WIDTH=0
VHT_CENTER=0
case "$AP_CHANNEL" in
    36|40|44|48)      VHT_WIDTH=1; VHT_CENTER=42  ;;
    149|153|157|161)  VHT_WIDTH=1; VHT_CENTER=155 ;;
esac

{
    echo "interface=${AP_WIFI_IF}"
    echo "driver=nl80211"
    echo "country_code=US"
    echo "ssid=${AP_SSID}"
    echo "wpa=2"
    echo "wpa_key_mgmt=WPA-PSK"
    echo "wpa_passphrase=${AP_PASSPHRASE}"
    echo "rsn_pairwise=CCMP"
    echo "auth_algs=1"
    echo "macaddr_acl=0"
    echo "ignore_broadcast_ssid=0"
    echo "wmm_enabled=1"
    echo "channel=${AP_CHANNEL}"
    echo "ieee80211n=1"
    if [[ "$AP_BAND" == "a" ]]; then
        echo "hw_mode=a"
        echo "ieee80211ac=1"
        echo "ht_capab=[HT40+][SHORT-GI-20][SHORT-GI-40]"
        if [[ "$VHT_WIDTH" == 1 ]]; then
            echo "vht_capab=[SHORT-GI-80]"
            echo "vht_oper_chwidth=${VHT_WIDTH}"
            echo "vht_oper_centr_freq_seg0_idx=${VHT_CENTER}"
        fi
    else
        echo "hw_mode=g"
        echo "ht_capab=[SHORT-GI-20]"
    fi
} > /etc/hostapd/hostapd.conf
chmod 0600 /etc/hostapd/hostapd.conf

systemctl daemon-reload
systemctl enable pi5-router-ap-ip.service >/dev/null
systemctl restart pi5-router-ap-ip.service
systemctl unmask hostapd >/dev/null 2>&1 || true
systemctl enable hostapd >/dev/null
systemctl restart hostapd

# ---------------------------------------------------------------------------
# 5. dnsmasq
# ---------------------------------------------------------------------------
log "configuring dnsmasq"
render "$SCRIPT_DIR/templates/dnsmasq.conf.tmpl" /etc/dnsmasq.d/10-hotel-ap.conf \
    "AP_WIFI_IF=$AP_WIFI_IF" \
    "AP_IP=$AP_IP" \
    "DNS_SERVER_LINES=$(dns_server_lines)" \
    "DHCP_RANGE_START=$DHCP_RANGE_START" \
    "DHCP_RANGE_END=$DHCP_RANGE_END"
systemctl enable dnsmasq >/dev/null
systemctl restart dnsmasq

# ---------------------------------------------------------------------------
# 6. sysctl / IPv6
# ---------------------------------------------------------------------------
log "configuring sysctl (ip_forward, disable IPv6 on router interfaces)"
render "$SCRIPT_DIR/templates/sysctl.conf.tmpl" /etc/sysctl.d/99-hotel-router.conf \
    "ETH_IF=$ETH_IF" \
    "AP_WIFI_IF=$AP_WIFI_IF"
sysctl --system >/dev/null

# ---------------------------------------------------------------------------
# 7. nftables
# ---------------------------------------------------------------------------
log "configuring nftables"
render "$SCRIPT_DIR/templates/nftables.conf.tmpl" /etc/nftables.conf \
    "AP_WIFI_IF=$AP_WIFI_IF" \
    "ETH_IF=$ETH_IF" \
    "VPN_IF=$VPN_IF" \
    "SSH_PORT=$SSH_PORT" \
    "RDP_PORT=$RDP_PORT" \
    "DASHBOARD_PORT=$DASHBOARD_PORT"
systemctl enable nftables >/dev/null
systemctl restart nftables

# ---------------------------------------------------------------------------
# 7b. VPN (WireGuard) scaffolding
# ---------------------------------------------------------------------------
# Only the directory and package are set up here -- no tunnel exists until a
# provider config is saved through the dashboard's VPN card. wireguard-tools
# ships the wg-quick@.service systemd template used to bring it up/down, so
# nothing project-specific is needed there either.
log "preparing WireGuard directory"
install -d -m 0700 /etc/wireguard

# ---------------------------------------------------------------------------
# 7c. VPN (OpenVPN) scaffolding
# ---------------------------------------------------------------------------
# Same posture as the WireGuard scaffolding above: only the plumbing goes
# here, no tunnel exists until a config is saved through the dashboard. The
# up/down hook scripts are static (not per-config, unlike WireGuard's
# injected Table=/PostUp/PreDown) since OpenVPN client configs don't have an
# equivalent single-directive way to land routes in a dedicated table --
# see the scripts themselves for why this is safe.
log "preparing OpenVPN client directory"
install -d -m 0700 /etc/openvpn/client
install -d -m 0755 /opt/pi5-router/openvpn-hooks
install -m 0755 "$SCRIPT_DIR/templates/openvpn-client-up.sh.tmpl" /opt/pi5-router/openvpn-hooks/up.sh
install -m 0755 "$SCRIPT_DIR/templates/openvpn-client-down.sh.tmpl" /opt/pi5-router/openvpn-hooks/down.sh

# ---------------------------------------------------------------------------
# 8. SSH
# ---------------------------------------------------------------------------
log "restricting sshd to $AP_IP"
render "$SCRIPT_DIR/templates/sshd.conf.tmpl" /etc/ssh/sshd_config.d/99-hotel-router.conf \
    "AP_IP=$AP_IP" \
    "SSH_PORT=$SSH_PORT"
systemctl enable ssh >/dev/null
systemctl restart ssh

# ---------------------------------------------------------------------------
# 9. xrdp
# ---------------------------------------------------------------------------
# xrdp 0.10.x does not bind to a plain "address=" key — a bare "port=3389"
# means listen on all interfaces regardless of "address". The bind address
# has to be encoded into the port directive itself: port=tcp://<ip>:<port>.
log "restricting xrdp to $AP_IP:$RDP_PORT"
backup_once /etc/xrdp/xrdp.ini
sed -i -E '/^address=/d' /etc/xrdp/xrdp.ini
if grep -qE '^port=' /etc/xrdp/xrdp.ini; then
    sed -i -E "s#^port=.*#port=tcp://${AP_IP}:${RDP_PORT}#" /etc/xrdp/xrdp.ini
else
    sed -i "/^\[Globals\]/a port=tcp://${AP_IP}:${RDP_PORT}" /etc/xrdp/xrdp.ini
fi
systemctl enable xrdp xrdp-sesman >/dev/null
systemctl restart xrdp xrdp-sesman

# xrdp reads/writes its own TLS cert; needs the login user in ssl-cert.
usermod -aG ssl-cert "$TARGET_USER"

# ~/.xsession must exist, be executable, and force GDK_BACKEND=x11. Without
# the exec bit, Debian's /etc/X11/Xsession falls through to whatever session
# picker is system-default (Raspberry Pi Desktop's Wayfire/labwc, not XFCE),
# producing sessions that crash or hang. Even with that fixed, XFCE's GTK3
# apps (xfce4-panel, xfdesktop) will silently connect to labwc's Wayland
# socket at /run/user/<uid>/wayland-0 instead of this X11 session whenever
# a local monitor session is also active on the same box (this is a hotel
# router with a physical-monitor use case, not just headless) — GDK tries
# the Wayland backend before X11 by default, and libwayland-client falls back
# to connecting to "wayland-0" even when $WAYLAND_DISPLAY isn't set. Result:
# an RDP session that looks alive (all processes running, no crash) but
# renders solid black, while a rogue second desktop briefly disrupts the
# physical monitor's wallpaper. Forcing GDK_BACKEND=x11 is what actually
# fixes this — everything else tried along the way (xorg.conf GPU options,
# resetting xfconf) was not the root cause.
log "writing $TARGET_HOME/.xsession (forces GDK_BACKEND=x11)"
install -m 0755 -o "$TARGET_USER" -g "$TARGET_USER" \
    "$SCRIPT_DIR/templates/xsession.tmpl" "$TARGET_HOME/.xsession"

# The xorgxrdp package ships an xorg.conf written for x86 (DRMAllowList
# "i915 radeon", which doesn't exist on this SoC's vc4/v3d GPU, plus an
# explicit DRMDevice/DRI3 pointed at the real GPU render node). Deploy a
# corrected version: glamor stays loaded (xorgxrdp's driver and input
# modules have a hard link-time dependency on glamor symbols and fail to
# load at all without it — this isn't optional acceleration), but nothing
# in this file targets a real DRM device, and ProbeAllGpus is turned off.
log "correcting /etc/X11/xrdp/xorg.conf for this hardware"
backup_once /etc/X11/xrdp/xorg.conf
install -m 0644 "$SCRIPT_DIR/templates/xrdp-xorg.conf.tmpl" /etc/X11/xrdp/xorg.conf

# Firefox's default-on DNS-over-HTTPS bypasses the local resolver, which
# breaks captive-portal detection (the portal's redirect never fires, and
# what does load looks like an unrelated TLS/cert failure). This is a
# system-wide policy, not a per-profile pref, so it applies before the user
# ever opens Settings.
log "disabling Firefox DNS-over-HTTPS (breaks captive-portal login otherwise)"
mkdir -p /etc/firefox/policies
install -m 0644 "$SCRIPT_DIR/templates/firefox-policies.json.tmpl" /etc/firefox/policies/policies.json

# ---------------------------------------------------------------------------
# 10. avahi — restrict to the AP interface only
# ---------------------------------------------------------------------------
log "restricting avahi to $AP_WIFI_IF"
backup_once /etc/avahi/avahi-daemon.conf
if grep -qE '^allow-interfaces=' /etc/avahi/avahi-daemon.conf; then
    sed -i -E "s/^allow-interfaces=.*/allow-interfaces=${AP_WIFI_IF}/" /etc/avahi/avahi-daemon.conf
elif grep -qE '^#allow-interfaces=' /etc/avahi/avahi-daemon.conf; then
    sed -i -E "s/^#allow-interfaces=.*/allow-interfaces=${AP_WIFI_IF}/" /etc/avahi/avahi-daemon.conf
else
    sed -i "/^\[server\]/a allow-interfaces=${AP_WIFI_IF}" /etc/avahi/avahi-daemon.conf
fi
systemctl restart avahi-daemon

# ---------------------------------------------------------------------------
# 11. rpcbind — restrict to loopback + AP subnet only
# ---------------------------------------------------------------------------
# rpcbind is systemd socket-activated on this system: rpcbind.socket binds
# 0.0.0.0:111/[::]:111 directly in the unit file, before rpcbind's own -h
# flags (set via /etc/default/rpcbind) ever get a chance to matter. Both
# have to be addressed: the OPTIONS file for non-socket-activated fallback,
# and a socket unit override for the actual listening addresses.
log "restricting rpcbind to loopback + $AP_IP"
backup_once /etc/default/rpcbind
RPCBIND_OPTS="-w -h 127.0.0.1 -h ::1 -h ${AP_IP}"
if grep -qE '^OPTIONS=' /etc/default/rpcbind; then
    sed -i -E "s|^OPTIONS=.*|OPTIONS=\"${RPCBIND_OPTS}\"|" /etc/default/rpcbind
else
    echo "OPTIONS=\"${RPCBIND_OPTS}\"" >> /etc/default/rpcbind
fi

mkdir -p /etc/systemd/system/rpcbind.socket.d
cat > /etc/systemd/system/rpcbind.socket.d/pi5-router.conf <<EOF
[Socket]
ListenStream=
ListenDatagram=
ListenStream=127.0.0.1:111
ListenDatagram=127.0.0.1:111
ListenStream=[::1]:111
ListenDatagram=[::1]:111
ListenStream=${AP_IP}:111
ListenDatagram=${AP_IP}:111
EOF

systemctl daemon-reload
systemctl restart rpcbind.socket
systemctl restart rpcbind.service

# ---------------------------------------------------------------------------
# 12. Status dashboard
# ---------------------------------------------------------------------------
log "installing status dashboard"

mkdir -p /etc/pi5-router
cat > /etc/pi5-router/runtime.env <<EOF
ETH_IF=$ETH_IF
AP_WIFI_IF=$AP_WIFI_IF
EOF
chmod 0644 /etc/pi5-router/runtime.env

install -m 0600 "$SCRIPT_DIR/router.conf" /etc/pi5-router/router.conf 2>/dev/null || \
    install -m 0600 "$SCRIPT_DIR/router.conf.example" /etc/pi5-router/router.conf

mkdir -p /opt/pi5-router
rm -rf /opt/pi5-router/dashboard
cp -a "$SCRIPT_DIR/dashboard" /opt/pi5-router/dashboard
chown -R root:root /opt/pi5-router/dashboard

install -m 0644 "$SCRIPT_DIR/templates/pi5-router-dashboard.service.tmpl" \
    /etc/systemd/system/pi5-router-dashboard.service

render "$SCRIPT_DIR/templates/pi5-router-speedtest.dispatcher.tmpl" \
    /etc/NetworkManager/dispatcher.d/90-pi5-router-speedtest \
    "ETH_IF=$ETH_IF" \
    "AP_WIFI_IF=$AP_WIFI_IF"
chmod 0755 /etc/NetworkManager/dispatcher.d/90-pi5-router-speedtest

systemctl daemon-reload
systemctl enable pi5-router-dashboard >/dev/null
systemctl restart pi5-router-dashboard

# ---------------------------------------------------------------------------
# 13. Summary
# ---------------------------------------------------------------------------
cat <<EOF

==================================================================
pi5-router setup complete.

  Ethernet uplink : $ETH_IF
  USB wifi uplink : choose from the dashboard's adapter picker (VPN/Wifi Uplink card)
  AP radio        : $AP_WIFI_IF
  AP SSID         : $AP_SSID
  AP address      : ${AP_IP}/${AP_SUBNET_PREFIX}
  SSH / RDP       : reachable only from ${AP_IP}/${AP_SUBNET_PREFIX}
  Dashboard       : http://127.0.0.1:${DASHBOARD_PORT} or http://${AP_IP}:${DASHBOARD_PORT}
                    (reachable only from ${AP_IP}/${AP_SUBNET_PREFIX} and loopback)

If the hotel network requires a captive-portal login, that has to be
completed by the Pi's own uplink (${ETH_IF}, or whichever USB wifi adapter
is chosen from the dashboard) before room devices get real internet access
— devices behind the AP cannot complete it on the Pi's behalf. RDP into
${AP_IP}:${RDP_PORT} for a full XFCE desktop (NetworkManager applet in the
panel tray, Firefox and Chromium both installed) to pick the uplink SSID
and drive the portal login yourself.

Two gotchas that look unrelated to networking but aren't:
  - No RTC battery means the clock resets to 1970 on power loss and
    stays wrong until NTP syncs — which needs the uplink to already be
    online, a chicken-and-egg problem the captive portal makes worse.
    A wrong clock breaks OCSP/certificate validation on essentially
    every HTTPS site, including the portal's own login page, and shows
    up as unrelated-looking "secure connection failed" errors. If nmcli
    can already reach the AP's gateway (or NTP happens to sneak through
    the captive portal's walled garden) this resolves itself; otherwise
    set the date manually first with \`sudo date -s "YYYY-MM-DD HH:MM:SS"\`
    (local time) before troubleshooting anything else HTTPS-related.
  - Firefox's DNS-over-HTTPS is disabled by this installer's policy
    (see templates/firefox-policies.json.tmpl) because it bypasses the
    local resolver and breaks captive-portal redirect detection
    entirely — a symptom that looks like "nothing loads" rather than
    an obvious DNS error. Chromium has no equivalent policy here; if
    portal detection silently fails there too, check
    chrome://settings/security → Use secure DNS.
==================================================================
EOF
