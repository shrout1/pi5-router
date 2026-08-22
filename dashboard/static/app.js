const POLL_MS = 5000;

function fmtDuration(seconds) {
  if (seconds == null) return "—";
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  if (m === 0) return `${s}s`;
  const h = Math.floor(m / 60);
  if (h === 0) return `${m}m ${s}s`;
  return `${h}h ${m % 60}m`;
}

function statusClass(value) {
  if (value === "active" || value === "up") return "up";
  if (value === "inactive" || value === "failed" || value === "dead") return "down";
  return "unknown";
}

function setText(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text ?? "—";
}

let apSsidFieldInitialized = false;

function renderAp(ap) {
  setText("ap-ssid", ap.ssid);
  setText("ap-band-channel", `${ap.band === "a" ? "5GHz" : "2.4GHz"} · ch ${ap.channel}`);
  setText("ap-ip", ap.ip);
  setText("ap-client-count", String(ap.client_count));
  const hostapd = document.getElementById("ap-hostapd");
  hostapd.textContent = ap.hostapd;
  hostapd.className = `status ${statusClass(ap.hostapd)}`;

  // Prefill the SSID field once so it's editable-in-place; never touch the
  // password field (the API never returns AP_PASSPHRASE, so there's
  // nothing to prefill it with anyway -- blank means "keep current").
  if (!apSsidFieldInitialized && ap.ssid) {
    document.getElementById("ap-ssid-input").value = ap.ssid;
    // Reflect the actually-running band, not just the radio's HTML default
    // (5GHz) -- only on this first sync, same as the SSID field, so it
    // doesn't fight with whatever the user has clicked since.
    const bandInput = document.querySelector(`input[name="ap-band"][value="${ap.band}"]`);
    if (bandInput) bandInput.checked = true;
    apSsidFieldInitialized = true;
  }
}

async function updateAp() {
  const btn = document.getElementById("ap-update");
  const message = document.getElementById("ap-message");
  const ssid = document.getElementById("ap-ssid-input").value.trim();
  const passphrase = document.getElementById("ap-passphrase-input").value;
  const bandInput = document.querySelector('input[name="ap-band"]:checked');
  const band = bandInput ? bandInput.value : null;

  if (!ssid) {
    message.textContent = "SSID is required.";
    message.className = "message error";
    return;
  }

  btn.disabled = true;
  message.textContent = "Updating… the AP will restart and connected devices will briefly disconnect.";
  message.className = "message";
  try {
    const res = await fetch("/api/ap/update", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ssid, passphrase, band }),
    });
    const data = await res.json();
    if (res.ok && data.status === "ok") {
      message.textContent = "AP updated.";
      message.className = "message ok";
      document.getElementById("ap-passphrase-input").value = "";
    } else {
      message.textContent = data.message || "Failed to update AP.";
      message.className = "message error";
    }
  } catch (err) {
    // If you're connected via the AP itself, restarting hostapd can drop
    // this very request -- doesn't mean it failed. poll() below shows the
    // real state once reconnected.
    message.textContent = "Connection interrupted (expected if you're on this AP) -- check status above.";
    message.className = "message error";
  } finally {
    btn.disabled = false;
    poll();
  }
}

document.getElementById("ap-update").addEventListener("click", updateAp);

function renderUplink(uplink, wanIp) {
  setText("uplink-active", uplink.active ? `${uplink.active} (${uplink.active_interface})` : "none");
  setText("uplink-network", uplink.network_name);
  setText("wan-ip", wanIp || "unavailable");

  for (const [key, statusId, ipId, gatewayId] of [
    ["ethernet", "uplink-eth", "uplink-eth-ip", "uplink-eth-gateway"],
    ["wifi", "uplink-wifi", "uplink-wifi-ip", "uplink-wifi-gateway"],
  ]) {
    const statusEl = document.getElementById(statusId);
    const info = uplink.interfaces[key];
    if (!info) {
      statusEl.textContent = "not present";
      statusEl.className = "status unknown";
      setText(ipId, "—");
      setText(gatewayId, "—");
      continue;
    }
    statusEl.textContent = info.active ? `${info.state} (active)` : info.state;
    statusEl.className = `status ${statusClass(info.state)}`;
    setText(ipId, key === "wifi" && info.ssid ? `${info.ip || "—"} (${info.ssid})` : info.ip);
    setText(gatewayId, info.gateway);
  }
}

