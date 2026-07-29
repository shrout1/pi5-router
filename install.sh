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

for v in AP_SSID AP_PASSPHRASE AP_BAND AP_SUBNET_PREFIX AP_IP \
         DHCP_RANGE_START DHCP_RANGE_END UPSTREAM_DNS SSH_PORT RDP_PORT; do
    [[ -n "${!v:-}" ]] || die "config variable $v is not set"
done

CONN_NAME="${AP_SSID}-AP"

# ---------------------------------------------------------------------------
# 2. Detect interfaces
# ---------------------------------------------------------------------------
log "detecting network interfaces"

ETH_IF="$(nmcli -t -f DEVICE,TYPE device status | awk -F: '$2=="ethernet"{print $1; exit}')"

USB_WIFI_IF=""
AP_WIFI_IF=""
while IFS=: read -r dev type; do
    [[ "$type" == "wifi" ]] || continue
    devpath="$(readlink -f "/sys/class/net/$dev/device" 2>/dev/null || true)"
    if [[ "$devpath" == *"/usb"* ]]; then
        USB_WIFI_IF="$dev"
    else
        AP_WIFI_IF="$dev"
    fi
done < <(nmcli -t -f DEVICE,TYPE device status)

[[ -n "$ETH_IF" ]]      || die "no ethernet interface detected"
[[ -n "$USB_WIFI_IF" ]] || die "no USB wifi interface detected (expected the Alfa uplink)"
[[ -n "$AP_WIFI_IF" ]]  || die "no onboard wifi interface detected (expected the AP radio)"
[[ "$USB_WIFI_IF" != "$AP_WIFI_IF" ]] || die "USB and onboard wifi resolved to the same interface"

log "  ethernet uplink : $ETH_IF"
log "  USB wifi uplink : $USB_WIFI_IF (Alfa)"
log "  AP radio        : $AP_WIFI_IF"

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
apt-get install -y dnsmasq nftables xfce4 lightdm xrdp

# ---------------------------------------------------------------------------
# 4. AP connection (NetworkManager)
# ---------------------------------------------------------------------------
log "configuring AP connection '$CONN_NAME' on $AP_WIFI_IF"
if ! nmcli -t -f NAME connection show | grep -Fxq "$CONN_NAME"; then
    nmcli connection add type wifi ifname "$AP_WIFI_IF" con-name "$CONN_NAME" autoconnect yes ssid "$AP_SSID"
fi
nmcli connection modify "$CONN_NAME" \
    802-11-wireless.mode ap \
    802-11-wireless.band "$AP_BAND" \
    ipv4.method manual \
    ipv4.addresses "${AP_IP}/${AP_SUBNET_PREFIX}" \
    ipv4.never-default yes \
    ipv4.dns "" \
    ipv6.method disabled \
    wifi-sec.key-mgmt wpa-psk \
    wifi-sec.proto rsn \
    wifi-sec.pairwise ccmp \
    wifi-sec.group ccmp \
    wifi-sec.psk "$AP_PASSPHRASE"
nmcli connection up "$CONN_NAME"

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
    "USB_WIFI_IF=$USB_WIFI_IF" \
    "AP_WIFI_IF=$AP_WIFI_IF"
sysctl --system >/dev/null

# ---------------------------------------------------------------------------
# 7. nftables
# ---------------------------------------------------------------------------
log "configuring nftables"
render "$SCRIPT_DIR/templates/nftables.conf.tmpl" /etc/nftables.conf \
    "AP_WIFI_IF=$AP_WIFI_IF" \
    "ETH_IF=$ETH_IF" \
    "USB_WIFI_IF=$USB_WIFI_IF" \
    "SSH_PORT=$SSH_PORT" \
    "RDP_PORT=$RDP_PORT"
systemctl enable nftables >/dev/null
systemctl restart nftables

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
log "restricting xrdp to $AP_IP:$RDP_PORT"
backup_once /etc/xrdp/xrdp.ini
if grep -qE '^address=' /etc/xrdp/xrdp.ini; then
    sed -i -E "s/^address=.*/address=${AP_IP}/" /etc/xrdp/xrdp.ini
else
    sed -i "/^\[Globals\]/a address=${AP_IP}" /etc/xrdp/xrdp.ini
fi
if grep -qE '^port=' /etc/xrdp/xrdp.ini; then
    sed -i -E "s/^port=.*/port=${RDP_PORT}/" /etc/xrdp/xrdp.ini
else
    sed -i "/^\[Globals\]/a port=${RDP_PORT}" /etc/xrdp/xrdp.ini
fi
systemctl enable xrdp xrdp-sesman >/dev/null
systemctl restart xrdp xrdp-sesman

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
log "restricting rpcbind to loopback + $AP_IP"
backup_once /etc/default/rpcbind
RPCBIND_OPTS="-w -h 127.0.0.1 -h ::1 -h ${AP_IP}"
if grep -qE '^OPTIONS=' /etc/default/rpcbind; then
    sed -i -E "s|^OPTIONS=.*|OPTIONS=\"${RPCBIND_OPTS}\"|" /etc/default/rpcbind
else
    echo "OPTIONS=\"${RPCBIND_OPTS}\"" >> /etc/default/rpcbind
fi
systemctl restart rpcbind

# ---------------------------------------------------------------------------
# 12. Summary
# ---------------------------------------------------------------------------
cat <<EOF

==================================================================
pi5-router setup complete.

  Ethernet uplink : $ETH_IF
  USB wifi uplink : $USB_WIFI_IF (Alfa)
  AP radio        : $AP_WIFI_IF
  AP SSID         : $AP_SSID
  AP address      : ${AP_IP}/${AP_SUBNET_PREFIX}
  SSH / RDP       : reachable only from ${AP_IP}/${AP_SUBNET_PREFIX}

If the hotel network requires a captive-portal login, that has to be
completed by the Pi's own uplink (eth0/wlan1) — e.g. via a browser on
the Pi if a display is attached — before room devices get real
internet access. Devices behind the AP cannot complete it on the
Pi's behalf.
==================================================================
EOF
