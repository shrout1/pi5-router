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
    apSsidFieldInitialized = true;
  }
}

async function updateAp() {
  const btn = document.getElementById("ap-update");
  const message = document.getElementById("ap-message");
  const ssid = document.getElementById("ap-ssid-input").value.trim();
  const passphrase = document.getElementById("ap-passphrase-input").value;

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
      body: JSON.stringify({ ssid, passphrase }),
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

function renderClients(clients) {
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

function renderClock(clock) {
  setText("clock-now", new Date(clock.now).toLocaleString());
  const ntp = document.getElementById("clock-ntp");
  ntp.textContent = clock.ntp_synchronized ? "synchronized" : "not synchronized";
  ntp.className = `status ${clock.ntp_synchronized ? "up" : "unknown"}`;

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

async function poll() {
  const indicator = document.getElementById("refresh-indicator");
  try {
    const res = await fetch("/api/status");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    renderAp(data.ap);
    renderUplink(data.uplink, data.wan_ip);
    renderWifiConnectState(data.wifi_connect);
    renderSpeedtest(data.speedtest);
    renderServices(data.services);
    renderClients(data.clients);
    renderSessions(data.sessions);
    renderClock(data.clock);
    indicator.classList.remove("stale");
  } catch (err) {
    indicator.classList.add("stale");
  }
}

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

poll();
setInterval(poll, POLL_MS);
