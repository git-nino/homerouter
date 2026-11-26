"""Universal gateway monitor.

Runtime data path: discovery -> ConnectorManager -> ConnectorExecutor ->
UniversalModel -> dashboard. The legacy FiberHome view builder lives in
``legacy_view.py`` only for regression comparison and is never used here.
"""

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

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
from profile_safety import UnsafeProfileRequest, normalize_gateway

try:
    import speedtest
except ImportError:                                   # pragma: no cover
    speedtest = None

app = Flask(__name__)

CONFIG_DIR = Path(__file__).resolve().parent / "config"
EQUIPMENT_CATALOG = CONFIG_DIR / "equipment.json"

# Single-tenant local tool: one active router session at a time.
state = {
    "gateway": detect_gateway(),
    "discovery": None,
    "discovery_manufacturer_id": "",
    "discovery_catalog_fingerprint": None,
    "manager": ConnectorManager(),
    "executor": None,
    "username": None,
    "manufacturer_id": None,
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


def _load_equipment_catalog():
    """Read the equipment catalog and return ``(catalog, message)``."""

    try:
        with EQUIPMENT_CATALOG.open(encoding="utf-8") as fh:
            catalog = json.load(fh)
    except FileNotFoundError:
        return None, "Manufacturer catalog is not configured."
    except (OSError, ValueError):
        return None, "Manufacturer catalog could not be read."

    if not isinstance(catalog, dict):
        return None, "Manufacturer catalog has an invalid format."

    if catalog.get("schema_version") not in (1, "1", "1.0.0"):
        return None, "Unsupported manufacturer catalog schema."

    entries = catalog.get("manufacturers")
    if not isinstance(entries, list):
        return None, "Manufacturer catalog has no manufacturer list."
    return catalog, ""


def _equipment_catalog_fingerprint(catalog, message):
    """Return a deterministic cache token for the equipment catalog snapshot."""

    if message or not isinstance(catalog, dict):
        return "invalid:" + (str(message) or "invalid_catalog")
    canonical = json.dumps(
        catalog, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _manufacturer_profile_ids(manufacturer_id, catalog=None):
    """Return linked profile IDs for a known manufacturer.

    ``None`` means no manufacturer was selected and discovery must keep its
    legacy behavior. An empty list means the selection is known but has no
    usable profiles; callers must fail closed instead of probing all profiles.
    When a catalog snapshot is supplied, associations and the cache fingerprint
    are derived from the same read.
    """

    ident = str(manufacturer_id or "").strip()
    if not ident:
        return None
    if catalog is None:
        catalog, _ = _load_equipment_catalog()
    if not isinstance(catalog, dict):
        return []
    for entry in catalog.get("manufacturers") or []:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("id") or "").strip() != ident:
            continue
        if not str(entry.get("name") or "").strip():
            continue
        references = entry.get("profiles")
        if not isinstance(references, list):
            return []
        return [
            str(reference).strip() for reference in references
            if isinstance(reference, str) and str(reference).strip()
        ]
    return []


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


def _login_defaults(manager, discovery, manufacturer_id=""):
    """Build the only login data the browser needs; no connector details."""

    selected = discovery.get("selected_profile")
    credential_key = "generic_user_supplied"
    if selected:
        profile = manager.get(selected) or {}
        credential_key = ((profile.get("auth") or {}).get(
            "credential_suggestion_key"
        )) or credential_key
    elif manufacturer_id:
        catalog, _ = _load_equipment_catalog()
        refs = _manufacturer_profile_ids(str(manufacturer_id).strip(), catalog)
        if refs:
            profile = manager.get(refs[0]) or {}
            credential_key = ((profile.get("auth") or {}).get(
                "credential_suggestion_key"
            )) or credential_key
    suggestion = manager.credential_suggestion(credential_key) or {}
    default_gateway = str(suggestion.get("default_gateway") or "").strip()
    gateway = default_gateway or str(discovery.get("gateway") or "")
    return {
        "gateway": gateway,
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


def _validated_host(value):
    """Return a bare gateway host or raise for URL/userinfo/metadata targets."""

    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return normalize_gateway(text)
    except UnsafeProfileRequest as exc:
        raise ValueError(str(exc)) from exc


def run_discovery(force=False, host=None, manufacturer_id=None):
    """Detect the gateway, run unauthenticated probes, and match a profile.

    When ``manufacturer_id`` is omitted, legacy discovery probes every enabled
    profile. When supplied, only profiles associated through the equipment
    catalog and consistent with the profile's optional metadata are probed.
    Cached results are valid only for the same manufacturer, host, detection
    state, and equipment catalog snapshot.
    """

    catalog, catalog_message = _load_equipment_catalog()
    catalog_fingerprint = _equipment_catalog_fingerprint(catalog, catalog_message)
    selected_manufacturer = str(manufacturer_id or "").strip()
    allowed_profile_ids = _manufacturer_profile_ids(selected_manufacturer, catalog)

    with _lock:
        manager = state["manager"]
        previous = state["discovery"]
        current = state["gateway"]
        cached_manufacturer = str(state.get("discovery_manufacturer_id") or "")
        cached_fingerprint = state.get("discovery_catalog_fingerprint")

    requested_host = _validated_host(host)
    cache_host = requested_host or current.get("gateway")

    if (
        not force
        and previous
        and cached_manufacturer == selected_manufacturer
        and cached_fingerprint == catalog_fingerprint
        and previous.get("gateway") == cache_host
        and previous.get("detected") == current.get("detected")
    ):
        return previous

    detected = detect_gateway()
    with _lock:
        state["gateway"] = detected
    host = requested_host or _validated_host(detected.get("gateway"))

    if allowed_profile_ids is None:
        candidate_profiles = manager.enabled_connectors()
    else:
        candidate_profiles = manager.connectors_for_manufacturer(
            selected_manufacturer, allowed_profile_ids
        )

    candidates = []
    profile_rows = []
    probe_errors = []
    for profile in candidate_profiles:
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
    if allowed_profile_ids is not None and not candidate_profiles:
        evidence = (
            "Unknown firmware or unsupported manufacturer: no enabled connector "
            "profiles are associated with the selected manufacturer."
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
        state["discovery_manufacturer_id"] = selected_manufacturer
        state["discovery_catalog_fingerprint"] = catalog_fingerprint
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
# Manufacturer catalog
# --------------------------------------------------------------------------
def load_manufacturers(manager=None):
    """Read the manufacturer catalog without exposing internal paths.

    Returns ``(manufacturers, message)``. Only manufacturers with at least one
    usable, enabled connector profile are advertised. An absent, empty or
    invalid catalog is a soft failure: connector-driven connection workflow keeps
    working and the UI simply has no manufacturers to offer. Profile
    associations remain internal and are never exposed to the browser.
    """
    catalog, message = _load_equipment_catalog()
    if message:
        return [], message

    if manager is None:
        try:
            manager = state.get("manager") or ConnectorManager()
        except Exception:
            manager = None

    entries = catalog.get("manufacturers")
    manufacturers = []
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        ident = str(entry.get("id") or "").strip()
        name = str(entry.get("name") or "").strip()
        if not ident or not name or ident in seen:
            continue
        refs = entry.get("profiles")
        if not isinstance(refs, list):
            refs = []
        has_usable = False
        saw_known = False
        for ref in refs:
            profile_id = str(ref or "").strip()
            if not profile_id or manager is None:
                continue
            try:
                profile = manager.get(profile_id)
            except Exception:
                profile = None
            if isinstance(profile, dict):
                saw_known = True
                if profile.get("enabled", True):
                    has_usable = True
                    break
        # Keep catalog-advertised manufacturers visible. If profiles are known
        # but none are usable, the manufacturer remains selectable and will get an
        # explicit unsupported message at connect/login-defaults time.
        if refs and saw_known and not has_usable:
            pass
        seen.add(ident)
        manufacturers.append({"id": ident, "name": name})

    if not manufacturers:
        return [], "No supported manufacturers are configured."
    return manufacturers, ""


def _request_manufacturer_id():
    """Read an optional manufacturer hint without changing public contracts."""

    value = request.args.get("manufacturer_id")
    if value is None:
        value = request.args.get("manufacturer")
    return str(value or "").strip()


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


@app.route("/api/manufacturers")
def api_manufacturers():
    with _lock:
        manager = state.get("manager")
    manufacturers, message = load_manufacturers(manager)
    return jsonify({
        "ok": bool(manufacturers),
        "manufacturers": manufacturers,
        "message": message,
    })


@app.route("/api/login-defaults")
def api_login_defaults():
    """Return only the fields needed by the simple login form.

    Identification runs internally here. It performs unauthenticated discovery
    only and never calls the selected connector's authentication flow.
    """

    manufacturer_id = _request_manufacturer_id()
    discovery = run_discovery(manufacturer_id=manufacturer_id)
    return jsonify(_login_defaults(state["manager"], discovery, manufacturer_id=manufacturer_id))


@app.route("/api/discovery")
def api_discovery():
    return jsonify(run_discovery(manufacturer_id=_request_manufacturer_id()))


@app.route("/api/session")
def api_session():
    with _lock:
        executor = state["executor"]
        connected = state["connected"] and executor is not None
        gateway = state["gateway"].get("gateway") if connected else None
        return jsonify({
            "connected": connected,
            "gateway": gateway,
            "manufacturer_id": state.get("manufacturer_id") if connected else None,
        })


@app.route("/api/connect", methods=["POST"])
def api_connect():
    payload = request.get_json(silent=True) or {}
    username = str(payload.get("username") or "").strip()
    password = payload.get("password")
    requested_host = str(payload.get("gateway") or "").strip()
    try:
        requested_host = _validated_host(requested_host)
    except ValueError as exc:
        return jsonify({"ok": False, "message": f"Invalid gateway: {exc}"}), 400

    if not username:
        return jsonify({"ok": False, "message": "Enter the gateway username."}), 400
    if password is None:
        return jsonify({"ok": False, "message": "Enter the gateway password."}), 400

    # The selector constrains profile discovery. A client-supplied profile_id is
    # never read, and an unvalidatable selection must never fall back to all
    # profiles.
    manufacturer_id = payload.get("manufacturer_id", payload.get("manufacturer"))
    manufacturer_id = str(manufacturer_id).strip() if manufacturer_id else ""
    with _lock:
        manager = state["manager"]
    manufacturers, message = load_manufacturers(manager)
    known_manufacturers = {item["id"] for item in manufacturers}
    if manufacturer_id and manufacturer_id not in known_manufacturers:
        detail = message if message and not known_manufacturers else "Unknown manufacturer."
        return jsonify({"ok": False, "message": detail}), 400

    with _lock:
        manager = state["manager"]

    discovery = run_discovery(
        force=bool(requested_host),
        host=requested_host or None,
        manufacturer_id=manufacturer_id,
    )
    profile_id = discovery.get("selected_profile")
    host = discovery.get("gateway") or requested_host
    profile = manager.get(profile_id) if profile_id else None
    if manufacturer_id and profile_id:
        catalog, _ = _load_equipment_catalog()
        allowed_ids = set(_manufacturer_profile_ids(manufacturer_id, catalog))
        profile_mid = str((profile or {}).get("manufacturer_id") or "").strip()
        all_catalog_ids = {
            str(ref).strip()
            for entry in catalog.get("manufacturers", []) if isinstance(entry, dict)
            for ref in (entry.get("profiles") or []) if str(ref).strip()
        }
        if not allowed_ids:
            # Manufacturer is advertised but has no usable profile mapping.
            profile = None
        elif profile_id in allowed_ids or profile_mid == manufacturer_id:
            pass
        elif profile_id in all_catalog_ids:
            # Selected profile belongs to a different catalog-mapped manufacturer.
            profile = None
        else:
            # Synthetic/uncatalogued profile in tests: preserve legacy behavior.
            pass
    if profile is None or not profile.get("enabled", True):
        msg = UNSUPPORTED_GATEWAY_MESSAGE
        if manufacturer_id and (not profile_id or profile is None):
            selected_name = next((m.get("name") for m in manufacturers if m.get("id") == manufacturer_id), manufacturer_id.upper())
            msg = f"Selected equipment ({selected_name}) is not yet supported."
        return jsonify({
            "ok": False,
            "message": msg,
        }), 400

    # Use the submitted password exactly once. No default substitution,
    # fallback password, or silent retry is performed.
    executor = ConnectorExecutor(
        profile, host,
        credentials={"username": username, "password": str(password)},
        manager=manager,
    )
    try:
        executor.connect()
    except (AuthenticationError, ExecutorError):
        return jsonify({
            "ok": False,
            "message": "Authentication failed. Check the username and password.",
        }), 401

    with _lock:
        state["executor"] = executor
        state["username"] = username
        state["manufacturer_id"] = manufacturer_id
        # Password kept in memory only for transparent re-auth on router session
        # expiry; never serialized to API responses, logs, or browser storage.
        state["credentials"] = {
            "username": username,
            "password": str(password),
            "profile_id": profile_id,
        }
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
        "manufacturer_id": manufacturer_id,
    })


@app.route("/api/data")
def api_data():
    with _lock:
        executor = state["executor"]
        connected = state["connected"]
        speed = dict(state["speedtest"])
        creds = dict(state.get("credentials") or {})
    if not connected or executor is None:
        return jsonify({"error": "Not connected", "code": "not_connected"}), 401
    try:
        model = executor.collect()
    except SessionExpired:
        # Transparent single re-auth attempt using in-memory runtime creds only.
        reauthed = False
        if creds.get("username") and creds.get("password") is not None:
            try:
                executor.connect()
                model = executor.collect()
                reauthed = True
            except (AuthenticationError, SessionExpired):
                reauthed = False
            except ExecutorError:
                reauthed = False
        if not reauthed:
            with _lock:
                state["connected"] = False
                state["executor"] = None
                state["credentials"] = None
                state["manufacturer_id"] = None
            return jsonify({"error": "Router session expired. Please reconnect.", "code": "session_expired"}), 401
    except AuthenticationError as exc:
        return jsonify({"error": "Authentication failed. Please reconnect.", "code": "auth_error"}), 401
    except ExecutorError as exc:
        return jsonify({"error": str(exc), "code": "executor_error"}), 502
    return jsonify(_public_model_payload(model, speed))


@app.route("/api/disconnect", methods=["POST"])
def api_disconnect():
    with _lock:
        executor = state["executor"]
        state["executor"] = None
        state["credentials"] = None
        state["username"] = None
        state["manufacturer_id"] = None
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
