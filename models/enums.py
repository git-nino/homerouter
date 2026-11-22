"""Stable enumerations for the universal FTTH data model.

These values are part of the internal contract between the connector engine,
the normalized model, and the dashboard. They are intentionally explicit so
that "missing", "unsupported", "invalid", and "not loaded" can never be
silently rendered as zero, false, or an empty string.
"""

from enum import Enum


class Availability(str, Enum):
    """State of a single normalized value."""

    AVAILABLE = "available"
    STALE = "stale"
    UNAVAILABLE = "unavailable"
    UNSUPPORTED = "unsupported"
    INVALID = "invalid"
    NOT_LOADED = "not_loaded"
    ERROR = "error"


class Domain(str, Enum):
    """The seven domains of the universal dashboard."""

    DEVICE_IDENTITY = "device_identity"
    WAN = "wan"
    OPTICAL = "optical"
    VOIP = "voip"
    SYSTEM = "system"
    CONNECTED_DEVICES = "connected_devices"
    SPEED_TEST = "speed_test"


class ValueType(str, Enum):
    """Stable value types used by metric definitions."""

    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    DURATION_SECONDS = "duration_seconds"
    IP_ADDRESS = "ip_address"
    MAC_ADDRESS = "mac_address"
    PERCENT = "percent"
    DBM = "dbm"
    CELSIUS = "celsius"
    MILLIAMP = "milliamp"
    VOLT = "volt"
    MBPS = "megabits_per_second"
    MILLISECONDS = "milliseconds"
    TIMESTAMP = "timestamp"


class CompatibilityState(str, Enum):
    """High-level compatibility state of an identified device/profile."""

    COMPATIBLE = "compatible"
    PARTIALLY_COMPATIBLE = "partially_compatible"
    INCOMPATIBLE = "incompatible"
    UNSUPPORTED_FIRMWARE = "unsupported_firmware"
    AUTHENTICATION_FAILED = "authentication_failed"
    CONNECTION_FAILED = "connection_failed"
    IDENTIFICATION_INCOMPLETE = "identification_incomplete"
