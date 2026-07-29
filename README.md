# pi5-router

Turns a Raspberry Pi into a hotel-room travel router:

- Uplink to the hotel network via wired Ethernet when available, falling back
  automatically to a USB wifi adapter (tested with the Alfa AWUS036AXML) when
  it's not. NetworkManager's route metrics handle the failover — Ethernet
  just needs a lower metric than the wifi uplink, which is the NetworkManager
  default.
- NATed access point on the Pi's onboard wifi radio for devices in the room.
- SSH and RDP (xrdp) reachable only from the AP-side subnet, never from the
  hotel-facing interfaces.
- Silent on the hotel-facing side: no response to port scans or pings, no
  mDNS/Bonjour broadcasts, no RPC portmapper exposure.

Built for Debian 13 (trixie) on a Raspberry Pi 5, using NetworkManager +
nftables. Requires:
- one wired Ethernet interface (NetworkManager-managed)
- one onboard wifi radio (becomes the AP)
- one USB wifi adapter (becomes the wifi uplink)

## Usage

```sh
cp router.conf.example router.conf
$EDITOR router.conf        # set your SSID/passphrase, subnet, DNS, etc.
sudo ./install.sh
```

Re-running `install.sh` is safe — it's idempotent, and only changes files it
owns or a small set of well-known package-config lines it edits in place
(each of those gets a one-time `*.pi5-router.orig` backup on first touch).

`router.conf` is gitignored since it holds the wifi passphrase — copy it from
`router.conf.example` locally rather than committing real credentials.

## After running

The Pi's own uplink still has to clear the hotel's captive portal itself
(e.g. via a browser on the Pi, if it has a display attached, or a
text-mode workaround over SSH/console) before the room network gets real
internet access — a device connected to the AP can't do this on the Pi's
behalf, since most hotel portals gate per-MAC/per-session on the uplink
connection, not on downstream NATed clients.

## Verification checklist

1. `nft list ruleset` — ruleset matches `templates/nftables.conf.tmpl`,
   `nftables.service` enabled.
2. From a device connected to the AP SSID: gets a lease in the configured
   DHCP range, can reach `ssh`/RDP on the AP IP, has internet once the
   uplink is past the captive portal.
3. From the hotel-side network: `nmap -Pn <pi-hotel-ip>` shows nothing open,
   `ping` gets no reply, `avahi-browse -a` from another device on that
   segment doesn't see the Pi.
4. `ip addr show`, `ip route` — no IPv6 global addresses on the uplink
   interfaces; default route flips between Ethernet and wifi automatically
   as you plug/unplug Ethernet.
5. SSH/RDP connection attempts from the hotel-side network fail
   (refused/timeout); attempts from the AP subnet succeed.
6. `sudo ./install.sh` a second time completes cleanly with no duplicated
   NetworkManager connections or firewall rules.

## Changing SSID/subnet for a different trip

Just edit `router.conf` and re-run `sudo ./install.sh` — nothing else to
touch.