function renderSpeedtest(speedtest) {
  const btn = document.getElementById("speedtest-run");
  btn.disabled = speedtest.status === "running";
  btn.textContent = speedtest.status === "running" ? "Running…" : "Run now";

  if (speedtest.status === "running") {
    setText("speed-provider", "running…");
    setText("speed-down", "running…");
    setText("speed-up", "running…");
    setText("speed-ping", "running…");
    setText("speed-server", "—");
    setText("speed-server-ip", "—");
  } else if (speedtest.status === "ok") {
    setText("speed-provider", speedtest.provider_label);
    setText("speed-down", speedtest.download_mbps != null ? `${speedtest.download_mbps} Mbps` : "—");
    setText("speed-up", speedtest.upload_mbps != null ? `${speedtest.upload_mbps} Mbps` : "—");
    setText("speed-ping", speedtest.ping_ms != null ? `${speedtest.ping_ms} ms` : "—");
    const serverBits = [speedtest.server_name, speedtest.sponsor].filter(Boolean).join(" — ");
    setText("speed-server", serverBits || speedtest.server_location || "—");
    setText(
      "speed-server-ip",
      speedtest.server_ip ? `${speedtest.server_ip}${speedtest.server_host ? ` (${speedtest.server_host})` : ""}` : "—"
    );
    setText("speed-time", new Date(speedtest.timestamp * 1000).toLocaleString());
  } else if (speedtest.status === "failed") {
    setText("speed-provider", speedtest.provider_label || "—");
    setText("speed-down", "failed");
    setText("speed-up", "—");
    setText("speed-ping", "—");
    setText("speed-server", speedtest.message || "—");
    setText("speed-server-ip", "—");
    setText("speed-time", speedtest.timestamp ? new Date(speedtest.timestamp * 1000).toLocaleString() : "—");
  } else {
    setText("speed-provider", "—");
    setText("speed-down", "not yet run");
    setText("speed-up", "—");
    setText("speed-ping", "—");
    setText("speed-server", "—");
    setText("speed-server-ip", "—");
    setText("speed-time", "—");
  }
}

function renderServices(services) {
  const list = document.getElementById("services-list");
  list.innerHTML = "";
  for (const [unit, state] of Object.entries(services)) {
    const dt = document.createElement("dt");
    dt.textContent = unit;
    const dd = document.createElement("dd");
    dd.textContent = state;
    dd.className = `status ${statusClass(state)}`;
    list.appendChild(dt);
    list.appendChild(dd);
  }
}

let lastClientCount = 0;

function renderClients(clients) {
  lastClientCount = clients.length;
  const body = document.getElementById("clients-body");
  body.innerHTML = "";
  if (!clients.length) {
    body.innerHTML = '<tr><td colspan="4" class="empty">No clients connected</td></tr>';
    return;
  }
  for (const c of clients) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${c.hostname || "—"}</td>
      <td>${c.ip || "—"}</td>
      <td>${c.mac}</td>
      <td>${fmtDuration(c.connected_seconds)}</td>
    `;
    body.appendChild(tr);
  }
}

const CONNECTION_LABELS = { ssh: "SSH", rdp: "RDP", local: "Local" };

function connectionLabel(conn) {
  return CONNECTION_LABELS[conn] || conn;
}

function connectionBadgeClass(conn) {
  if (conn === "ssh") return "badge-ssh";
  if (conn === "rdp") return "badge-rdp";
  if (conn === "local") return "badge-local";
  return "badge-unknown";
}

function renderSessions(sessions) {
  const body = document.getElementById("sessions-body");
  body.innerHTML = "";
  if (!sessions.length) {
    body.innerHTML = '<tr><td colspan="5" class="empty">No active sessions</td></tr>';
    return;
  }
  for (const s of sessions) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${s.user}</td>
      <td><span class="badge ${connectionBadgeClass(s.connection)}">${connectionLabel(s.connection)}</span></td>
      <td>${s.source || "—"}</td>
      <td>${s.tty || "—"}</td>
      <td>${s.since || "—"}</td>
    `;
    body.appendChild(tr);
  }
}

let wifiAdapterActive = null;
let wifiAdapterSwitching = false;

function renderWifiAdapters(info) {
  const select = document.getElementById("wifi-adapter-select");
  const scanBtn = document.getElementById("wifi-scan");
  wifiAdapterActive = info.active;

  // Don't stomp on the dropdown while a switch this tab just requested is
  // still in flight -- the next poll after it lands will reflect it anyway.
  if (wifiAdapterSwitching) return;

  if (!info.candidates.length) {
    select.innerHTML = '<option value="">No USB wifi adapter detected</option>';
    select.disabled = true;
    scanBtn.disabled = true;
    return;
  }

  scanBtn.disabled = false;
  select.disabled = false;
  const options = info.candidates.map((c) => {
    const activeTag = c.interface === info.active ? (info.auto ? " (active, auto)" : " (active)") : "";
    return `<option value="${c.interface}">${c.label}${activeTag}</option>`;
  });
  select.innerHTML = options.join("");
  select.value = info.active || info.candidates[0].interface;
}

