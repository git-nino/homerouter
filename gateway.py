"""Gateway detection and multi-router API client.

Supports three authentication strategies (selectable in the UI):
  * fiberhome  - FiberHome ONU AES challenge/response (HG6145F1 and family)
  * plain      - plain-text credentials to common router login endpoints
  * basic      - HTTP Basic authentication
"auto" tries them in that order and keeps the first one that works.
"""

import binascii
import json
import random
import re
import socket
import struct
import subprocess

import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from requests.auth import HTTPBasicAuth

VOIP_STATUS_CODES = {
    "0": "INACTIVE",
    "1": "REGISTERING",
    "2": "IDLE",
    "3": "OFF_HOOK",
    "4": "DIALING",
    "5": "RING",
    "6": "RINGBACK",
    "7": "CONNECTING",
    "8": "CONNECTED",
    "9": "ON_HOOK",
    "10": "DISCONNECTING",
    "11": "BUSY",
    "12": "REG_FAIL",
    "13": "LINE_FAIL",
    "14": "NOT_FOUND",
    "15": "AUTH",
    "16": "NO_RESPONSE",
    "17": "IP_CONFIG",
}

COMMON_GATEWAYS = [
    "192.168.1.1", "192.168.0.1", "192.168.2.1", "192.168.100.1",
    "10.0.0.1", "10.1.1.1", "172.16.0.1", "192.168.10.1",
]

# Endpoints tried when the router is not a FiberHome ONU.
GENERIC_ENDPOINTS = [
    "get_status", "status", "get_system_info", "get_device_info",
    "get_wan_info", "get_lan_info", "get_wan_status", "get_lan_status",
    "get_net_info", "get_network_status", "get_interface_status",
]

# Common plain-login (endpoint, fields) combinations.
PLAIN_LOGINS = [
    ("/cgi-bin/ajax", ("username", "password", "port", "sessionid")),
    ("/cgi-bin/ajax", ("username", "loginpd", "port", "sessionid")),
    ("/login", ("username", "password")),
    ("/login", ("user", "pass")),
    ("/cgi-bin/login", ("username", "password")),
    ("/goform/login", ("username", "password")),
]


# --------------------------------------------------------------------------
# Gateway detection
# --------------------------------------------------------------------------
def default_gateway():
    """Return the IPv4 address of the default route, if present."""
    try:
        with open("/proc/net/route", encoding="utf-8") as fh:
            for line in list(fh)[1:]:
                parts = line.split()
                if len(parts) > 2 and parts[1] == "00000000":
                    return socket.inet_ntoa(struct.pack("<L", int(parts[2], 16)))
    except OSError:
        pass
    try:
        out = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True, text=True, timeout=3,
        ).stdout
        match = re.search(r"via\s+(\d+\.\d+\.\d+\.\d+)", out)
        if match:
            return match.group(1)
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def subnet_candidates():
    """Guess the gateway address of each local IPv4 subnet (first usable IP)."""
    guesses = []
    try:
        out = subprocess.run(
            ["ip", "-4", "-o", "addr", "show"],
            capture_output=True, text=True, timeout=3,
        ).stdout
        for line in out.splitlines():
            match = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)", line)
            if not match:
                continue
            ip, prefix = match.group(1), int(match.group(2))
            if prefix < 8 or prefix > 30 or ip.startswith("127."):
                continue
            octets = [int(x) for x in ip.split(".")]
            bits = prefix
            network = []
            for octet in octets:
                if bits >= 8:
                    network.append(octet)
                    bits -= 8
                elif bits > 0:
                    network.append(octet & (0xFF << (8 - bits) & 0xFF))
                    bits = 0
                else:
                    network.append(0)
            network[3] |= 1
            guesses.append(".".join(str(x) for x in network))
    except (OSError, subprocess.SubprocessError):
        pass
    return guesses


def probe(host, timeout=1.2):
    """Check whether *host* looks like a router's web interface."""
    session = requests.Session()
    checks = [
        ("GET", f"http://{host}/cgi-bin/ajax",
         {"ajaxmethod": "get_device_name"}, None),
        ("GET", f"http://{host}/", None, None),
    ]
    for method, url, params, data in checks:
        try:
            response = session.request(
                method, url, params=params, data=data, timeout=timeout,
                allow_redirects=True,
            )
            if response.status_code != 200:
                continue
            payload = _parse_response(response)
            model = payload.get("ModelName", "")
            text = response.text.lower()
            looks_like_router = any(word in text for word in (
                "router", "gateway", "gpon", "onu", "wan", "password",
                "modelname", "login", "administration",
            ))
            if model or looks_like_router:
                return True, model
        except requests.RequestException:
            continue
    return False, ""


def detect_gateway():
    """Probe likely addresses and return the first real gateway found."""
    candidates = []
    router = default_gateway()
    if router:
        candidates.append(router)
    candidates.extend(subnet_candidates())
    candidates.extend(COMMON_GATEWAYS)

    seen = set()
    for host in candidates:
        if host in seen:
            continue
        seen.add(host)
        ok, model = probe(host)
        if ok:
            return {"gateway": host, "model": model, "detected": True}
    return {"gateway": router or COMMON_GATEWAYS[0], "model": "", "detected": False}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _parse_response(response):
    text = (response.text or "").strip()
    if "\n\n" in text:                 # some firmware prepends a Content-type line
        text = text.split("\n\n", 1)[1].strip()
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return {}


