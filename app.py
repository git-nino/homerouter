"""Netdata-style FiberHome gateway monitor.

Run with:  python app.py   (listens on 0.0.0.0:7000)
"""

import threading

from flask import Flask, jsonify, render_template, request

from gateway import RouterClient, as_ip, detect_gateway, human_uptime, parse_voice_status
from models.phone import build_call_action

try:
    import speedtest
except ImportError:                                   # pragma: no cover
    speedtest = None

app = Flask(__name__)

# Single-tenant local tool: one active router session at a time.
state = {
    "gateway": detect_gateway(),
    "client": None,
    "username": "user",
    "credentials": None,
    "connected": False,
    "speedtest": {
        "status": "idle",        # idle | running | done | error
        "progress": "",
        "result": None,
        "error": None,
    },
}
_lock = threading.Lock()


# --------------------------------------------------------------------------
# View helpers
# --------------------------------------------------------------------------
def build_view(raw):
    """Turn raw router JSON into display-ready values."""
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


# --------------------------------------------------------------------------
# Speedtest worker
# --------------------------------------------------------------------------
SPEEDTEST_TIMEOUT = 120  # seconds — speedtest-cli can hang on some networks
_speed_token = 0


def _speed_worker(token):
    """Run speedtest-cli in the background with a hard timeout.

    The token guards against a stale worker overwriting a newer test result.
    """
    finished = threading.Event()

    def update(**kwargs):
        with _lock:
            if state["speedtest"].get("token") != token:
                return                                  # cancelled / superseded
            state["speedtest"].update(kwargs)

    def watchdog():
        if not finished.wait(SPEEDTEST_TIMEOUT):
            with _lock:
                if state["speedtest"].get("token") == token:
                    state["speedtest"].update(
                        status="error", progress="", result=None,
                        error=(f"Speedtest timed out after {SPEEDTEST_TIMEOUT}s. "
                               "Check your internet connection and try again."),
                    )
                    state["speedtest"]["token"] = None   # invalidate the worker

    threading.Thread(target=watchdog, daemon=True).start()

    if speedtest is None:
        update(status="error", error="speedtest-cli is not installed.", progress="")
        finished.set()
        return

    try:
        update(status="running", progress="Finding the closest test server…",
               result=None, error=None)
        client = speedtest.Speedtest()
        client.get_best_server()

        update(progress="Measuring download speed…")
        down = client.download() / 1_000_000

        update(progress="Measuring upload speed…")
        up = client.upload() / 1_000_000

        results = client.results
        server = results.server or {}
        update(
            status="done",
            progress="",
            result={
                "download": round(down, 2),
                "upload": round(up, 2),
                "ping": round(getattr(results, "ping", 0), 1),
                "jitter": round(getattr(results, "jitter", 0) or 0, 1),
                "server": server.get("name", ""),
                "sponsor": server.get("sponsor", ""),
                "country": server.get("country", ""),
            },
        )
    except Exception as exc:                            # pragma: no cover
        update(status="error", progress="", error=str(exc))
    finally:
        finished.set()


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/gateway")
def api_gateway():
    with _lock:
        return jsonify(state["gateway"])


@app.route("/api/session")
def api_session():
    with _lock:
        client = state["client"]
        return jsonify({
            "connected": state["connected"] and client is not None,
            "gateway": state["gateway"].get("gateway"),
            "profile": getattr(client, "profile", None) if client else None,
        })


@app.route("/api/connect", methods=["POST"])
def api_connect():
    payload = request.get_json(silent=True) or {}
    username = (payload.get("username") or "user").strip()
    password = payload.get("password") or "user1234"
    mode = (payload.get("mode") or "auto").strip()
    host = (payload.get("gateway") or "").strip()

    if not host:
        with _lock:
            host = state["gateway"]["gateway"]
        detected = detect_gateway()
        if detected["detected"]:
            host = detected["gateway"]
            with _lock:
                state["gateway"] = detected

    client = RouterClient(host, username=username, password=password, mode=mode)
    ok, message = client.login()

    # Convenience for the verified default on FiberHome models when the user
    # simply pressed Enter with the pre-filled short password.
    if not ok and mode == "auto" and password == "user1234":
        ok, _ = client.login(password="user123456")
        if ok:
            message = "Connected (using verified default password user123456)."

    if not ok:
        return jsonify({"ok": False, "message": message}), 401

    with _lock:
        state["client"] = client
        state["username"] = username
        state["credentials"] = {
            "username": username, "password": client.password, "mode": mode,
        }
        state["connected"] = True
        state["gateway"] = {
            "gateway": host,
            "model": (client.get("get_device_name") or {}).get("ModelName", ""),
            "detected": True,
        }
    return jsonify({"ok": True, "message": message, "gateway": host})


@app.route("/api/data")
def api_data():
    with _lock:
        client = state["client"]
        connected = state["connected"]
    if not connected or client is None:
        return jsonify({"error": "Not connected"}), 401
    try:
        raw = client.collect()
        if not raw.get("_meta", {}).get("session_valid"):
            # Router sessions expire after a few minutes — reconnect silently.
            ok, _ = client.login()
            if ok:
                raw = client.collect()
        if not raw.get("_meta", {}).get("session_valid"):
            with _lock:
                state["connected"] = False
            return jsonify({"error": "Router session expired. Please reconnect."}), 401
    except Exception as exc:                            # pragma: no cover
        return jsonify({"error": str(exc)}), 502
    return jsonify(build_view(raw))


@app.route("/api/disconnect", methods=["POST"])
def api_disconnect():
    with _lock:
        client = state["client"]
        state["client"] = None
        state["credentials"] = None
        state["connected"] = False

    router_logout = True
    if client is not None:
        try:
            router_logout, _ = client.logout()
        except Exception:                                # pragma: no cover
            router_logout = False

    return jsonify({
        "ok": True,
        "router_logout": router_logout,
        "message": (
            "Disconnected."
            if router_logout
            else "Disconnected locally; router session could not be ended."
        ),
    })


@app.route("/api/speedtest", methods=["POST"])
def api_speedtest_start():
    global _speed_token
    with _lock:
        if state["speedtest"]["status"] in ("starting", "running"):
            return jsonify({"ok": False, "message": "A test is already running."})
        _speed_token += 1
        token = _speed_token
        state["speedtest"] = {
            "status": "starting", "progress": "Starting…",
            "result": None, "error": None, "token": token,
        }
    threading.Thread(target=_speed_worker, args=(token,), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/speedtest/status")
def api_speedtest_status():
    with _lock:
        data = dict(state["speedtest"])
    data.pop("token", None)
    return jsonify(data)


if __name__ == "__main__":
    print("FiberHome Gateway Monitor → http://0.0.0.0:7000")
    app.run(host="0.0.0.0", port=7000, debug=False, threaded=True)