async function selectWifiAdapter() {
  const select = document.getElementById("wifi-adapter-select");
  const message = document.getElementById("wifi-adapter-message");
  const iface = select.value;
  if (iface === wifiAdapterActive) return;

  wifiAdapterSwitching = true;
  message.textContent = `Switching to ${iface}…`;
  message.className = "message";
  try {
    const res = await fetch("/api/uplink/wifi_adapter/select", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ interface: iface }),
    });
    const data = await res.json();
    if (res.ok && data.status === "ok") {
      message.textContent = `Now using ${iface} as the wifi uplink.`;
      message.className = "message ok";
    } else {
      message.textContent = data.message || "Failed to switch adapter.";
      message.className = "message error";
    }
  } catch (err) {
    message.textContent = "Request failed.";
    message.className = "message error";
  } finally {
    wifiAdapterSwitching = false;
    poll();
  }
}

document.getElementById("wifi-adapter-select").addEventListener("change", selectWifiAdapter);

let wifiNetworks = [];
let selectedSsid = null;

function renderWifiNetworks(networks) {
  wifiNetworks = networks;
  const list = document.getElementById("wifi-network-list");
  list.innerHTML = "";
  if (!networks.length) {
    list.innerHTML = '<li class="empty">No networks found</li>';
    return;
  }
  for (const net of networks) {
    const li = document.createElement("li");
    li.className = net.ssid === selectedSsid ? "selected" : "";
    li.innerHTML = `
      <span>${net.ssid}${net.in_use ? " (connected)" : ""}</span>
      <span class="wifi-meta">${net.secured ? "🔒 " : ""}${net.signal}%</span>
    `;
    li.addEventListener("click", () => selectWifiNetwork(net));
    list.appendChild(li);
  }
}

function selectWifiNetwork(net) {
  selectedSsid = net.ssid;
  document.getElementById("wifi-selected-ssid").textContent = net.ssid;
  document.getElementById("wifi-password-label").hidden = !net.secured;
  document.getElementById("wifi-password").value = "";
  document.getElementById("wifi-connect-form").hidden = false;
  document.getElementById("wifi-connect").hidden = false;
  document.getElementById("wifi-message").textContent = "";
  renderWifiNetworks(wifiNetworks);
}

async function scanWifi() {
  const btn = document.getElementById("wifi-scan");
  const list = document.getElementById("wifi-network-list");
  btn.disabled = true;
  btn.textContent = "Scanning…";
  list.innerHTML = '<li class="empty">Scanning…</li>';
  try {
    const res = await fetch("/api/wifi/scan");
    const networks = await res.json();
    renderWifiNetworks(networks);
  } catch (err) {
    list.innerHTML = '<li class="empty">Scan failed</li>';
  } finally {
    btn.disabled = false;
    btn.textContent = "Scan for networks";
  }
}

// Scan+associate+DHCP for a hotel guest network can take 15-20+ seconds.
// A synchronous fetch that blocks the whole time risks the browser aborting
// the stalled request before the server's response arrives -- even though
// the backend finishes successfully underneath it. So /api/wifi/connect
// returns immediately (just starts a background thread server-side) and
// the real outcome is read back out of the regular status poll instead.
function renderWifiConnectState(state) {
  const btn = document.getElementById("wifi-connect");
  const message = document.getElementById("wifi-message");
  if (!state || state.status === "idle") return;

  if (state.status === "connecting") {
    btn.disabled = true;
    message.textContent = `Connecting to ${state.ssid}… this can take 15-20s on some networks.`;
    message.className = "message";
  } else if (state.status === "ok") {
    btn.disabled = false;
    message.textContent = `Connected to ${state.ssid}.`;
    message.className = "message ok";
  } else if (state.status === "error") {
    btn.disabled = false;
    message.textContent = state.message || `Failed to connect to ${state.ssid}.`;
    message.className = "message error";
  }
}

async function connectWifi() {
  if (!selectedSsid) return;
  const btn = document.getElementById("wifi-connect");
  const message = document.getElementById("wifi-message");
  const password = document.getElementById("wifi-password").value;

  btn.disabled = true;
  message.textContent = `Connecting to ${selectedSsid}… this can take 15-20s on some networks.`;
  message.className = "message";
  try {
    await fetch("/api/wifi/connect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ssid: selectedSsid, password }),
    });
  } catch (err) {
    // Doesn't matter -- the connect runs server-side regardless of whether
    // this particular request round-trips cleanly; poll() below reads the
    // real state back out.
  }
  poll();
}

document.getElementById("wifi-scan").addEventListener("click", scanWifi);
document.getElementById("wifi-connect").addEventListener("click", connectWifi);

let clockFieldsInitialized = false;
let timezoneOptionsLoaded = false;
let pendingTimezone = null;
let currentFormTimezone = null;

function applyPendingTimezone() {
  if (timezoneOptionsLoaded && pendingTimezone) {
    document.getElementById("clock-tz").value = pendingTimezone;
    currentFormTimezone = pendingTimezone;
    pendingTimezone = null;
  }
}