def fhencrypt(password, seed):
    """Replicate FiberHome's fhencrypt(): AES-128-CBC, key=IV, uppercase hex."""
    key = (seed[6:][:16] or "6HTT4VDg3jgkkLVL").encode("utf-8")
    key = key.ljust(16, b"\0")[:16]
    data = password.encode("utf-8")
    padding = 16 - (len(data) % 16)
    data += bytes([padding]) * padding
    encryptor = Cipher(algorithms.AES(key), modes.CBC(key)).encryptor()
    return binascii.hexlify(encryptor.update(data) + encryptor.finalize()).decode().upper()


def as_ip(value):
    """Some firmware encodes dots as '_point_' in JSON values."""
    return str(value).replace("_point_", ".") if value else ""


def human_uptime(seconds):
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return "-"
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours >= 24:
        days, hours = divmod(hours, 24)
        return f"{days}d {hours}h {minutes}m"
    return f"{hours}h {minutes}m {secs}s"


def parse_voice_status(raw):
    """Map the observed voice_status payload to display-ready VoIP fields.

    The source mapping comes from the router's own voice_info_inter.js:
    ServerType 2 uses URI<n>; other server types use DirectoryNumber<n> with
    AuthUserName<n> (before '@') as fallback. RegAndCallStatus<n> is the
    registration/call state code shown by the router UI.
    """

    voice = (raw or {}).get("voice_status", {}) or {}
    try:
        ports = int(voice.get("voice_port_num") or 0)
    except (TypeError, ValueError):
        ports = 0

    result = {
        "phone_number": "",
        "registration_code": "",
        "registration_state": "",
        "availability": "unsupported",
        "source": "get_voice_status",
    }
    if ports < 1:
        return result

    server_type = str(voice.get("ServerType", ""))
    number = ""
    if server_type == "2":
        number = voice.get("URI1", "") or ""
    else:
        number = voice.get("DirectoryNumber1", "") or ""
        if not number:
            number = str(voice.get("AuthUserName1", "") or "").split("@")[0]

    code = str(voice.get("RegAndCallStatus1", "") or "")
    result["phone_number"] = str(number or "")
    result["registration_code"] = code
    result["registration_state"] = VOIP_STATUS_CODES.get(code, "unknown" if code else "")
    result["availability"] = "available" if number else "unavailable"
    return result


def _looks_successful(response):
    """Heuristic for a successful plain/basic login response."""
    payload = _parse_response(response)
    for key in ("login_result", "result", "code", "status"):
        if key in payload and str(payload[key]) in ("0", "1", "true", "success"):
            return True
    for key in ("success", "ok", "logged_in"):
        if payload.get(key) in (True, 1, "1", "true"):
            return True
    text = (response.text or "").lower()
    return any(token in text for token in ('"success"', 'login successful', 'logged in'))


