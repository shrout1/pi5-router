# Future improvements

Ideas that aren't built yet, but worth doing next. Not a commitment, just a
place to park design thinking so it isn't lost between sessions.

## VPN services pane — done

Built: WireGuard tunnels managed from the dashboard, plus a traffic-mode
selector — off, all AP clients, or a hand-picked subset — implemented as
`fwmark`-based policy routing (`nftables` `ip vpn` table marks opted-in
clients' forward traffic; a dedicated `ip rule`/routing table, installed by
wg-quick's own `PostUp`/`PreDown`, sends marked traffic out the tunnel).
Provider is pluggable (`VPN_PROVIDERS` in `dashboard/app.py`); ProtonVPN is
the first one, via a pasted WireGuard config — no ProtonVPN account
credentials touch the Pi at all.

The VPN card supports multiple *named* saved configs (e.g. "Tokyo",
"Amsterdam") rather than one anonymous one: each is stored under
`/etc/wireguard/pi5-router-vpn-library/<id>.conf` (0600), with only the
currently-active one copied into the fixed `/etc/wireguard/wg-pi5.conf`
wg-quick actually runs (the interface name is fixed for nftables/policy-
routing to target, so only one config can be live at a time; "activating" a
different one stops the tunnel, swaps the file, reconnects). Pasting a
config gets real structural validation (specific errors for a missing
section/key/malformed key, not one generic failure) plus a short guided
step list with a direct link to ProtonVPN's config-download page.

Settled, for the record: WireGuard over OpenVPN (simpler to script,
kernel-native); split tunnel over full tunnel (per-client selection was the
actual ask); `DNS =` is stripped from every saved config, not just
Table/PostUp/PreDown — wg-quick only shells out to `resolvconf` (not
installed) when a config has one, and letting a split-tunnel's DNS become
the box's system-wide resolver would be the same class of mistake covered
below.

**ProtonVPN's official Linux app was evaluated as an alternative and
rejected** — worth recording so it isn't re-investigated from scratch.
Installed `proton-vpn-cli` (their real, GPG-signed apt repo) and connected
live, twice. Findings:
- It manages the tunnel via NetworkManager and implements its kill switch
  by pointing dummy interfaces as the *system-wide* default route/DNS for
  both IPv4 and IPv6 — confirmed directly in NetworkManager's log. This
  broke this box's own connectivity both times, requiring physical
  intervention to fix, even with the kill switch explicitly set to `off`
  (the IPv6 leak-protection interface isn't governed by that setting).
- Its built-in split tunneling is keyed by Linux uid/app path or
  destination IP range — not by which downstream AP client sent the
  traffic — so it can't do what this pane needs regardless of the above.
- The underlying library (`python3-proton-vpn-api-core` etc.) has no
  code path that emits a portable WireGuard config text file; the
  WireGuard backend builds an in-memory NetworkManager object graph
  directly. Using it for auth-and-generate-our-own-config would mean
  hand-assembling a `.conf` from internal fields never designed to be read
  that way, on a less-tested code path than their full CLI.

Left open rather than solved:

- **DNS doesn't follow the VPN mark.** dnsmasq proxies each client's DNS
  query as its own new outbound request, so a VPN-routed client's lookups
  still go out the normal uplink via `UPSTREAM_DNS` — there's no client
  identity left on that request for anything to mark. Fixing this would mean
  either policy-routing dnsmasq's own upstream queries by uid, or running a
  second resolver bound to the tunnel, neither of which is built.
- **No automatic fallback on tunnel failure.** If the tunnel drops, marked
  traffic blackholes (no route in its policy table) rather than falling
  back to the direct uplink, until it reconnects or the mode is turned off.
  WireGuard's own keepalive/reconnect behavior means this is usually
  self-healing, but it hasn't been tested against a real multi-minute
  outage.
- The original double-NAT WebRTC motivation for this pane (STUN breaking
  when the hotel's own uplink is itself behind NAT) hasn't actually been
  re-tested against a real video call yet now that the pane exists — worth
  confirming it fixes what it was built for.
- **Bring-your-own WireGuard key.** Generating the keypair locally (`wg
  genkey`) and only handing ProtonVPN the public key would be a genuine
  security upgrade (private key never leaves the Pi) and a much shorter
  paste step, but depends on whether ProtonVPN's config-download page
  actually supports supplying your own public key -- not yet confirmed.

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
