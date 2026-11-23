"""Universal gateway monitor.

Runtime data path: discovery -> ConnectorManager -> ConnectorExecutor ->
UniversalModel -> dashboard. The legacy FiberHome view builder lives in
``legacy_view.py`` only for regression comparison and is never used here.
"""

import threading
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template, request

from connector_executor import (
    AuthenticationError,
    ConnectorExecutor,
    ExecutorError,
    SessionExpired,
)
from connector_manager import ConnectorManager
from gateway import detect_gateway
from models.enums import Availability, Domain

try:
    import speedtest
except ImportError:                                   # pragma: no cover
    speedtest = None

app = Flask(__name__)

# Single-tenant local tool: one active router session at a time.
state = {
    "gateway": detect_gateway(),
    "discovery": None,
    "manager": ConnectorManager(),
    "executor": None,
    "username": None,
    "credentials": None,
    "connected": False,
    "speedtest": {
        "status": "idle",        # idle | starting | running | done | error
        "progress": "",
        "result": None,
        "error": None,
    },
}
_lock = threading.Lock()


CONFIDENCE_RANK = {"unknown": 0, "partial": 1, "exact": 2}
UNSUPPORTED_GATEWAY_MESSAGE = (
    "Unsupported or unidentified gateway. This device does not yet have a "
    "compatible connector profile."
)


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _safe_credential_suggestion(manager, key):
    if not key:
        return {
            "label": "User-supplied credentials",
            "username_source": "user_input",
            "password_source": "user_input",
            "requires_user_confirmation": True,
            "provenance": "No credential values are stored in this repository.",
        }
    suggestion = manager.credential_suggestion(key) or {}
    return {
        "key": key,
        "label": suggestion.get("label", "User-supplied credentials"),
        "username_source": suggestion.get("username_source", "user_input"),
        "password_source": suggestion.get("password_source", "user_input"),
        "requires_user_confirmation": suggestion.get(
            "requires_user_confirmation", True
        ),
        "provenance": suggestion.get(
            "provenance", "No credential values are stored in this repository."
        ),
    }


def _login_defaults(manager, discovery):
    """Build the only login data the browser needs; no connector details."""

    selected = discovery.get("selected_profile")
    if selected:
        profile = manager.get(selected) or {}
        credential_key = ((profile.get("auth") or {}).get(
            "credential_suggestion_key"
        ))
    else:
        credential_key = "generic_user_supplied"
    suggestion = manager.credential_suggestion(credential_key) or {}
    return {
        "gateway": discovery.get("gateway", ""),
        "gateway_detected": bool(discovery.get("gateway_detected")),
        "supported": bool(selected),
        "username": str(suggestion.get("default_username") or ""),
        "password": str(suggestion.get("default_password") or ""),
        "message": None if selected else UNSUPPORTED_GATEWAY_MESSAGE,
    }


def _public_model_payload(model, speed):
    """Serialize the universal model without exposing internal connector state."""

    payload = _apply_speedtest_to_model(model, speed).to_dict()
    payload.pop("profile_id", None)
    payload.pop("compatibility", None)
    return payload


