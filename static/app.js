'use strict';

const $ = (id) => document.getElementById(id);
let pollTimer = null;
let speedTimer = null;
let speedWatchTimer = null;
let speedStartedAt = 0;

/* ---------- helpers ---------- */
function setText(id, value) {
  const el = $(id);
  if (el) el.textContent = (value === undefined || value === null || value === '') ? '–' : value;
}
function esc(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
function kvTable(id, rows) {
  const table = $(id);
  table.innerHTML = rows.map(([k, v]) =>
    `<tr><td>${esc(k)}</td><td>${esc(v == null || v === '' ? '–' : v)}</td></tr>`
  ).join('');
}
function setBar(id, percent) {
  const el = $(id);
  if (el) el.style.width = Math.min(100, Math.max(0, Number(percent) || 0)) + '%';
}
function show(id, visible) { $(id).hidden = !visible; }

/* Accept only digits, separators, and one optional leading '+'. */
function dialUriFrom(value) {
  if (value === undefined || value === null) return null;
  const raw = String(value).trim();
  if (!raw || /[A-Za-z]/.test(raw)) return null;
  if ((raw.match(/\+/g) || []).length > 1) return null;
  const international = raw.startsWith('+');
  if (raw.includes('+') && !international) return null;
  const digits = raw.replace(/\D/g, '');
  if (digits.length < 7 || digits.length > 15) return null;
  return encodeURI('tel:' + (international ? '+' : '') + digits);
}

function metricValue(model, domain, metricId) {
  const domains = (model && model.domains) || {};
  const metric = (domains[domain] || {})[metricId] || {};
  return metric;
}

/* ---------- gateway detection ---------- */
async function loadGateway() {
  try {
    const res = await fetch('/api/gateway');
    const data = await res.json();
    if (data.gateway) {
      $('gatewayInput').value = data.gateway;
      $('loginGateway') && ($('loginGateway').textContent = data.gateway);
      $('gatewayBadge').textContent = data.gateway +
        (data.detected ? ' ✓' : ' (guess)');
    }
  } catch (e) { /* keep the fallback value */ }
}

/* ---------- login ---------- */
$('loginForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const btn = $('loginBtn');
  const msg = $('loginMsg');
  btn.disabled = true;
  btn.textContent = 'Connecting…';
  msg.className = 'msg';
  msg.textContent = 'Detecting gateway and logging in…';

  try {
    const res = await fetch('/api/connect', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        gateway: $('gatewayInput').value.trim(),
        username: $('userInput').value.trim(),
        password: $('passInput').value,
        mode: $('modeInput').value,
      }),
    });
    const data = await res.json();
    if (data.ok) {
      msg.className = 'msg ok';
      msg.textContent = data.message || 'Connected.';
      $('gatewayBadge').textContent = (data.gateway || '') + ' ✓';
      setTimeout(enterDashboard, 350);
    } else {
      msg.className = 'msg error';
      msg.textContent = data.message || 'Connection failed.';
    }
  } catch (e) {
    msg.className = 'msg error';
    msg.textContent = 'Could not reach the local app: ' + e.message;
  } finally {
    btn.disabled = false;
    btn.textContent = 'Connect →';
  }
});

$('showPass').addEventListener('click', () => {
  const input = $('passInput');
  input.type = input.type === 'password' ? 'text' : 'password';
});

function enterDashboard() {
  show('loginView', false);
  show('dashView', true);
  show('speedBtn', true);
  show('disconnectBtn', true);
  $('statusDot').className = 'dot dot-on';
  show('speedPanel', true);
  refreshData();
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(refreshData, 5000);
}

/* ---------- data refresh ---------- */
async function refreshData() {
  try {
    const res = await fetch('/api/data');
    if (res.status === 401) {
      leaveDashboard('Session expired. Please reconnect.');
      return;
    }
    if (!res.ok) throw new Error('HTTP ' + res.status);
    render(await res.json());
  } catch (e) {
    // keep the last known values on a transient failure
    $('lastUpdate').textContent = 'refresh failed: ' + e.message;
  }
}

