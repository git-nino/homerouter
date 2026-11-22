"""Phone-number validation and safe dialer URI construction.

The call action is data only. Nothing in this module places a call; the UI must
hand the generated ``tel:`` URI to the device's native dialer after an explicit
user tap.
"""

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote


_ALLOWED_CHARS = re.compile(r"^[0-9+()\s.\-]+$")
_NON_DIGITS = re.compile(r"\D")

_MIN_DIGITS = 7
_MAX_DIGITS = 15


def normalize_phone_number(value: object) -> Optional[str]:
    """Return a digits-only (optionally E.164) number, or ``None`` if invalid.

    Accepted input is limited to digits, spaces, parentheses, dots, hyphens,
    and at most one leading ``+``. This deliberately rejects letters, URI
    schemes, and scripting payloads.
    """

    if value is None:
        return None

    raw = str(value).strip()
    if not raw or not _ALLOWED_CHARS.fullmatch(raw):
        return None
    if raw.count("+") > 1:
        return None

    is_international = raw.startswith("+")
    if "+" in raw and not is_international:
        return None

    digits = _NON_DIGITS.sub("", raw)
    if not _MIN_DIGITS <= len(digits) <= _MAX_DIGITS:
        return None

    return ("+" if is_international else "") + digits


def build_tel_uri(value: object) -> Optional[str]:
    """Build a safely encoded ``tel:`` URI from a validated phone number."""

    normalized = normalize_phone_number(value)
    if normalized is None:
        return None
    return "tel:" + quote(normalized, safe="+")


@dataclass(frozen=True)
class CallAction:
    """A disabled-by-default, explicitly triggered native-dialer action."""

    enabled: bool
    dial_uri: Optional[str] = None
    reason: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "dial_uri": self.dial_uri,
            "reason": self.reason,
        }


def build_call_action(
    value: object,
    *,
    unavailable_reason: str = "No phone number has been loaded.",
) -> CallAction:
    """Return a call action only when the number passes validation."""

    if value is None or value == "":
        return CallAction(False, None, unavailable_reason)

    dial_uri = build_tel_uri(value)
    if dial_uri is None:
        return CallAction(False, None, "Phone number failed validation.")

    return CallAction(True, dial_uri, None)