# --------------------------------------------------------------------------
# Router client
# --------------------------------------------------------------------------
class RouterClient:
    def __init__(self, host, username="user", password="", mode="auto"):
        self.host = host
        self.username = username
        self.password = password
        self.mode = mode
        self.base = f"http://{host}/cgi-bin/ajax"
        self.root = f"http://{host}"
        self.http = requests.Session()
        self.http.headers.update({"User-Agent": "Mozilla/5.0 (FH-Monitor)"})
        self.sessionid = None
        self.logged_in = False
        self.profile = None
        self.model = ""

    # ---------- generic request helpers ----------
    def get(self, method):
        try:
            response = self.http.get(
                self.base,
                params={"ajaxmethod": method, "_": random.random()},
                timeout=6,
            )
            return _parse_response(response)
        except requests.RequestException:
            return {}

    def _post(self, path, data):
        try:
            return self.http.post(self.root + path, data=data, timeout=8,
                                  allow_redirects=True)
        except requests.RequestException:
            return None

    # ---------- authentication strategies ----------
    def _login_fiberhome(self):
        seed_data = self.get("get_acs_random")
        seed = seed_data.get("acsRandom", "")
        self.sessionid = seed_data.get("sessionid") or self.sessionid
        if not seed:
            return False, "No FiberHome encryption seed found on this router."

        try:
            encrypted = fhencrypt(self.password, seed)
        except Exception as exc:                       # pragma: no cover
            return False, f"Encryption error: {exc}"

        response = self._post("/cgi-bin/ajax", {
            "ajaxmethod": "do_login",
            "username": self.username,
            "loginpd": encrypted,
            "port": 0,
            "sessionid": self.sessionid or "",
        })
        if response is None:
            return False, "Connection error during login."
        result = _parse_response(response).get("login_result")
        if result in (0, 1):
            self.logged_in = True
            return True, "Logged in (FiberHome AES)."
        if result == 4:
            return False, "Invalid FiberHome username or password (code 4)."
        return False, f"FiberHome login rejected (code {result})."

    def _login_plain(self):
        errors = []
        for path, fields in PLAIN_LOGINS:
            data = {}
            for field in fields:
                if field in ("username", "user"):
                    data[field] = self.username
                elif field in ("password", "pass", "loginpd"):
                    data[field] = self.password
                elif field == "port":
                    data[field] = 0
                elif field == "sessionid":
                    data[field] = self.sessionid or ""
            if path.endswith("ajax"):
                data["ajaxmethod"] = "do_login"
            response = self._post(path, data)
            if response is not None and _looks_successful(response):
                self.logged_in = True
                return True, f"Logged in (plain form: {path})."
            errors.append(path)
        return False, "Plain login rejected on: " + ", ".join(errors)

    def _login_basic(self):
        self.http.auth = HTTPBasicAuth(self.username, self.password)
        try:
            response = self.http.get(self.root + "/", timeout=8,
                                     allow_redirects=True)
            text = (response.text or "").lower()
            is_login_form = (
                'type="password"' in text
                or "name=\"password\"" in text
                or ("<form" in text and "login" in text)
            )
            if response.status_code == 200 and not is_login_form:
                self.logged_in = True
                return True, "Logged in (HTTP Basic)."
        except requests.RequestException as exc:
            return False, f"HTTP Basic error: {exc}"
        self.http.auth = None
        return False, "HTTP Basic authentication failed."

    def _has_data_access(self):
        """Confirm the session can actually read data (guards Basic false positives)."""
        payload = self.get("get_base_info")
        if payload:
            return payload.get("session_valid", 1) != 0
        return True        # generic router with no FiberHome-style endpoint

    def login(self, password=None):
        if password is not None:
            self.password = password

        strategies = []
        if self.mode in ("auto", "fiberhome"):
            strategies.append(("fiberhome", self._login_fiberhome))
        if self.mode in ("auto", "plain"):
            strategies.append(("plain", self._login_plain))
        if self.mode in ("auto", "basic"):
            strategies.append(("basic", self._login_basic))

        attempts = []
        for name, strategy in strategies:
            ok, message = strategy()
            if not ok:
                attempts.append(message)
                continue
            if self.mode == "auto" and not self._has_data_access():
                attempts.append(message + " (no data access)")
                self.logged_in = False
                if name == "basic":
                    self.http.auth = None
                continue
            self.profile = name
            self.logged_in = True
            return True, message

        return False, " | ".join(attempts) if attempts else "Unknown auth mode."

    def _logout_username(self):
        """Resolve the role name expected by the router's do_logout method."""

        role = str((self.get("get_login_user") or {}).get("login_user", ""))
        if role in ("4", "superadmin"):
            return "superadmin"
        if role in ("2", "admin"):
            return "admin"
        return "common"

    def logout(self):
        """Explicitly end the router session using the router's own do_logout.

        The firmware replies with session_valid 1 to do_logout, but a following
        get_base_info reports session_valid 0, so HTTP 200 is the success
        signal here. Local state is always cleared even if the request fails.
        """

        if not self.logged_in and not self.sessionid:
            return True, "No active router session."

        response = self._post("/cgi-bin/ajax", {
            "ajaxmethod": "do_logout",
            "sessionid": self.sessionid or "",
            "username": self._logout_username(),
        })
        self.logged_in = False
        self.sessionid = None
        self.http.auth = None
        if response is None or response.status_code != 200:
            return False, "Router logout request failed."
        return True, "Disconnected from router."

    # ---------- data collection ----------
    def collect(self):
        """Fetch router data using the known profile plus generic fallbacks."""
        methods = [
            "get_base_info", "get_allwan_info", "get_lan_status",
            "get_operator", "get_device_name", "get_dongle_info",
            "get_ipv6_lan", "get_login_user", "get_voice_status",
        ]
        raw = {name: self.get(name) for name in methods}
        self.model = raw.get("get_device_name", {}).get("ModelName", "")

        generic = []
        has_fiberhome_data = bool(self.model or raw.get("get_base_info", {}).get("ModelName"))
        if not has_fiberhome_data:
            for name in GENERIC_ENDPOINTS:
                payload = self.get(name)
                if payload and payload.get("session_valid", 1) != 0:
                    generic.append({"endpoint": name, "data": payload})
            # Also try a couple of non-ajax paths used by common firmwares.
            for path in ("/status", "/cgi-bin/status", "/api/status"):
                try:
                    response = self.http.get(self.root + path, timeout=4)
                    payload = _parse_response(response)
                    if payload:
                        generic.append({"endpoint": path, "data": payload})
                except requests.RequestException:
                    pass

        raw["generic"] = generic
        raw["_meta"] = {
            "gateway": self.host,
            "sessionid": self.sessionid,
            "profile": self.profile,
            "model": self.model,
            "session_valid": (
                raw.get("get_base_info", {}).get("session_valid", 1)
                if has_fiberhome_data else 1
            ),
        }
        return raw