// Interpret dateStr/timeStr as wall-clock time in `timeZone` and return the
// UTC instant (ms) it corresponds to. Intl has no direct "zoned time -> UTC"
// call, so this uses the standard trick: read the fields back as if they
// were UTC, see what that instant actually looks like when formatted in
// `timeZone`, and use the difference as the zone's offset at that moment
// (this is what makes it DST-correct instead of a fixed offset).
function zonedTimeToUtcMs(dateStr, timeStr, timeZone) {
  const baseline = new Date(`${dateStr}T${timeStr}Z`).getTime();
  const dtf = new Intl.DateTimeFormat("en-US", {
    timeZone,
    hour12: false,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
  const parts = Object.fromEntries(dtf.formatToParts(baseline).map((p) => [p.type, p.value]));
  const hour = parts.hour === "24" ? 0 : Number(parts.hour);
  const asIfLocalMs = Date.UTC(
    Number(parts.year),
    Number(parts.month) - 1,
    Number(parts.day),
    hour,
    Number(parts.minute),
    Number(parts.second)
  );
  return baseline - (asIfLocalMs - baseline);
}

// Inverse direction: a UTC instant -> the wall-clock date/time fields for it
// in `timeZone`.
function utcMsToZonedFields(utcMs, timeZone) {
  const dtf = new Intl.DateTimeFormat("en-US", {
    timeZone,
    hour12: false,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
  const parts = Object.fromEntries(dtf.formatToParts(utcMs).map((p) => [p.type, p.value]));
  const hour = parts.hour === "24" ? "00" : parts.hour;
  return {
    date: `${parts.year}-${parts.month}-${parts.day}`,
    time: `${hour}:${parts.minute}:${parts.second}`,
  };
}

function handleTimezoneChange() {
  const dateInput = document.getElementById("clock-date");
  const timeInput = document.getElementById("clock-time");
  const newTz = document.getElementById("clock-tz").value;

  if (currentFormTimezone && dateInput.value && timeInput.value) {
    const utcMs = zonedTimeToUtcMs(dateInput.value, timeInput.value, currentFormTimezone);
    const fields = utcMsToZonedFields(utcMs, newTz);
    dateInput.value = fields.date;
    timeInput.value = fields.time;
  }
  currentFormTimezone = newTz;
}

document.getElementById("clock-tz").addEventListener("change", handleTimezoneChange);

// Ticks the displayed clock every second between polls, rather than only
// updating in 5s jumps -- resynced against the server's actual value on
// every poll (renderClock below) so it can't drift or go stale.
let clockBaseServerMs = null;
let clockBaseLocalMs = null;
let clockTimezone = null;

function updateClockDisplay() {
  if (clockBaseServerMs == null) return;
  const estimated = new Date(clockBaseServerMs + (Date.now() - clockBaseLocalMs));
  // Explicitly in the Pi's own configured timezone -- this panel is about
  // the Pi's system clock, not whatever timezone the viewing browser is in.
  const opts = clockTimezone ? { timeZone: clockTimezone } : undefined;
  setText("clock-now", estimated.toLocaleString(undefined, opts));
}

setInterval(updateClockDisplay, 1000);

function renderClock(clock) {
  clockBaseServerMs = new Date(clock.now).getTime();
  clockBaseLocalMs = Date.now();
  clockTimezone = clock.timezone || null;
  updateClockDisplay();

  const ntpToggle = document.getElementById("clock-ntp-toggle");
  ntpToggle.checked = clock.ntp_enabled;
  ntpToggle.title = clock.ntp_enabled ? "Enabled" : "Disabled";

  const ntp = document.getElementById("clock-ntp");
  if (!clock.ntp_enabled) {
    ntp.textContent = "Disabled";
    ntp.className = "status unknown";
  } else {
    ntp.textContent = clock.ntp_synchronized ? "synchronized" : "not synchronized";
    ntp.className = `status ${clock.ntp_synchronized ? "up" : "unknown"}`;
  }

  // Only prefill the form once, on first load -- otherwise a poll landing
  // mid-edit would stomp on whatever the user is currently typing.
  if (clockFieldsInitialized) return;
  clockFieldsInitialized = true;

  const now = new Date(clock.now);
  const pad = (n) => String(n).padStart(2, "0");
  document.getElementById("clock-date").value =
    `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
  document.getElementById("clock-time").value =
    `${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`;
  if (clock.timezone) {
    // The timezone <select>'s options load separately (see loadTimezones)
    // and may not have arrived yet -- stash the value and apply it once
    // both are ready, in whichever order they actually resolve.
    pendingTimezone = clock.timezone;
    applyPendingTimezone();
  }
}

async function loadTimezones() {
  try {
    const res = await fetch("/api/timezones");
    if (!res.ok) return;
    const zones = await res.json();
    const select = document.getElementById("clock-tz");
    select.innerHTML = zones
      .map((z) => `<option value="${z.name}">${z.name}${z.offset ? ` (${z.offset})` : ""}</option>`)
      .join("");
    timezoneOptionsLoaded = true;
    applyPendingTimezone();
  } catch (err) {
    // non-fatal -- Set Clock still works, just without a timezone change
  }
}

async function applyClock() {
  const btn = document.getElementById("clock-apply");
  const message = document.getElementById("clock-message");
  const date = document.getElementById("clock-date").value;
  const time = document.getElementById("clock-time").value;
  const timezone = document.getElementById("clock-tz").value.trim();

  if (!date || !time) {
    message.textContent = "Date and time are required.";
    message.className = "message error";
    return;
  }

  btn.disabled = true;
  message.textContent = "";
  message.className = "message";
  try {
    const res = await fetch("/api/clock/set", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        date,
        time: time.length === 5 ? `${time}:00` : time,
        timezone: timezone || null,
      }),
    });
    const data = await res.json();
    if (res.ok && data.status === "ok") {
      message.textContent = "Clock updated.";
      message.className = "message ok";
      renderClock(data.clock);
    } else {
      message.textContent = data.message || "Failed to update clock.";
      message.className = "message error";
    }
  } catch (err) {
    message.textContent = "Request failed.";
    message.className = "message error";
  } finally {
    btn.disabled = false;
  }
}

document.getElementById("clock-apply").addEventListener("click", applyClock);
loadTimezones();

// Live control, independent of the Set Clock button below -- flipping this
// off lets a manually-set time actually stick (see set_clock() server-side:
// it now only re-enables NTP afterward if it was already on), flipping it
// back on resumes normal auto-sync.
async function toggleNtp() {
  const toggle = document.getElementById("clock-ntp-toggle");
  const message = document.getElementById("clock-message");
  const desired = toggle.checked;

  toggle.disabled = true;
  message.textContent = "";
  message.className = "message";
  try {
    const res = await fetch("/api/clock/ntp", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: desired }),
    });
    const data = await res.json();
    if (res.ok && data.status === "ok") {
      renderClock(data.clock);
    } else {
      toggle.checked = !desired;
      message.textContent = data.message || "Failed to change NTP state.";
      message.className = "message error";
    }
  } catch (err) {
    toggle.checked = !desired;
    message.textContent = "Request failed.";
    message.className = "message error";
  } finally {
    toggle.disabled = false;
  }
}

document.getElementById("clock-ntp-toggle").addEventListener("change", toggleNtp);

// Purely client-side -- the browser already knows its own clock and
// timezone (Intl.DateTimeFormat's resolvedOptions), no server round-trip
// needed. Compared against the Pi's own extrapolated time (same estimate
// updateClockDisplay ticks every second) so a skew shows up directly,
// rather than making the user eyeball two absolute timestamps against
// each other -- this is the actual diagnostic signal for a stale/wrong
// system clock silently breaking a VPN handshake (WireGuard's replay
// protection rejects a handshake timestamped earlier than the last one
// it saw from that peer).
function getClientTime() {
  const now = new Date();
  const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
  let text = `${now.toLocaleString()} (${tz})`;

  if (clockBaseServerMs != null) {
    const piNowMs = clockBaseServerMs + (Date.now() - clockBaseLocalMs);
    const skewMs = now.getTime() - piNowMs;
    const skewSec = Math.round(Math.abs(skewMs) / 1000);
    const direction = skewMs >= 0 ? "ahead of" : "behind";
    let skewText;
    if (skewSec < 1) {
      skewText = "in sync with the Pi";
    } else if (skewSec < 60) {
      skewText = `${skewSec}s ${direction} the Pi`;
    } else if (skewSec < 3600) {
      skewText = `${Math.round(skewSec / 60)}m ${direction} the Pi`;
    } else {
      skewText = `${(skewSec / 3600).toFixed(1)}h ${direction} the Pi`;
    }
    text += ` — ${skewText}`;
  }

  setText("clock-client-time", text);

  // Also prefill the Set Clock form with this same reading, so the button
  // doubles as "sync the Pi to my device" -- one click here, one click on
  // Set Clock (turning off the NTP checkbox first if it's on, otherwise
  // this gets stepped straight back per set_clock()'s own behavior).
  const pad = (n) => String(n).padStart(2, "0");
  document.getElementById("clock-date").value =
    `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
  document.getElementById("clock-time").value =
    `${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`;
  const tzSelect = document.getElementById("clock-tz");
  tzSelect.value = tz;
  currentFormTimezone = tz;
}

document.getElementById("clock-get-client-time").addEventListener("click", getClientTime);

async function poll() {
  const indicator = document.getElementById("refresh-indicator");
  try {
    const res = await fetch("/api/status");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    renderAp(data.ap);
    renderUplink(data.uplink, data.wan_ip);
    renderWifiAdapters(data.uplink_wifi_adapters);
    renderWifiConnectState(data.wifi_connect);
    renderSpeedtest(data.speedtest);
    renderServices(data.services);
    renderClients(data.clients);
    renderSessions(data.sessions);
    renderClock(data.clock);
    vpnClients = data.clients;
    renderVpn(data.vpn);
    indicator.classList.remove("stale");
  } catch (err) {
    indicator.classList.add("stale");
  }
}

async function refreshNow(btn) {
  // Clients/sessions already auto-update every poll cycle (5s); this just
  // triggers one immediately instead of waiting out the cycle.
  btn.disabled = true;
  const original = btn.textContent;
  btn.textContent = "Refreshing…";
  try {
    await poll();
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
}

document.getElementById("clients-refresh").addEventListener("click", (e) => refreshNow(e.currentTarget));
document.getElementById("sessions-refresh").addEventListener("click", (e) => refreshNow(e.currentTarget));

async function runSpeedtest() {
  const btn = document.getElementById("speedtest-run");
  const provider = document.getElementById("speedtest-provider").value;
  // Optimistic feedback the instant it's clicked; poll() right after gives
  // the authoritative "running" state (also true if it was already running,
  // e.g. fired automatically by the uplink-up dispatcher) and every poll
  // after that keeps it accurate until it finishes.
  btn.disabled = true;
  btn.textContent = "Running…";
  try {
    await fetch("/api/speedtest/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider }),
    });
  } finally {
    poll();
  }
}

document.getElementById("speedtest-run").addEventListener("click", runSpeedtest);

function startPowerCountdown(action, seconds) {
  const message = document.getElementById("power-message");
  const label = action === "reboot" ? "Restarting" : "Shutting down";
  let remaining = seconds;
  message.className = "message";
  message.textContent = `${label} in ${remaining}s…`;
  const timer = setInterval(() => {
    remaining -= 1;
    if (remaining <= 0) {
      clearInterval(timer);
      message.textContent =
        action === "reboot"
          ? "Restarting now — the dashboard will be back in a minute or two."
          : "Shutting down now — power the Pi back on by hand when you need it again.";
      return;
    }
    message.textContent = `${label} in ${remaining}s…`;
  }, 1000);
}

async function triggerPower(action) {
  const label = action === "reboot" ? "restart" : "shut down";
  let confirmMsg = `Are you sure you want to ${label} the Pi?`;
  if (lastClientCount > 0) {
    confirmMsg += ` ${lastClientCount} device${lastClientCount === 1 ? "" : "s"} currently connected to the AP will lose their connection.`;
  }
  if (action === "shutdown") {
    confirmMsg += " It will stay off until it's powered back on by hand — there's no remote power-on.";
  }
  if (!window.confirm(confirmMsg)) return;

  const rebootBtn = document.getElementById("power-reboot");
  const shutdownBtn = document.getElementById("power-shutdown");
  const message = document.getElementById("power-message");
  rebootBtn.disabled = true;
  shutdownBtn.disabled = true;
  message.textContent = "";
  message.className = "message";

  try {
    const res = await fetch("/api/system/power", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, confirm: true }),
    });
    const data = await res.json();
    if (res.ok && data.status === "scheduled") {
      startPowerCountdown(data.action, data.delay_seconds);
    } else {
      message.textContent = data.message || `Failed to ${label}.`;
      message.className = "message error";
      rebootBtn.disabled = false;
      shutdownBtn.disabled = false;
    }
  } catch (err) {
    message.textContent = "Request failed.";
    message.className = "message error";
    rebootBtn.disabled = false;
    shutdownBtn.disabled = false;
  }
}

document.getElementById("power-reboot").addEventListener("click", () => triggerPower("reboot"));
document.getElementById("power-shutdown").addEventListener("click", () => triggerPower("shutdown"));

// Provider field schema + help steps -- fetched once from the backend
// rather than duplicated here, so a future non-ProtonVPN provider only
// needs a backend change (VPN_PROVIDERS in app.py), not a frontend one too.
let vpnProviders = {};

function renderVpnProviderFields() {
  const provider = document.getElementById("vpn-provider-select").value;
  const info = vpnProviders[provider] || { fields: [], help: [], help_url: null };

  const help = document.getElementById("vpn-provider-help");
  help.innerHTML = info.help
    .map((step, i) => {
      if (i === 0 && info.help_url) {
        return `<li><a href="${info.help_url}" target="_blank" rel="noopener">${step}</a></li>`;
      }
      return `<li>${step}</li>`;
    })
    .join("");

  const container = document.getElementById("vpn-provider-fields");
  container.innerHTML = "";
  for (const field of info.fields) {
    const label = document.createElement("label");
    label.textContent = field.label;
    const input = document.createElement(field.type === "textarea" ? "textarea" : "input");
    if (field.type !== "textarea") input.type = field.type;
    else {
      input.rows = 8;
      input.style.fontFamily = "monospace";
      input.style.fontSize = "0.8rem";
    }
    input.id = `vpn-field-${field.name}`;
    input.placeholder = field.placeholder || "";
    input.autocomplete = "off";
    label.appendChild(input);
    container.appendChild(label);

    // Paste-or-browse -- a file picker alongside the textarea reads the
    // chosen file straight into it (still editable/reviewable before
    // saving, doesn't submit anything on its own), so you don't have to
    // open the file yourself just to copy its contents.
    if (field.type === "textarea") {
      const fileLabel = document.createElement("label");
      fileLabel.className = "vpn-file-picker";
      fileLabel.textContent = "…or browse for the file";
      const fileInput = document.createElement("input");
      fileInput.type = "file";
      fileInput.accept = ".conf,.ovpn,.txt,text/plain";
      fileInput.addEventListener("change", () => {
        const file = fileInput.files[0];
        if (!file) return;
        const reader = new FileReader();
        reader.onload = () => {
          input.value = reader.result;
        };
        reader.readAsText(file);
      });
      fileLabel.appendChild(fileInput);
      container.appendChild(fileLabel);
    }
  }
}

async function loadVpnProviders() {
  try {
    const res = await fetch("/api/vpn/providers");
    if (!res.ok) return;
    const providers = await res.json();
    const select = document.getElementById("vpn-provider-select");
    select.innerHTML = providers.map((p) => `<option value="${p.id}">${p.label}</option>`).join("");
    vpnProviders = Object.fromEntries(providers.map((p) => [p.id, p]));
    renderVpnProviderFields();
  } catch (err) {
    // non-fatal -- the static ProtonVPN <option> already in the HTML still works
  }
}

document.getElementById("vpn-provider-select").addEventListener("change", renderVpnProviderFields);

async function saveVpnConfig() {
  const btn = document.getElementById("vpn-save-config");
  const message = document.getElementById("vpn-config-message");
  const provider = document.getElementById("vpn-provider-select").value;
  const label = document.getElementById("vpn-config-label").value.trim();
  const fields = {};
  for (const field of (vpnProviders[provider] || { fields: [] }).fields) {
    const el = document.getElementById(`vpn-field-${field.name}`);
    if (el) fields[field.name] = el.value;
  }

  btn.disabled = true;
  message.textContent = "";
  message.className = "message";
  try {
    const res = await fetch("/api/vpn/configs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ label, provider, fields }),
    });
    const data = await res.json();
    if (res.ok && data.status === "ok") {
      message.textContent = `Saved "${label}".`;
      message.className = "message ok";
      document.getElementById("vpn-config-label").value = "";
      renderVpnProviderFields(); // clears the pasted config from the form
    } else {
      message.textContent = data.message || "Failed to save VPN configuration.";
      message.className = "message error";
    }
  } catch (err) {
    message.textContent = "Request failed.";
    message.className = "message error";
  } finally {
    btn.disabled = false;
    poll();
  }
}

document.getElementById("vpn-save-config").addEventListener("click", saveVpnConfig);

function renderVpnConfigList(vpn) {
  const list = document.getElementById("vpn-config-list");
  const configs = vpn.configs || [];
  list.innerHTML = "";
  if (!configs.length) {
    list.innerHTML = '<li class="empty">No configs saved yet</li>';
    return;
  }
  for (const c of configs) {
    const isActive = c.id === vpn.active_config_id;
    const li = document.createElement("li");
    li.className = isActive ? "selected" : "";

    const info = document.createElement("span");
    const providerLabel = (vpnProviders[c.provider] || {}).label || c.provider;
    const activeTag = isActive ? (vpn.connected ? " (connected)" : " (selected, disconnected)") : "";
    info.textContent = `${c.label} — ${providerLabel}${activeTag}`;

    const actions = document.createElement("span");
    actions.className = "list-row-actions";

    const connectBtn = document.createElement("button");
    if (isActive && vpn.connected) {
      connectBtn.textContent = "Disconnect";
      connectBtn.className = "btn-warn";
      connectBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        vpnConfigAction(c.id, "disconnect");
      });
    } else {
      connectBtn.textContent = "Connect";
      connectBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        vpnConfigAction(c.id, isActive ? "connect" : "activate");
      });
    }

    const deleteBtn = document.createElement("button");
    deleteBtn.textContent = "Delete";
    deleteBtn.className = "btn-danger";
    deleteBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      vpnConfigAction(c.id, "delete", c.label);
    });

    actions.appendChild(connectBtn);
    actions.appendChild(deleteBtn);
    li.appendChild(info);
    li.appendChild(actions);
    list.appendChild(li);
  }
}

