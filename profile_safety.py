"""Shared request-safety rules for declarative connector profiles.

These helpers are intentionally generic: they contain no vendor behavior and no
executable profile logic. Both the runtime executor and the Profile Builder CLI
use them so a profile cannot turn a configured gateway into an arbitrary URL.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any

SAFE_METHODS = frozenset({"GET", "POST"})
SAFE_SCHEMES = frozenset({"http", "https"})
MAX_RESPONSE_BYTES = 1_048_576
MAX_ENDPOINT_PATH_LENGTH = 2048

_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")
_PATH_RE = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@/-]*$")
_BLOCKED_HOSTNAMES = frozenset({
    "localhost",
    "localhost.localdomain",
    "metadata",
    "metadata.google.internal",
    "metadata.goog",
})


class UnsafeProfileRequest(ValueError):
    """Raised when a profile or gateway would produce an unsafe request."""


def validate_method(method: Any) -> str:
    """Return the normalized HTTP method or reject it."""

    normalized = str(method or "").strip().upper()
    if normalized not in SAFE_METHODS:
        raise UnsafeProfileRequest(
            f"unsafe HTTP method {method!r}; only GET and POST are allowed"
        )
    return normalized


def validate_scheme(scheme: Any) -> str:
    """Return the normalized scheme or reject it."""

    normalized = str(scheme or "").strip().lower()
    if normalized not in SAFE_SCHEMES:
        raise UnsafeProfileRequest(
            f"unsafe scheme {scheme!r}; only http and https are allowed"
        )
    return normalized


def validate_endpoint_path(path: Any) -> str:
    """Validate one profile-declared path.

    Query parameters belong in the profile's ``query`` object, not in ``path``.
    Fragments, traversal, encoded separators, control characters, backslashes,
    and network-path references are rejected so the path cannot escape the
    configured gateway.
    """

    text = str(path or "")
    if not text or len(text) > MAX_ENDPOINT_PATH_LENGTH:
        raise UnsafeProfileRequest("endpoint path is missing or too long")
    if not text.startswith("/") or text.startswith("//"):
        raise UnsafeProfileRequest("endpoint path must be a single leading-slash path")
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise UnsafeProfileRequest("endpoint path contains a control character")
    if ".." in text.split("/") or any(mark in text for mark in ("\\", "?", "#", "%")):
        raise UnsafeProfileRequest("endpoint path contains traversal or an unsafe marker")
    if not _PATH_RE.fullmatch(text):
        raise UnsafeProfileRequest("endpoint path contains unsupported characters")
    return text


def normalize_gateway(gateway: Any) -> str:
    """Return a host only, rejecting URLs, userinfo, ports, and metadata hosts."""

    text = str(gateway or "").strip().rstrip("/")
    if not text:
        raise UnsafeProfileRequest("a gateway address is required")
    if "://" in text:
        raise UnsafeProfileRequest("gateway must be a bare host, not a URL")
    if text.startswith("//") or any(mark in text for mark in ("/", "\\", "@", "?", "#", ":")):
        raise UnsafeProfileRequest("gateway must be a bare host without userinfo, port, or path")
    if not _HOST_RE.fullmatch(text):
        raise UnsafeProfileRequest("gateway contains unsupported characters")
    if text.lower() in _BLOCKED_HOSTNAMES:
        raise UnsafeProfileRequest("metadata service hostnames are not allowed")
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        # Some resolvers accept alternate numeric loopback spellings that
        # ``ipaddress`` rejects (127.1, 2130706433, 0x7f000001, 0177.0.0.1,
        # and mixed forms such as 0x7f.1). Reject numeric-looking aliases and
        # radix-prefixed labels rather than resolving DNS here.
        if (
            re.fullmatch(r"[0-9.]+", text)
            or re.fullmatch(r"0[xX][0-9A-Fa-f]+", text)
            or re.fullmatch(r"0[oO][0-7]+", text)
            or re.fullmatch(r"0[bB][01]+", text)
            or any(
                label.lower().startswith(("0x", "0o", "0b"))
                for label in text.split(".")
            )
        ):
            raise UnsafeProfileRequest("numeric gateway aliases are not allowed")
        return text
    if (
        address.is_unspecified
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
    ):
        raise UnsafeProfileRequest("gateway address is not allowed")
    return text


def build_request_url(scheme: Any, gateway: Any, path: Any) -> str:
    """Build a gateway-pinned URL from validated profile components."""

    return (
        f"{validate_scheme(scheme)}://"
        f"{normalize_gateway(gateway)}"
        f"{validate_endpoint_path(path)}"
    )


def is_redirect(status_code: Any) -> bool:
    """Return True for 3xx responses that must never be followed."""

    try:
        code = int(status_code)
    except (TypeError, ValueError):
        return False
    return 300 <= code < 400


def check_response_size(response: Any) -> str:
    """Return response text only when it stays within the bounded size."""

    headers = getattr(response, "headers", {}) or {}
    header = headers.get("Content-Length", headers.get("content-length"))
    if header is not None:
        try:
            length = int(header)
        except (TypeError, ValueError):
            length = None
        if length is not None and length > MAX_RESPONSE_BYTES:
            raise UnsafeProfileRequest("response exceeds the size limit")
    text = getattr(response, "text", "") or ""
    if len(str(text).encode("utf-8", errors="replace")) > MAX_RESPONSE_BYTES:
        raise UnsafeProfileRequest("response exceeds the size limit")
    return str(text)


def read_bounded_text(response: Any) -> str:
    """Read and decode a response body without buffering unlimited data.

    Real ``requests`` responses are streamed in bounded chunks. Minimal fake
    sessions used by offline fixtures do not expose ``iter_content``, so they
    fall back to the already materialized-text guard.
    """

    headers = getattr(response, "headers", {}) or {}
    header = headers.get("Content-Length", headers.get("content-length"))
    if header is not None:
        try:
            length = int(header)
        except (TypeError, ValueError):
            length = None
        if length is not None and length > MAX_RESPONSE_BYTES:
            raise UnsafeProfileRequest("response exceeds the size limit")

    iterator = getattr(response, "iter_content", None)
    if callable(iterator):
        chunks = []
        total = 0
        for chunk in iterator(chunk_size=8192):
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                raise UnsafeProfileRequest("response exceeds the size limit")
            chunks.append(chunk)
        encoding = getattr(response, "encoding", None) or "utf-8"
        return b"".join(chunks).decode(encoding, errors="replace")
    return check_response_size(response)
