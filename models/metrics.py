"""Metric definitions and single-value containers."""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .enums import Availability, Domain, ValueType


UNITS: Dict[ValueType, Optional[str]] = {
    ValueType.STRING: None,
    ValueType.INTEGER: None,
    ValueType.NUMBER: None,
    ValueType.BOOLEAN: None,
    ValueType.DURATION_SECONDS: "s",
    ValueType.IP_ADDRESS: None,
    ValueType.MAC_ADDRESS: None,
    ValueType.PERCENT: "%",
    ValueType.DBM: "dBm",
    ValueType.CELSIUS: "°C",
    ValueType.MILLIAMP: "mA",
    ValueType.VOLT: "V",
    ValueType.MBPS: "Mbps",
    ValueType.MILLISECONDS: "ms",
    ValueType.TIMESTAMP: None,
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class MetricSpec:
    """Immutable definition of one stable metric identifier."""

    id: str
    domain: Domain
    label: str
    value_type: ValueType
    unit: Optional[str] = None


@dataclass
class Metric:
    """One normalized value plus its availability and provenance.

    ``value`` is the normalized value. ``source_value`` keeps the original
    connector value so diagnostics and future migrations never lose evidence.
    A non-available state must never carry a fabricated ``value``.
    """

    spec: MetricSpec
    value: Optional[Any] = None
    availability: Availability = Availability.NOT_LOADED
    source_value: Optional[Any] = None
    source: Optional[str] = None
    observed_at: Optional[datetime] = None
    error: Optional[str] = None

    @property
    def id(self) -> str:
        return self.spec.id

    @property
    def domain(self) -> Domain:
        return self.spec.domain

    @property
    def label(self) -> str:
        return self.spec.label

    @property
    def unit(self) -> Optional[str]:
        return self.spec.unit

    @property
    def value_type(self) -> ValueType:
        return self.spec.value_type

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "domain": self.domain.value,
            "label": self.label,
            "type": self.value_type.value,
            "unit": self.unit,
            "value": self.value,
            "source_value": self.source_value,
            "availability": self.availability.value,
            "source": self.source,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "error": self.error,
        }