def run_discovery(force=False, host=None):
    """Detect the gateway, run unauthenticated probes, and match a profile."""

    with _lock:
        manager = state["manager"]
        previous = state["discovery"]
        current = state["gateway"]

    requested_host = str(host or "").strip()
    cache_host = requested_host or current.get("gateway")

    if (
        not force
        and previous
        and previous.get("gateway") == cache_host
        and previous.get("detected") == current.get("detected")
    ):
        return previous

    detected = detect_gateway()
    with _lock:
        state["gateway"] = detected
    host = requested_host or str(detected.get("gateway") or "").strip()

    candidates = []
    profile_rows = []
    probe_errors = []
    for profile in manager.enabled_connectors():
        profile_rows.append({
            "id": profile.get("id"),
            "display_name": profile.get("display_name"),
            "status": profile.get("status"),
            "identification_confidence": (
                profile.get("identification") or {}
            ).get("confidence", "unknown"),
            "capabilities": list(profile.get("capabilities") or []),
            "device_compatibility": "not_evaluated",
        })
        try:
            executor = ConnectorExecutor(profile, host, {}, manager=manager)
            probe = executor.discover()
        except ExecutorError as exc:
            probe_errors.append(f"{profile.get('id')}: {exc}")
            continue
        probe_errors.extend(probe.get("errors", []))
        identification = manager.identify(
            probe["observations"], phase="pre_auth", connector_id=profile.get("id")
        )
        if not identification["matches"]:
            continue
        weight = sum(
            int(match.get("weight") or 0)
            for match in (profile.get("identification") or {}).get("rules", [])
            if isinstance(match, dict)
        )
        candidates.append((weight, profile, identification))

    selected = None
    detected_device = None
    confidence = "unknown"
    evidence = (
        "Unknown firmware: no pre-authentication rule matched a known connector profile."
    )
    if candidates:
        weight, profile, identification = max(candidates, key=lambda item: item[0])
        if not identification["ambiguous"]:
            selected = profile.get("id")
            confidence = identification["confidence"]
            match = identification["matches"][0]
            detected_device = str(match.get("value", "")) or None
            evidence = match.get("evidence") or "Matched a profile identification rule."

    credential_key = None
    if selected:
        credential_key = ((manager.get(selected) or {}).get("auth") or {}).get(
            "credential_suggestion_key"
        )

    result = {
        "gateway": host,
        "detected": bool(detected.get("detected")),
        "gateway_detected": bool(detected.get("detected")),
        "detected_device": detected_device,
        "selected_profile": selected,
        "identification_confidence": confidence,
        "evidence": evidence,
        "credential_suggestion": _safe_credential_suggestion(manager, credential_key),
        "profiles": profile_rows,
        "discovery_errors": probe_errors,
        "physical_device_check_required": True,
    }
    with _lock:
        state["discovery"] = result
    return result


# --------------------------------------------------------------------------
# Speedtest worker — local measurement injected into the universal model
# --------------------------------------------------------------------------
SPEEDTEST_TIMEOUT = 120  # seconds — speedtest-cli can hang on some networks
_speed_token = 0


def _speed_worker(token):
    """Run speedtest-cli in the background with a hard timeout."""

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
                        completed_at=_utc_now(),
                    )
                    state["speedtest"]["token"] = None   # invalidate the worker

    threading.Thread(target=watchdog, daemon=True).start()

    if speedtest is None:
        update(status="error", error="speedtest-cli is not installed.", progress="",
               completed_at=_utc_now())
        finished.set()
        return

    try:
        update(status="running", progress="Finding the closest test server…",
               result=None, error=None, started_at=_utc_now())
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
            completed_at=_utc_now(),
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
        update(status="error", progress="", error=str(exc), completed_at=_utc_now())
    finally:
        finished.set()


def _apply_speedtest_to_model(model, speed):
    """Expose local speedtest results through the universal model only."""

    status = speed.get("status", "idle")
    model.set_metric(
        Domain.SPEED_TEST, "speed_test.status", value=status,
        availability=Availability.AVAILABLE, source="local_speedtest",
    )
    result = speed.get("result") if status == "done" else None
    metric_map = {
        "download": "speed_test.download_mbps",
        "upload": "speed_test.upload_mbps",
        "ping": "speed_test.ping_ms",
        "jitter": "speed_test.jitter_ms",
        "server": "speed_test.server",
        "sponsor": "speed_test.sponsor",
        "country": "speed_test.country",
    }
    for source_key, metric_id in metric_map.items():
        if result and result.get(source_key) not in (None, ""):
            model.set_metric(
                Domain.SPEED_TEST, metric_id, value=result[source_key],
                availability=Availability.AVAILABLE, source="local_speedtest",
            )
        else:
            model.set_metric(
                Domain.SPEED_TEST, metric_id, value=None,
                availability=(Availability.ERROR if speed.get("error") and status == "error"
                              else Availability.NOT_LOADED),
                source="local_speedtest", error=speed.get("error"),
            )
    for timestamp_id, key in (
        ("speed_test.started_at", "started_at"),
        ("speed_test.completed_at", "completed_at"),
    ):
        if speed.get(key):
            model.set_metric(
                Domain.SPEED_TEST, timestamp_id, value=speed[key],
                availability=Availability.AVAILABLE, source="local_speedtest",
            )
    if status == "error" and speed.get("error"):
        model.set_metric(
            Domain.SPEED_TEST, "speed_test.error", value=None,
            availability=Availability.ERROR, source="local_speedtest",
            error=speed["error"],
        )
    return model


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