async function vpnConfigAction(configId, action, label) {
  const message = document.getElementById("vpn-config-action-message");

  if (action === "delete" && !window.confirm(`Delete "${label}"? This can't be undone.`)) {
    return;
  }

  const verbs = { activate: "Connecting…", connect: "Connecting…", disconnect: "Disconnecting…", delete: "Deleting…" };
  message.textContent = verbs[action];
  message.className = "message";

  try {
    let res;
    if (action === "activate") {
      res = await fetch(`/api/vpn/configs/${configId}/activate`, { method: "POST" });
    } else if (action === "connect") {
      res = await fetch("/api/vpn/connect", { method: "POST" });
    } else if (action === "disconnect") {
      res = await fetch("/api/vpn/disconnect", { method: "POST" });
    } else {
      res = await fetch(`/api/vpn/configs/${configId}`, { method: "DELETE" });
    }
    const data = await res.json();
    if (res.ok && data.status === "ok") {
      message.textContent = "";
    } else {
      message.textContent = data.message || `Failed to ${action}.`;
      message.className = "message error";
    }
  } catch (err) {
    message.textContent = "Request failed.";
    message.className = "message error";
  } finally {
    poll();
  }
}

let vpnClients = [];
let vpnSelectedMacs = new Set();

function renderVpnClientList() {
  const list = document.getElementById("vpn-client-list");
  const mode = document.getElementById("vpn-mode-select").value;
  list.hidden = mode !== "selected";
  if (mode !== "selected") return;

  list.innerHTML = "";
  if (!vpnClients.length) {
    list.innerHTML = '<li class="empty">No clients connected</li>';
    return;
  }
  for (const c of vpnClients) {
    const li = document.createElement("li");
    li.className = vpnSelectedMacs.has(c.mac) ? "selected" : "";
    li.innerHTML = `<span>${c.hostname || c.mac}</span><span class="wifi-meta">${c.ip || "—"}</span>`;
    li.addEventListener("click", () => {
      if (vpnSelectedMacs.has(c.mac)) vpnSelectedMacs.delete(c.mac);
      else vpnSelectedMacs.add(c.mac);
      renderVpnClientList();
    });
    list.appendChild(li);
  }
}

