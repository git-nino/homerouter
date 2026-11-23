"""Legacy FiberHome view builder, retained only for regression comparison.

This module is NOT part of the live dashboard path. The live application uses
the JSON profile, ConnectorExecutor, and UniversalModel instead.
"""

from gateway import as_ip, human_uptime, parse_voice_status
from models.phone import build_call_action


def build_view(raw):
    """Build the historical display model from raw FiberHome responses."""
    base = raw.get("get_base_info", {}) or {}
    wan_data = raw.get("get_allwan_info", {}) or {}
    wan = (wan_data.get("wan") or [{}])[0]
    lan = (raw.get("get_lan_status", {}) or {}).get("lan_status", {}) or {}
    operator = raw.get("get_operator", {}) or {}
    dongles = (raw.get("get_dongle_info", {}) or {}).get("dongle_info", []) or []
    wifi = wan_data.get("wifi_obj_enable", {}) or {}
    meta = raw.get("_meta", {}) or {}
    voip = parse_voice_status(raw.get("get_voice_status", {}))

    total = int(base.get("mem_total") or 0)
    free = int(base.get("mem_free") or 0)
    mem_pct = round((total - free) / total * 100, 1) if total else 0

    devices = []
    for item in lan.get("data", []) or []:
        devices.append({
            "host": item.get("HostName") or "unknown",
            "ip": as_ip(item.get("IPAddress")),
            "mac": item.get("MACAddress", ""),
            "type": item.get("AccessType") or item.get("InterfaceType") or "-",
            "source": item.get("AddressSource", ""),
            "uptime": human_uptime(item.get("ClientUpTime", 0)),
            "rssi": item.get("rssi", ""),
            "active": item.get("Active") == "1",
        })

    ssids = [
        wifi.get(f"ssid{i}") for i in range(1, 17)
        if wifi.get(f"ConfigActive{i}") == "1" and wifi.get(f"ssid{i}")
    ]

    return {
        "meta": meta,
        "profile": meta.get("profile", ""),
        "is_fiberhome": bool(base.get("ModelName") or wan.get("ExternalIPAddress")),
        "generic": raw.get("generic", []),
        "device": {
            "manufacturer": base.get("Manufacturer", ""),
            "model": base.get("ModelName", ""),
            "hardware": base.get("HardwareVersion", ""),
            "software": base.get("SoftwareVersion", ""),
            "serial": base.get("SerialNumber", ""),
            "gpon_sn": base.get("gponsn", ""),
            "oui": base.get("ManufacturerOUI", ""),
            "operator": operator.get("operator_name", ""),
            "operator_code": operator.get("operators_code", ""),
            "base_mac": base.get("brmac", ""),
            "wan_mac": wan.get("MACAddress", ""),
            "os": base.get("os_version", ""),
            "compiled": base.get("compile_time", ""),
        },
        "internet": {
            "public_ip": as_ip(wan.get("ExternalIPAddress")),
            "gateway": as_ip(wan.get("RemoteIPAddress")),
            "dns": as_ip(wan.get("DNSServers")),
            "type": wan.get("ConnectionType", ""),
            "status": wan.get("ConnectionStatus", ""),
            "name": wan.get("Name", ""),
            "vlan": wan.get("vlanid", ""),
            "pppoe_user": wan.get("Username", ""),
            "mtu": wan.get("mtu", ""),
            "nat": "Enabled" if str(wan.get("NATEnabled")) == "1" else "Disabled",
            "uptime": human_uptime(wan.get("Uptime", 0)),
            "uptime_raw": wan.get("Uptime", "0"),
            "error": wan.get("LastConnectionError", ""),
        },
        "voip": voip,
        "call_action": build_call_action(voip["phone_number"]).to_dict(),
        "system": {
            "uptime": human_uptime(base.get("uptime", 0)),
            "uptime_raw": base.get("uptime", "0"),
            "cpu": base.get("cpu_usage", "0"),
            "memory": mem_pct,
            "flash": base.get("flash_usage", "0"),
            "lan_ports": base.get("lan_port_num", ""),
            "uplink": base.get("uplink_mode", ""),
        },
        "optical": {
            "mode": base.get("WANAccessType", ""),
            "tx": base.get("txpower", ""),
            "rx": base.get("rxpower", ""),
            "temperature": base.get("transceivertemperature", ""),
            "current": base.get("biascurrent", ""),
            "voltage": base.get("supplyvottage", ""),
            "onu_id": base.get("onu_id", ""),
            "vendor": base.get("vendor", ""),
            "reg_state": base.get("pon_reg_state", ""),
            "auth": base.get("authStatus", ""),
            "loid": base.get("loid_name", ""),
            "bytes_sent": base.get("ponBytesSent", ""),
            "bytes_recv": base.get("ponBytesReceived", ""),
            "packets_sent": base.get("ponPacketsSent", ""),
            "packets_recv": base.get("ponPacketsReceived", ""),
        },
        "lan_ports": [
            {"port": f"LAN{i}", "status": base.get(f"lan_status_{i}", "-")}
            for i in range(1, int(base.get("lan_port_num") or 4) + 1)
        ],
        "devices": devices,
        "ssids": ssids,
        "dongles": [
            {"index": d.get("dongle_index"), "status": d.get("Status")}
            for d in dongles
        ],
        "ipv6": (raw.get("get_ipv6_lan", {}) or {}).get("lan_ipv6", {}) or {},
    }