function renderVoip(d) {
  const voip = d.voip || {};
  const phoneMetric = metricValue(d, 'voip', 'voip.phone_number');
  const regMetric = metricValue(d, 'voip', 'voip.registration_state');
  const action = d.call_action || voip.call_action || null;

  const phone = phoneMetric.value !== undefined
    ? phoneMetric.value
    : (voip.phone_number || voip.phoneNumber || null);
  const availability = phoneMetric.availability || voip.phone_number_availability
    || voip.availability || 'not_loaded';
  const registration = regMetric.value !== undefined
    ? regMetric.value
    : (voip.registration_state || voip.registrationState || null);

  let dialUri = null;
  let reason = action ? action.reason : null;
  if (action) {
    if (action.enabled && action.dial_uri) dialUri = action.dial_uri;
  } else if (availability === 'available' || availability === 'stale') {
    dialUri = dialUriFrom(phone);
    if (!dialUri) reason = 'Phone number failed validation.';
  } else {
    reason = 'No validated phone number is available for this profile.';
  }

  const stateText = registration || String(availability).replace(/_/g, ' ');
  $('voipState').textContent = stateText;
  $('voipState').className = 'pill' + (dialUri ? '' : ' warn');
  setText('voipPhone', phone);
  setText('voipRegistration', registration ? 'Registration: ' + registration : 'State: ' + stateText);

  const btn = $('voipCallBtn');
  btn.dataset.dialUri = dialUri || '';
  btn.hidden = !dialUri;
  btn.disabled = !dialUri;
  $('voipNote').textContent = dialUri
    ? 'Opens your phone dialer with the number prefilled. No call is placed automatically.'
    : (reason || 'Call is unavailable.');
}

function render(d) {
  const dev = d.device || {}, net = d.internet || {}, sys = d.system || {}, opt = d.optical || {};

  setText('sPublicIp', net.public_ip);
  setText('sStatus', net.status);
  setText('sUptime', sys.uptime);
  setText('sTx', opt.tx);
  setText('sRx', opt.rx);
  setText('sCpu', sys.cpu + '%');
  setText('sMem', sys.memory + '%');
  setText('sDevices', (d.devices || []).length);
  setText('sModel', dev.model);
  setText('sOperator', dev.operator);
  setBar('barCpu', sys.cpu);
  setBar('barMem', sys.memory);
  renderVoip(d);

  $('wanStatusPill').textContent = net.status || '–';
  $('wanStatusPill').className = net.status === 'Connected' ? 'pill' : 'pill warn';
  $('ponPill').textContent = opt.mode || '–';
  $('deviceCount').textContent = (d.devices || []).length;

  kvTable('wanTable', [
    ['Connection', net.name], ['Public IP', net.public_ip],
    ['Type', net.type], ['Status', net.status],
    ['ISP gateway', net.gateway], ['DNS servers', net.dns],
    ['VLAN ID', net.vlan], ['PPPoE user', net.pppoe_user],
    ['WAN MAC', dev.wan_mac], ['MTU', net.mtu], ['NAT', net.nat],
    ['Connection uptime', net.uptime], ['Last error', net.error],
  ]);

  kvTable('ponTable', [
    ['PON mode', opt.mode], ['Optical TX power', opt.tx ? opt.tx + ' dBm' : ''],
    ['Optical RX power', opt.rx ? opt.rx + ' dBm' : ''],
    ['Transceiver temp', opt.temperature ? opt.temperature + ' °C' : ''],
    ['Laser bias current', opt.current ? opt.current + ' mA' : ''],
    ['Supply voltage', opt.voltage ? opt.voltage + ' V' : ''],
    ['ONU ID', opt.onu_id], ['OLT vendor', opt.vendor],
    ['Registration state', opt.reg_state], ['Auth status', opt.auth],
    ['LoID', opt.loid],
    ['Bytes sent', opt.bytes_sent], ['Bytes received', opt.bytes_recv],
    ['Packets sent', opt.packets_sent], ['Packets received', opt.packets_recv],
  ]);

  kvTable('systemTable', [
    ['Manufacturer', dev.manufacturer], ['Model', dev.model],
    ['Hardware version', dev.hardware], ['Software version', dev.software],
    ['Serial number', dev.serial], ['GPON SN (hex)', dev.gpon_sn],
    ['Operator', dev.operator], ['Base MAC', dev.base_mac],
    ['System uptime', sys.uptime], ['CPU usage', sys.cpu + '%'],
    ['Memory usage', sys.memory + '%'], ['Flash usage', sys.flash + '%'],
    ['LAN ports', sys.lan_ports], ['Uplink', sys.uplink],
    ['Kernel', dev.os], ['Compiled', dev.compiled],
  ]);

  kvTable('lanTable', (d.lan_ports || []).map(p => [p.port, p.status]));

  $('ssidList').innerHTML = (d.ssids || []).map(s => `<span class="chip">${esc(s)}</span>`).join('') || '<span class="muted">none</span>';

  const tbody = $('deviceTable').querySelector('tbody');
  tbody.innerHTML = (d.devices || []).map((item, i) => {
    const sourceClass = item.source === 'DHCP' ? 'dhcp' : '';
    const linkClass = /2\.4G|5G/i.test(item.type) ? 'wifi' : '';
    return `<tr>
      <td>${i + 1}</td>
      <td>${esc(item.host)}</td>
      <td class="mono">${esc(item.ip)}</td>
      <td class="mono">${esc(item.mac)}</td>
      <td><span class="tag ${linkClass}">${esc(item.type)}</span></td>
      <td><span class="tag ${sourceClass}">${esc(item.source)}</span></td>
      <td class="mono">${esc(item.uptime)}</td>
    </tr>`;
  }).join('');

  // Non-FiberHome routers: show whatever the device actually exposes.
  const generic = d.generic || [];
  show('genericPanel', generic.length > 0);
  if (generic.length) {
    $('profilePill').textContent = d.profile || 'generic';
    $('genericContent').innerHTML = generic.map(item =>
      `<div class="generic-block"><h3>${esc(item.endpoint)}</h3>` +
      `<pre class="generic-pre">${esc(JSON.stringify(item.data, null, 2))}</pre></div>`
    ).join('');
  }

  $('lastUpdate').textContent = 'last update ' + new Date().toLocaleTimeString();
}