document.getElementById("vpn-mode-select").addEventListener("change", renderVpnClientList);

let vpnModeInitialized = false;

function fmtBytes(bytes) {
  if (bytes == null) return "—";
  return `${(bytes / 1_000_000).toFixed(1)} MB`;
}

function renderVpn(vpn) {
  setText("vpn-label", vpn.label || "none selected");
  const connected = document.getElementById("vpn-connected");
  connected.textContent = vpn.connected ? "connected" : vpn.configured ? "disconnected" : "not configured";
  connected.className = `status ${vpn.connected ? "up" : "unknown"}`;
  setText("vpn-tunnel-ip", vpn.tunnel_ip);
  setText("vpn-endpoint", vpn.endpoint);
  setText("vpn-handshake", vpn.latest_handshake ? new Date(vpn.latest_handshake * 1000).toLocaleString() : "—");
  setText(
    "vpn-transfer",
    vpn.rx_bytes != null && vpn.tx_bytes != null ? `${fmtBytes(vpn.rx_bytes)} down / ${fmtBytes(vpn.tx_bytes)} up` : "—"
  );

  renderVpnConfigList(vpn);

  // Only prefill once, same reasoning as the clock form -- a poll landing
  // mid-selection shouldn't stomp on what the user is currently choosing.
  if (!vpnModeInitialized) {
    document.getElementById("vpn-mode-select").value = vpn.mode || "off";
    vpnSelectedMacs = new Set(vpn.selected_clients || []);
    vpnModeInitialized = true;
  }
  renderVpnClientList();
}

async function applyVpnMode() {
  const btn = document.getElementById("vpn-mode-apply");
  const message = document.getElementById("vpn-mode-message");
  const mode = document.getElementById("vpn-mode-select").value;
  const macs = mode === "selected" ? Array.from(vpnSelectedMacs) : [];

  btn.disabled = true;
  message.textContent = "";
  message.className = "message";
  try {
    const res = await fetch("/api/vpn/mode", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode, macs }),
    });
    const data = await res.json();
    if (res.ok && data.status === "ok") {
      message.textContent = "Traffic mode applied.";
      message.className = "message ok";
    } else {
      message.textContent = data.message || "Failed to apply traffic mode.";
      message.className = "message error";
    }
  } catch (err) {
    message.textContent = "Request failed.";
    message.className = "message error";
  } finally {
    btn.disabled = false;
    poll();
  }
}

document.getElementById("vpn-mode-apply").addEventListener("click", applyVpnMode);
loadVpnProviders();

poll();
setInterval(poll, POLL_MS);
