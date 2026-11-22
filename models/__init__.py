"""Universal normalized FTTH data model."""

from .enums import (
    Availability,
    CompatibilityState,
    Domain,
    ValueType,
)
from .metrics import Metric, MetricSpec
from .phone import CallAction, build_call_action, build_tel_uri, normalize_phone_number
from .universal import (
    METRICS,
    Compatibility,
    ConnectedDevice,
    UniversalModel,
    VoipLine,
    make_metric,
)

__all__ = [
    "Availability",
    "CallAction",
    "Compatibility",
    "CompatibilityState",
    "ConnectedDevice",
    "Domain",
    "METRICS",
    "Metric",
    "MetricSpec",
    "UniversalModel",
    "ValueType",
    "VoipLine",
    "build_call_action",
    "build_tel_uri",
    "make_metric",
    "normalize_phone_number",
]
