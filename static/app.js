'use strict';

const $ = (id) => document.getElementById(id);
let pollTimer = null;
let speedTimer = null;
let speedWatchTimer = null;
let speedStartedAt = 0;

const READABLE_STATES = new Set(['available', 'stale']);

/* ---------- normalized-model helpers ---------- */
function setText(id, value) {
  const el = $(id);
  if (el) el.textContent = (value === undefined || value === null || value === '') ? '–' : value;
}
function esc(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
function show(id, visible) { $(id).hidden = !visible; }
function metric(model, domain, metricId) {
  const domains = (model && model.domains) || {};
  return ((domains[domain] || {})[metricId]) || {
    availability: 'not_loaded', value: null, source_value: null, unit: null
  };
}
function metricText(m, suffix) {
  if (READABLE_STATES.has(m.availability) && m.value !== null && m.value !== undefined && m.value !== '') {
    return String(m.value) + (suffix || '');
  }
  if (m.availability === 'not_loaded') return '–';
  return String(m.availability).replace(/_/g, ' ');
}
function valueText(model, domain, metricId, suffix) {
  return metricText(metric(model, domain, metricId), suffix);
}
function kvTable(id, rows) {
  const table = $(id);
  if (!table) return;
  table.innerHTML = rows.map(([k, v]) =>
    `<tr><td>${esc(k)}</td><td>${esc(v == null || v === '' ? '–' : v)}</td></tr>`
  ).join('');
}
function humanUptime(seconds) {
  if (seconds === null || seconds === undefined || seconds === '') return '–';
  const n = Number(seconds);
  if (!Number.isFinite(n) || n < 0) return 'invalid';
  const hours = Math.floor(n / 3600);
  const minutes = Math.floor((n % 3600) / 60);
  const secs = Math.floor(n % 60);
  if (hours >= 24) {
    const days = Math.floor(hours / 24);
    return `${days}d ${hours % 24}h ${minutes}m`;
  }
  return `${hours}h ${minutes}m ${secs}s`;
}
function collection(model, name) {
  return ((model && model.collections) || {})[name] || {
    name, items: [], loaded: false, availability: 'not_loaded', error: null
  };
}
function collectionChips(model, name) {
  const data = collection(model, name);
  if (!data.loaded) return `<span class="muted">${esc(data.availability.replace(/_/g, ' '))}</span>`;
  return data.items.map(item => `<span class="chip">${esc(item.name || item.id || '')}</span>`).join('') ||
    '<span class="muted">none</span>';
}

/* ---------- discovery ---------- */
async function loadDiscovery() {
  try {
    const res = await fetch('/api/discovery');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    if (data.gateway) $('gatewayInput').value = data.gateway;
    $('gatewayBadge').textContent = data.gateway +
      (data.detected ? ' ✓' : ' (guess)');

    $('detectedDevice').textContent = data.detected_device ||
      (data.detected ? 'Gateway detected' : 'Gateway not detected');
    $('profileMatch').textContent = data.selected_profile
      ? `${data.selected_profile} (${data.identification_confidence})`
      : 'Unknown firmware — choose a profile';
    $('discoveryEvidence').textContent = data.evidence || 'No identification evidence.';
    const hint = data.credential_suggestion || {};
    $('credentialHint').textContent = hint.label
      ? `${hint.label}. ${hint.provenance || ''}`
      : 'No credential values are stored in this app.';

    const select = $('profileInput');
    const previous = select.value;
    select.innerHTML = '<option value="">Select a profile…</option>' +
      (data.profiles || []).map(profile =>
        `<option value="${esc(profile.id)}">${esc(profile.display_name)} · ${esc(profile.status || 'observed')}</option>`
      ).join('');
    select.value = data.selected_profile || (previous &&
      (data.profiles || []).some(p => p.id === previous) ? previous : '');

    if ((data.discovery_errors || []).length) {
      $('discoveryEvidence').textContent += ' · ' + data.discovery_errors.join(' · ');
    }
  } catch (e) {
    $('detectedDevice').textContent = 'discovery unavailable';
    $('discoveryEvidence').textContent = e.message;
  }
}

/* ---------- connect ---------- */
$('loginForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const btn = $('loginBtn');
  const msg = $('loginMsg');
  btn.disabled = true;
  btn.textContent = 'Connecting…';
  msg.className = 'msg';
  msg.textContent = 'Authenticating with the selected connector profile…';

  try {
    const res = await fetch('/api/connect', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        gateway: $('gatewayInput').value.trim(),
        profile_id: $('profileInput').value,
        username: $('userInput').value,
        password: $('passInput').value,
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
    $('lastUpdate').textContent = 'refresh failed: ' + e.message;
  }
}

function renderVoip(model) {
  const phoneMetric = metric(model, 'voip', 'voip.phone_number');
  const registration = metric(model, 'voip', 'voip.registration_state');
  const action = model.call_action || { enabled: false, dial_uri: null };
  const phone = READABLE_STATES.has(phoneMetric.availability) ? phoneMetric.value : null;
  const stateText = metricText(registration);

  setText('voipPhone', phone);
  $('voipState').textContent = stateText === '–'
    ? String(phoneMetric.availability).replace(/_/g, ' ')
    : stateText;
  $('voipState').className = 'pill' + (action.enabled ? '' : ' warn');

  const btn = $('voipCallBtn');
  const dialUri = action.enabled && typeof action.dial_uri === 'string' &&
    action.dial_uri.startsWith('tel:') ? action.dial_uri : null;
  btn.dataset.dialUri = dialUri || '';
  btn.hidden = !dialUri;
  btn.disabled = !dialUri;
  btn.title = dialUri ? 'Open phone dialer · not ONT SIP' : 'No validated phone number';
}

function renderSpeed(model) {
  const download = metric(model, 'speed_test', 'speed_test.download_mbps');
  const upload = metric(model, 'speed_test', 'speed_test.upload_mbps');
  const hasResult = READABLE_STATES.has(download.availability) &&
    READABLE_STATES.has(upload.availability);
  if (!hasResult) return;
  setText('spDown', download.value);
  setText('spUp', upload.value);
  setText('spPing', valueText(model, 'speed_test', 'speed_test.ping_ms'));
  setText('spJitter', valueText(model, 'speed_test', 'speed_test.jitter_ms'));
  const sponsor = metric(model, 'speed_test', 'speed_test.sponsor');
  const server = metric(model, 'speed_test', 'speed_test.server');
  const country = metric(model, 'speed_test', 'speed_test.country');
  $('spServer').textContent = [sponsor.value, server.value, country.value]
    .filter(Boolean).join(' · ');
  show('speedIdle', false);
  show('speedResult', true);
}

function render(model) {
  if (!model || !model.domains) {
    $('lastUpdate').textContent = 'unexpected dashboard payload';
    return;
  }

  const uptime = metric(model, 'system', 'system.uptime_s');
  const wanStatus = metric(model, 'wan', 'wan.status');
  const ponMode = metric(model, 'optical', 'optical.pon_mode');
  const devicesCount = metric(model, 'connected_devices', 'devices.count');
  const modelName = metric(model, 'device_identity', 'device.model');
  const operator = metric(model, 'device_identity', 'device.operator');

  setText('sPublicIp', valueText(model, 'wan', 'wan.public_ip'));
  setText('sStatus', metricText(wanStatus));
  setText('sUptime', READABLE_STATES.has(uptime.availability)
    ? humanUptime(uptime.value) : metricText(uptime));
  setText('sTx', valueText(model, 'optical', 'optical.tx_power_dbm'));
  setText('sRx', valueText(model, 'optical', 'optical.rx_power_dbm'));
  setText('sDevices', metricText(devicesCount));
  setText('sModel', metricText(modelName));
  setText('sOperator', metricText(operator));
  renderVoip(model);
  renderSpeed(model);

  $('wanStatusPill').textContent = metricText(wanStatus);
  $('wanStatusPill').className = 'pill' + (wanStatus.value === 'Connected' ? '' : ' warn');
  $('ponPill').textContent = metricText(ponMode);
  $('deviceCount').textContent = metricText(devicesCount);

  kvTable('wanTable', [
    ['Connection', valueText(model, 'wan', 'wan.connection_name')],
    ['Public IP', valueText(model, 'wan', 'wan.public_ip')],
    ['Type', valueText(model, 'wan', 'wan.connection_type')],
    ['Status', metricText(wanStatus)],
    ['ISP gateway', valueText(model, 'wan', 'wan.gateway_ip')],
    ['DNS servers', valueText(model, 'wan', 'wan.dns_servers')],
    ['VLAN ID', valueText(model, 'wan', 'wan.vlan_id')],
    ['PPPoE user', valueText(model, 'wan', 'wan.pppoe_user')],
    ['WAN MAC', valueText(model, 'wan', 'wan.mac')],
    ['MTU', valueText(model, 'wan', 'wan.mtu')],
    ['NAT', valueText(model, 'wan', 'wan.nat')],
    ['Connection uptime', READABLE_STATES.has(metric(model, 'wan', 'wan.uptime_s').availability)
      ? humanUptime(metric(model, 'wan', 'wan.uptime_s').value)
      : metricText(metric(model, 'wan', 'wan.uptime_s'))],
    ['Last error', valueText(model, 'wan', 'wan.last_error')],
  ]);

  kvTable('ponTable', [
    ['PON mode', valueText(model, 'optical', 'optical.pon_mode')],
    ['Optical TX power', valueText(model, 'optical', 'optical.tx_power_dbm', ' dBm')],
    ['Optical RX power', valueText(model, 'optical', 'optical.rx_power_dbm', ' dBm')],
    ['Transceiver temp', valueText(model, 'optical', 'optical.temperature_c', ' °C')],
    ['Laser bias current', valueText(model, 'optical', 'optical.bias_current_ma', ' mA')],
    ['Supply voltage', valueText(model, 'optical', 'optical.supply_voltage_v', ' V')],
    ['ONU ID', valueText(model, 'optical', 'optical.onu_id')],
    ['OLT vendor', valueText(model, 'optical', 'optical.olt_vendor')],
    ['Registration state', valueText(model, 'optical', 'optical.registration_state')],
    ['Auth status', valueText(model, 'optical', 'optical.auth_status')],
    ['LoID', valueText(model, 'optical', 'optical.loid')],
    ['Bytes sent', valueText(model, 'optical', 'optical.bytes_sent')],
    ['Bytes received', valueText(model, 'optical', 'optical.bytes_received')],
    ['Packets sent', valueText(model, 'optical', 'optical.packets_sent')],
    ['Packets received', valueText(model, 'optical', 'optical.packets_received')],
  ]);

  kvTable('systemTable', [
    ['Manufacturer', valueText(model, 'device_identity', 'device.manufacturer')],
    ['Model', metricText(modelName)],
    ['Hardware version', valueText(model, 'device_identity', 'device.hardware')],
    ['Software version', valueText(model, 'device_identity', 'device.firmware')],
    ['Serial number', valueText(model, 'device_identity', 'device.serial')],
    ['GPON SN', valueText(model, 'device_identity', 'device.gpon_sn')],
    ['Operator', metricText(operator)],
    ['Base MAC', valueText(model, 'device_identity', 'device.mac_base')],
    ['WAN MAC', valueText(model, 'wan', 'wan.mac')],
    ['System uptime', READABLE_STATES.has(uptime.availability)
      ? humanUptime(uptime.value) : metricText(uptime)],
    ['CPU usage', valueText(model, 'system', 'system.cpu_percent', '%')],
    ['Memory usage', valueText(model, 'system', 'system.memory_percent', '%')],
    ['Flash usage', valueText(model, 'system', 'system.flash_percent', '%')],
    ['LAN ports', valueText(model, 'system', 'system.lan_port_count')],
    ['Uplink', valueText(model, 'system', 'system.uplink_mode')],
    ['Kernel', valueText(model, 'system', 'system.kernel')],
    ['Compiled', valueText(model, 'system', 'system.compile_time')],
  ]);

  const lan = collection(model, 'lan_ports');
  kvTable('lanTable', lan.loaded
    ? lan.items.map(item => [item.name || item.id || 'port', item.status || '–'])
    : [['LAN ports', lan.availability.replace(/_/g, ' ')]]);
  $('ssidList').innerHTML = collectionChips(model, 'ssids');

  const tbody = $('deviceTable').querySelector('tbody');
  const deviceData = collection(model, 'devices');
  if (!deviceData.loaded || !model.devices_loaded) {
    const stateText = deviceData.availability.replace(/_/g, ' ');
    tbody.innerHTML = `<tr><td colspan="7" class="muted">${esc(stateText)}</td></tr>`;
  } else {
    tbody.innerHTML = model.devices.map((item, i) => {
      const sourceClass = item.address_source === 'DHCP' ? 'dhcp' : '';
      const linkClass = /2\.4G|5G/i.test(item.link_type || '') ? 'wifi' : '';
      return `<tr>
        <td>${i + 1}</td>
        <td>${esc(item.hostname || 'unknown')}</td>
        <td class="mono">${esc(item.ip_address || '')}</td>
        <td class="mono">${esc(item.mac_address || '')}</td>
        <td><span class="tag ${linkClass}">${esc(item.link_type || '-')}</span></td>
        <td><span class="tag ${sourceClass}">${esc(item.address_source || '')}</span></td>
        <td class="mono">${esc(humanUptime(item.uptime_s))}</td>
      </tr>`;
    }).join('') || '<tr><td colspan="7" class="muted">none</td></tr>';
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
    if (s.status === 'done') {
      refreshData();
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
  msg.className = 'msg' + (message && message !== 'Disconnected.' ? ' error' : ' ok');
  msg.textContent = message || '';
  loadDiscovery();
}

/* ---------- PWA (install from the browser menu) ---------- */
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/static/sw.js').catch(() => {});
  });
}

/* ---------- init ---------- */
async function init() {
  await loadDiscovery();
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