/* ---------- voip call (explicit tap only) ---------- */
$('voipCallBtn').addEventListener('click', () => {
  const uri = $('voipCallBtn').dataset.dialUri;
  if (!uri || !uri.startsWith('tel:')) return;
  window.location.href = uri;
});

/* ---------- speedtest ---------- */
$('speedBtn').addEventListener('click', async () => {
  const btn = $('speedBtn');
  btn.disabled = true;
  try {
    const res = await fetch('/api/speedtest', { method: 'POST' });
    const data = await res.json();
    if (!data.ok) { alert(data.message || 'Could not start test.'); return; }
    show('speedPanel', true);
    show('speedIdle', false);
    show('speedResult', false);
    show('speedError', false);
    show('speedProgress', true);
    if (speedTimer) clearInterval(speedTimer);
    if (speedWatchTimer) clearInterval(speedWatchTimer);
    speedStartedAt = Date.now();
    speedTimer = setInterval(pollSpeed, 1000);
    speedWatchTimer = setInterval(() => {
      if (Date.now() - speedStartedAt > 135000) {
        clearInterval(speedWatchTimer);
        speedWatchTimer = null;
        $('speedError').textContent = 'Speedtest took too long. Please try again.';
        show('speedError', true);
        show('speedProgress', false);
        show('speedIdle', true);
        $('speedState').textContent = 'timeout';
      }
    }, 2000);
    pollSpeed();
  } finally { btn.disabled = false; }
});

async function pollSpeed() {
  try {
    const res = await fetch('/api/speedtest/status');
    const s = await res.json();
    $('speedState').textContent = s.status;
    $('speedState').className = 'pill' + (s.status === 'error' ? ' warn' : '');
    if (s.status === 'running' || s.status === 'starting') {
      show('speedProgress', true);
      setText('speedProgressText', s.progress || 'Working…');
      return;
    }
    clearInterval(speedTimer);
    speedTimer = null;
    if (speedWatchTimer) { clearInterval(speedWatchTimer); speedWatchTimer = null; }
    show('speedProgress', false);
    if (s.status === 'done' && s.result) {
      setText('spDown', s.result.download);
      setText('spUp', s.result.upload);
      setText('spPing', s.result.ping);
      setText('spJitter', s.result.jitter);
      $('spServer').textContent = [s.result.sponsor, s.result.server, s.result.country]
        .filter(Boolean).join(' · ');
      show('speedResult', true);
    } else if (s.status === 'error') {
      $('speedError').textContent = s.error || 'Speedtest failed.';
      show('speedError', true);
      show('speedIdle', true);
    }
  } catch (e) { /* try again next tick */ }
}

/* ---------- disconnect ---------- */
$('disconnectBtn').addEventListener('click', async () => {
  let message = 'Disconnected.';
  try {
    const response = await fetch('/api/disconnect', { method: 'POST' });
    const payload = await response.json();
    if (payload && payload.message) message = payload.message;
  } catch (e) { /* local state was still cleared by the server */ }
  leaveDashboard(message);
});

function leaveDashboard(message) {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  if (speedTimer) { clearInterval(speedTimer); speedTimer = null; }
  if (speedWatchTimer) { clearInterval(speedWatchTimer); speedWatchTimer = null; }
  show('dashView', false);
  show('loginView', true);
  show('speedBtn', false);
  show('disconnectBtn', false);
  $('statusDot').className = 'dot dot-off';
  const msg = $('loginMsg');
  msg.className = 'msg' + (message ? ' error' : '');
  msg.textContent = message || '';
}

/* ---------- PWA (install from the browser menu) ---------- */
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/static/sw.js').catch(() => {});
  });
}

/* ---------- init ---------- */
async function init() {
  await loadGateway();
  try {
    const res = await fetch('/api/session');
    const session = await res.json();
    if (session.connected) {
      if (session.gateway) $('gatewayBadge').textContent = session.gateway + ' ✓';
      enterDashboard();
      return;
    }
  } catch (e) { /* no active session — show login */ }
  show('loginView', true);
  show('dashView', false);
}
init();
