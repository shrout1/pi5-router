# Future improvements

Ideas that aren't built yet, but worth doing next. Not a commitment, just a
place to park design thinking so it isn't lost between sessions.

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

## Captive portal forwarding to AP clients

Right now, per the README's "After running" section, only the Pi's own
uplink can clear the hotel captive portal — someone has to RDP into the Pi
and drive the login in Firefox/Chromium there, because most portals gate
per-MAC/per-session on the uplink interface itself, not on downstream NATed
clients. Need to validate whether a connected AP client (someone's own
phone/laptop on `Armadillo`) can instead see and complete that same portal
login directly, so people don't have to RDP in just to click through a
hotel wifi agreement.

Not yet confirmed how much of this already works vs. needs building:

- The `nftables` forward chain (`templates/nftables.conf.tmpl`) already
  allows `wlan0 → {eth0, wlan1}` unconditionally, so walled-garden traffic
  from an AP client to the portal's servers may already pass through as-is
  — worth testing against a real hotel portal before assuming anything
  needs to change here.
- The likely gap is redirection, not forwarding: browsers on the AP side
  have no reason to hit the portal's captive-detection endpoint
  unprompted, so a client might just see "no internet" rather than being
  bounced to the login page the way a directly-connected device would.
  May need a dnsmasq-level redirect for the usual OS captive-portal probe
  URLs (`connectivitycheck.gstatic.com`, `captive.apple.com`, etc.) while
  the uplink itself is pre-auth.
- Some portals key the login session to the connecting device's own
  MAC/IP as seen by the hotel's gateway — through NAT, that's always the
  Pi's uplink identity regardless of which AP client's browser is doing
  the clicking. Need to confirm login-through-NAT actually authenticates
  the Pi's uplink session (which is the goal) and doesn't just fail or
  authenticate something unusable.
- If this doesn't fully work, worth documenting exactly where it breaks
  (no redirect vs. portal rejects NAT'd sessions vs. something else) rather
  than just noting today's RDP-based workaround.
