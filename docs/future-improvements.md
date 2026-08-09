# Future improvements

Ideas that aren't built yet, but worth doing next. Not a commitment, just a
place to park design thinking so it isn't lost between sessions.

## Power options pane

A dashboard panel with **Shutdown** and **Restart** buttons for the Pi
itself. Right now the only way to power-cycle it is a physical unplug or
RDP/SSH in and run `sudo systemctl poweroff` / `reboot` by hand — annoying
for something that's meant to be a plug-and-forget travel appliance,
especially before packing it up to leave a room.

Implementation notes, following the existing `dashboard/app.py` pattern
(each panel is a `get_X_status()` feeding `/api/status`, plus a POST action
endpoint):

- `pi5-router-dashboard.service` already runs as root (see `install.sh`
  step 12), so `systemctl poweroff` / `systemctl reboot` need no additional
  sudo/polkit wiring — same trust boundary as the AP-credential and clock
  endpoints already in `app.py`.
- Needs a confirmation step in the UI — this is the one dashboard action
  that's meaningfully destructive (kills the AP for every connected device
  mid-session), unlike the existing panels which are all read-only or
  easily reversible.
- Worth a short grace-period/toast ("Shutting down in 5s…") so a misclick is
  recoverable, and so it's obvious the button did something before the Pi
  actually goes dark.
- Restart should probably warn if there are active connected clients
  (`get_clients()` already has this data) rather than silently dropping
  everyone.

## VPN services pane

A panel to manage an OpenVPN (or WireGuard) tunnel back to a home
network/VPS, with connect/disconnect and status (connected, tunnel IP,
uptime).

This is the fix for the double-NAT WebRTC problem noted in past sessions:
when the hotel's own upstream is itself behind NAT (this Pi's NAT + the
hotel's NAT stacked), STUN-based media negotiation for video calls
(Slack, etc.) frequently breaks — connects, no audio/video. A VPN tunnel
back to a network with a real public IP (or at least only one NAT layer)
sidesteps that. A phone hotspot works as a manual workaround today; this
would make it automatic.

Nothing has been built for this yet — no client config, no routing changes.
Open questions to settle before implementing:

- **Split tunnel vs. full tunnel.** Routing all AP-side traffic through the
  VPN fixes WebRTC but adds latency to everything and defeats using the
  hotel's local speed for e.g. large downloads. A per-device or
  per-application split tunnel is more useful but more complex to wire
  through `nftables`/routing tables than a blanket route.
- **Client config storage.** `router.conf` already holds one credential
  (`AP_PASSPHRASE`) with the 0600-permissions/gitignore treatment; a VPN
  config would need the same care, probably as a separate gitignored file
  rather than cramming a multi-line `.ovpn`/WireGuard key into
  `router.conf`'s flat `KEY=value` format.
- **OpenVPN vs. WireGuard.** WireGuard is simpler to script status/up/down
  for a dashboard toggle and has lower overhead; OpenVPN is what was
  originally discussed. Worth deciding before writing any code rather than
  building against one and switching later.
- **Failure behavior.** If the tunnel drops mid-call, should traffic
  silently fall back to the direct uplink (breaks the fix, but keeps
  connectivity) or hold and retry (correct for the WebRTC use case, but
  looks like an outage for everything else)? Probably needs to be a
  per-tunnel policy choice surfaced in the pane, not hardcoded.