@app.route("/api/login-defaults")
def api_login_defaults():
    """Return only the fields needed by the simple login form.

    Identification runs internally here. It performs unauthenticated discovery
    only and never calls the selected connector's authentication flow.
    """

    discovery = run_discovery()
    return jsonify(_login_defaults(state["manager"], discovery))


@app.route("/api/discovery")
def api_discovery():
    return jsonify(run_discovery())


@app.route("/api/session")
def api_session():
    with _lock:
        executor = state["executor"]
        return jsonify({
            "connected": state["connected"] and executor is not None,
            "gateway": state["gateway"].get("gateway"),
        })


@app.route("/api/connect", methods=["POST"])
def api_connect():
    payload = request.get_json(silent=True) or {}
    username = str(payload.get("username") or "").strip()
    password = payload.get("password")
    requested_host = str(payload.get("gateway") or "").strip()

    if not username:
        return jsonify({"ok": False, "message": "Enter the gateway username."}), 400
    if password is None:
        return jsonify({"ok": False, "message": "Enter the gateway password."}), 400

    with _lock:
        manager = state["manager"]

    discovery = run_discovery(force=bool(requested_host), host=requested_host or None)
    profile_id = discovery.get("selected_profile")
    host = discovery.get("gateway") or requested_host
    profile = manager.get(profile_id) if profile_id else None
    if profile is None or not profile.get("enabled", True):
        return jsonify({
            "ok": False,
            "message": UNSUPPORTED_GATEWAY_MESSAGE,
        }), 400

    credential_key = ((profile.get("auth") or {}).get("credential_suggestion_key"))
    suggestion = manager.credential_suggestion(credential_key) or {}
    default_password = str(suggestion.get("default_password") or "")
    passwords = [str(password)]
    if default_password and passwords[0] == default_password:
        for candidate in suggestion.get("fallback_passwords") or []:
            candidate = str(candidate)
            if candidate and candidate not in passwords:
                passwords.append(candidate)

    executor = None
    for candidate in passwords:
        attempt = ConnectorExecutor(
            profile, host,
            credentials={"username": username, "password": candidate},
            manager=manager,
        )
        try:
            attempt.connect()
        except (AuthenticationError, ExecutorError):
            continue
        executor = attempt
        break

    if executor is None:
        return jsonify({
            "ok": False,
            "message": "Authentication failed. Check the username and password.",
        }), 401

    with _lock:
        state["executor"] = executor
        state["username"] = username
        state["credentials"] = {"username": username, "profile_id": profile_id}
        state["connected"] = True
        state["gateway"] = {
            "gateway": host,
            "model": discovery.get("detected_device") or "",
            "detected": True,
        }
    return jsonify({
        "ok": True,
        "message": "Connected.",
        "gateway": host,
    })


@app.route("/api/data")
def api_data():
    with _lock:
        executor = state["executor"]
        connected = state["connected"]
        speed = dict(state["speedtest"])
    if not connected or executor is None:
        return jsonify({"error": "Not connected"}), 401
    try:
        model = executor.collect()
    except SessionExpired:
        with _lock:
            state["connected"] = False
            state["executor"] = None
            state["credentials"] = None
        return jsonify({"error": "Router session expired. Please reconnect."}), 401
    except (AuthenticationError, ExecutorError) as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify(_public_model_payload(model, speed))


@app.route("/api/disconnect", methods=["POST"])
def api_disconnect():
    with _lock:
        executor = state["executor"]
        state["executor"] = None
        state["credentials"] = None
        state["username"] = None
        state["connected"] = False

    router_logout = True
    if executor is not None:
        try:
            router_logout = executor.logout()
        except ExecutorError:                            # pragma: no cover
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
            "result": None, "error": None,
            "started_at": _utc_now(), "completed_at": None,
            "token": token,
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
    print("Homerouter universal gateway monitor → http://0.0.0.0:7000")
    app.run(host="0.0.0.0", port=7000, debug=False, threaded=True)
