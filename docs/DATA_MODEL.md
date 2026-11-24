# Universal data model contract

**Status:** **Implemented** for the model and public serialization. Coverage of
individual router fields is **partial** because the shipped real profile was
derived from one observed FiberHome HG6145F1. Physical output equivalence is
**not evaluated**.

The authoritative definitions are `models/enums.py`, `models/metrics.py`,
`models/universal.py`, `models/phone.py`, and `_public_model_payload()` in
`app.py`. No other module may redefine these contracts.

## Seven domains

`Domain` is a stable, closed enum. The seven values and current metric counts
are:

| Domain | Value | Metric count |
|---|---|---:|
| Device identity | `device_identity` | 9 |
| WAN / Internet | `wan` | 13 |
| Optical / PON | `optical` | 15 |
| VoIP / Telephony | `voip` | 4 |
| System | `system` | 8 |
| Connected devices | `connected_devices` | 1 |
| Speed test | `speed_test` | 11 |

Total: **61 stable metric IDs**.

## Stable metric IDs

### `device_identity`

`device.manufacturer`, `device.model`, `device.firmware`, `device.hardware`,
`device.serial`, `device.gpon_sn`, `device.operator`, `device.mac_base`,
`device.mac_wan`

### `wan`

`wan.connection_name`, `wan.connection_type`, `wan.status`, `wan.public_ip`,
`wan.gateway_ip`, `wan.dns_servers`, `wan.vlan_id`, `wan.pppoe_user`,
`wan.mtu`, `wan.nat`, `wan.uptime_s`, `wan.last_error`, `wan.mac`

### `optical`

`optical.tx_power_dbm`, `optical.rx_power_dbm`,
`optical.temperature_c`, `optical.bias_current_ma`,
`optical.supply_voltage_v`, `optical.pon_mode`, `optical.onu_id`,
`optical.olt_vendor`, `optical.registration_state`, `optical.auth_status`,
`optical.loid`, `optical.bytes_sent`, `optical.bytes_received`,
`optical.packets_sent`, `optical.packets_received`

### `voip`

`voip.phone_number`, `voip.registration_state`, `voip.line_status`,
`voip.provider`

### `system`

`system.uptime_s`, `system.cpu_percent`, `system.memory_percent`,
`system.flash_percent`, `system.lan_port_count`, `system.uplink_mode`,
`system.kernel`, `system.compile_time`

### `connected_devices`

`devices.count`

### `speed_test`

`speed_test.status`, `speed_test.download_mbps`, `speed_test.upload_mbps`,
`speed_test.ping_ms`, `speed_test.jitter_ms`, `speed_test.server`,
`speed_test.sponsor`, `speed_test.country`, `speed_test.started_at`,
`speed_test.completed_at`, `speed_test.error`

`METRICS` maps each ID to exactly one domain, label, value type, and unit. An
unknown ID or a domain mismatch is rejected; `UniversalModel` never invents a
new metric at runtime.

## Availability vocabulary

`Availability` is a stable, closed enum. The exact serialized values are:

`available`, `stale`, `unavailable`, `unsupported`, `invalid`, `not_loaded`,
`error`

Invariants:

- Only `available` and `stale` may carry a non-null normalized `value`.
- Any other availability sets `value: null` but retains the connector's raw
  observation in `source_value`.
- A value-bearing metric with `value: null` is rejected at construction time.
- Missing metrics are returned as `not_loaded`, never as zero or an empty
  string.
- `observed_at` is generated only for value-bearing states.
- `error` carries a safe, secret-free explanation when extraction, endpoint
  access, validation, or a dial action fails.

## Value types and units

| `type` | Serialized `unit` |
|---|---|
| `string` | `null` |
| `integer` | `null` |
| `number` | `null` |
| `boolean` | `null` |
| `duration_seconds` | `"s"` |
| `ip_address` | `null` |
| `mac_address` | `null` |
| `percent` | `"%"` |
| `dbm` | `"dBm"` |
| `celsius` | `"°C"` |
| `milliamp` | `"mA"` |
| `volt` | `"V"` |
| `megabits_per_second` | `"Mbps"` |
| `milliseconds` | `"ms"` |
| `timestamp` | `null` |

## Serialized model shape

`UniversalModel.to_dict()` produces:

```text
gateway
domains
  <domain>: { <metric_id>: <metric object> }
devices
devices_loaded
collections
voip_lines
call_action
endpoint_errors
updated_at
```

Internal-only fields also present on the Python object are `profile_id` and
`compatibility`. They are removed by `_public_model_payload()` before
`jsonify()`, so `/api/data` must not expose them.

Each metric object has exactly:

```text
id, domain, label, type, unit, value, source_value,
availability, source, observed_at, error
```

Each connected device has:

```text
id, hostname, ip_address, mac_address, link_type,
address_source, uptime_s, availability, raw
```

Each collection has:

```text
name, items, count, loaded, availability, source, error
```

`count` is `null` unless the collection is loaded.

Each VoIP line has:

```text
line_id, phone_number, registration_state, line_status, call_action
```

## Derived aggregates

- `devices.count` is derived only when the `devices` collection is loaded. An
  unloaded collection produces `not_loaded`, or `error` when a failure reason
  exists.
- The primary VoIP line is assembled from `voip.phone_number`,
  `voip.registration_state`, and `voip.line_status`.
- `call_action` is `{"enabled": true, "dial_uri": "tel:…", "reason": null}` only
  when the phone number passes validation. Otherwise it is disabled with a
  safe reason and `dial_uri: null`. The model never places a call.
- Endpoint failures are recorded in `endpoint_errors` with `endpoint`,
  `source`, and `error` keys; they do not fabricate metric values.

## Speedtest injection

The backend keeps the last Speedtest state for API compatibility and injects it
into the model at `/api/data` read time under `source: "local_speedtest"`:

- `speed_test.status` is always set from the worker status.
- When status is `done`, result keys `download`, `upload`, `ping`, `jitter`,
  `server`, `sponsor`, and `country` populate their matching metric IDs.
- Otherwise those result metrics are `not_loaded`, or `error` when status is
  `error` and an error message exists.
- `started_at` and `completed_at` populate timestamp metrics when present.
- `speed_test.error` is an `error` metric only when status is `error`.

A fresh page load resets only the browser display. It does not clear backend
Speedtest state and does not change the `/api/data` or
`/api/speedtest/status` contracts.

## Frontend binding contract

`static/app.js` must continue reading canonical metric IDs from `model.domains`.
Do not reintroduce legacy payload keys such as `device`, `internet`, `generic`,
or `meta` into the public response. Do not add a `profile_id` or compatibility
field to `/api/data`.
