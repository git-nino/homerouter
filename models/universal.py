"""Universal normalized FTTH model.

The model is storage only: it does not know about HTTP, router APIs, or HTML.
Connectors write metrics through :meth:`UniversalModel.set_metric`; the
dashboard reads the JSON produced by :meth:`UniversalModel.to_dict`.

Important invariants:

* every metric has a stable id, domain, type, and unit;
* missing data is represented by an explicit availability state, never by a
  fabricated zero, empty string, or false value;
* the original connector value is preserved in ``source_value``;
* connected-device count is derived only from an actually loaded device list;
* VoIP call actions are generated only from a validated phone number.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from .enums import Availability, CompatibilityState, Domain, ValueType
from .metrics import Metric, MetricSpec, UNITS, utcnow
from .phone import CallAction, build_call_action


_MISSING = object()
_VALUE_BEARING_STATES = {Availability.AVAILABLE, Availability.STALE}


def _spec(
    metric_id: str,
    domain: Domain,
    label: str,
    value_type: ValueType,
) -> MetricSpec:
    return MetricSpec(
        id=metric_id,
        domain=domain,
        label=label,
        value_type=value_type,
        unit=UNITS[value_type],
    )


METRICS: Dict[str, MetricSpec] = {
    spec.id: spec
    for spec in [
        # Device identity -------------------------------------------------
        _spec("device.manufacturer", Domain.DEVICE_IDENTITY, "Manufacturer", ValueType.STRING),
        _spec("device.model", Domain.DEVICE_IDENTITY, "Model", ValueType.STRING),
        _spec("device.firmware", Domain.DEVICE_IDENTITY, "Firmware", ValueType.STRING),
        _spec("device.hardware", Domain.DEVICE_IDENTITY, "Hardware version", ValueType.STRING),
        _spec("device.serial", Domain.DEVICE_IDENTITY, "Serial number", ValueType.STRING),
        _spec("device.gpon_sn", Domain.DEVICE_IDENTITY, "GPON SN", ValueType.STRING),
        _spec("device.operator", Domain.DEVICE_IDENTITY, "Operator", ValueType.STRING),
        _spec("device.mac_base", Domain.DEVICE_IDENTITY, "Base MAC", ValueType.MAC_ADDRESS),
        _spec("device.mac_wan", Domain.DEVICE_IDENTITY, "WAN MAC", ValueType.MAC_ADDRESS),
        # WAN / Internet --------------------------------------------------
        _spec("wan.connection_name", Domain.WAN, "Connection", ValueType.STRING),
        _spec("wan.connection_type", Domain.WAN, "Type", ValueType.STRING),
        _spec("wan.status", Domain.WAN, "Status", ValueType.STRING),
        _spec("wan.public_ip", Domain.WAN, "Public IP", ValueType.IP_ADDRESS),
        _spec("wan.gateway_ip", Domain.WAN, "ISP gateway", ValueType.IP_ADDRESS),
        _spec("wan.dns_servers", Domain.WAN, "DNS servers", ValueType.STRING),
        _spec("wan.vlan_id", Domain.WAN, "VLAN ID", ValueType.STRING),
        _spec("wan.pppoe_user", Domain.WAN, "PPPoE user", ValueType.STRING),
        _spec("wan.mtu", Domain.WAN, "MTU", ValueType.INTEGER),
        _spec("wan.nat", Domain.WAN, "NAT", ValueType.STRING),
        _spec("wan.uptime_s", Domain.WAN, "Connection uptime", ValueType.DURATION_SECONDS),
        _spec("wan.last_error", Domain.WAN, "Last error", ValueType.STRING),
        _spec("wan.mac", Domain.WAN, "WAN MAC", ValueType.MAC_ADDRESS),
        # Optical / PON ---------------------------------------------------
        _spec("optical.tx_power_dbm", Domain.OPTICAL, "Optical TX power", ValueType.DBM),
        _spec("optical.rx_power_dbm", Domain.OPTICAL, "Optical RX power", ValueType.DBM),
        _spec("optical.temperature_c", Domain.OPTICAL, "Transceiver temperature", ValueType.CELSIUS),
        _spec("optical.bias_current_ma", Domain.OPTICAL, "Laser bias current", ValueType.MILLIAMP),
        _spec("optical.supply_voltage_v", Domain.OPTICAL, "Supply voltage", ValueType.VOLT),
        _spec("optical.pon_mode", Domain.OPTICAL, "PON mode", ValueType.STRING),
        _spec("optical.onu_id", Domain.OPTICAL, "ONU ID", ValueType.INTEGER),
        _spec("optical.olt_vendor", Domain.OPTICAL, "OLT vendor", ValueType.STRING),
        _spec("optical.registration_state", Domain.OPTICAL, "PON registration", ValueType.STRING),
        _spec("optical.auth_status", Domain.OPTICAL, "Auth status", ValueType.STRING),
        _spec("optical.loid", Domain.OPTICAL, "LoID", ValueType.STRING),
        _spec("optical.bytes_sent", Domain.OPTICAL, "PON bytes sent", ValueType.INTEGER),
        _spec("optical.bytes_received", Domain.OPTICAL, "PON bytes received", ValueType.INTEGER),
        _spec("optical.packets_sent", Domain.OPTICAL, "PON packets sent", ValueType.INTEGER),
        _spec("optical.packets_received", Domain.OPTICAL, "PON packets received", ValueType.INTEGER),
        # VoIP / Telephony ------------------------------------------------
        _spec("voip.phone_number", Domain.VOIP, "Phone number", ValueType.STRING),
        _spec("voip.registration_state", Domain.VOIP, "Registration state", ValueType.STRING),
        _spec("voip.line_status", Domain.VOIP, "Line status", ValueType.STRING),
        _spec("voip.provider", Domain.VOIP, "Telephony provider", ValueType.STRING),
        # System ----------------------------------------------------------
        _spec("system.uptime_s", Domain.SYSTEM, "System uptime", ValueType.DURATION_SECONDS),
        _spec("system.cpu_percent", Domain.SYSTEM, "CPU usage", ValueType.PERCENT),
        _spec("system.memory_percent", Domain.SYSTEM, "Memory usage", ValueType.PERCENT),
        _spec("system.flash_percent", Domain.SYSTEM, "Flash usage", ValueType.PERCENT),
        _spec("system.lan_port_count", Domain.SYSTEM, "LAN ports", ValueType.INTEGER),
        _spec("system.uplink_mode", Domain.SYSTEM, "Uplink", ValueType.STRING),
        _spec("system.kernel", Domain.SYSTEM, "Kernel", ValueType.STRING),
        _spec("system.compile_time", Domain.SYSTEM, "Compiled", ValueType.STRING),
        # Connected devices ----------------------------------------------
        _spec("devices.count", Domain.CONNECTED_DEVICES, "Connected devices", ValueType.INTEGER),
        # Speed test ------------------------------------------------------
        _spec("speed_test.status", Domain.SPEED_TEST, "Status", ValueType.STRING),
        _spec("speed_test.download_mbps", Domain.SPEED_TEST, "Download", ValueType.MBPS),
        _spec("speed_test.upload_mbps", Domain.SPEED_TEST, "Upload", ValueType.MBPS),
        _spec("speed_test.ping_ms", Domain.SPEED_TEST, "Ping", ValueType.MILLISECONDS),
        _spec("speed_test.jitter_ms", Domain.SPEED_TEST, "Jitter", ValueType.MILLISECONDS),
        _spec("speed_test.server", Domain.SPEED_TEST, "Server", ValueType.STRING),
        _spec("speed_test.sponsor", Domain.SPEED_TEST, "Sponsor", ValueType.STRING),
        _spec("speed_test.country", Domain.SPEED_TEST, "Country", ValueType.STRING),
        _spec("speed_test.started_at", Domain.SPEED_TEST, "Started", ValueType.TIMESTAMP),
        _spec("speed_test.completed_at", Domain.SPEED_TEST, "Completed", ValueType.TIMESTAMP),
        _spec("speed_test.error", Domain.SPEED_TEST, "Error", ValueType.STRING),
    ]
}


def _coerce_domain(domain: Any) -> Domain:
    return domain if isinstance(domain, Domain) else Domain(domain)


def _validate_spec(domain: Domain, metric_id: str) -> MetricSpec:
    spec = METRICS.get(metric_id)
    if spec is None:
        raise KeyError(f"Unknown metric id: {metric_id}")
    if spec.domain != domain:
        raise KeyError(f"Metric {metric_id} does not belong to domain {domain.value}")
    return spec


def make_metric(
    domain: Any,
    metric_id: str,
    *,
    value: Any = None,
    availability: Any = _MISSING,
    source_value: Any = _MISSING,
    source: Optional[str] = None,
    observed_at: Optional[datetime] = None,
    error: Optional[str] = None,
) -> Metric:
    """Build a metric while enforcing the no-fabrication invariant."""

    domain = _coerce_domain(domain)
    spec = _validate_spec(domain, metric_id)

    if availability is _MISSING:
        availability = Availability.AVAILABLE if value is not None else Availability.NOT_LOADED
    elif not isinstance(availability, Availability):
        availability = Availability(availability)

    if availability in _VALUE_BEARING_STATES and value is None:
        raise ValueError(f"{metric_id} cannot be {availability.value} without a value")

    if source_value is _MISSING:
        source_value = value
    if availability not in _VALUE_BEARING_STATES and value is not None:
        # Keep the connector's raw evidence, but never normalize it as a value.
        source_value = value
        value = None
    if observed_at is None and availability in _VALUE_BEARING_STATES:
        observed_at = utcnow()

    return Metric(
        spec=spec,
        value=value,
        availability=availability,
        source_value=source_value,
        source=source,
        observed_at=observed_at,
        error=error,
    )


def _voip_reason(metric: Metric) -> str:
    return {
        Availability.NOT_LOADED: "Phone number has not been loaded.",
        Availability.UNSUPPORTED: "This profile does not expose a phone number.",
        Availability.UNAVAILABLE: "Phone number is not available on this device.",
        Availability.INVALID: "Phone number failed validation.",
        Availability.STALE: "Phone number is stale; refresh before calling.",
        Availability.ERROR: "Phone number could not be read.",
    }.get(metric.availability, "No valid phone number is available.")


@dataclass
class Compatibility:
    """Compatibility state with secret-free diagnostic reasons."""

    state: CompatibilityState = CompatibilityState.IDENTIFICATION_INCOMPLETE
    reasons: List[str] = field(default_factory=list)
    profile_id: Optional[str] = None
    firmware: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "state": self.state.value,
            "reasons": list(self.reasons),
            "profile_id": self.profile_id,
            "firmware": self.firmware,
        }


@dataclass
class ConnectedDevice:
    """One connected client with its original payload preserved."""

    id: str
    hostname: Optional[str] = None
    ip_address: Optional[str] = None
    mac_address: Optional[str] = None
    link_type: Optional[str] = None
    address_source: Optional[str] = None
    uptime_s: Optional[int] = None
    availability: Availability = Availability.AVAILABLE
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "hostname": self.hostname,
            "ip_address": self.ip_address,
            "mac_address": self.mac_address,
            "link_type": self.link_type,
            "address_source": self.address_source,
            "uptime_s": self.uptime_s,
            "availability": self.availability.value,
            "raw": dict(self.raw),
        }


@dataclass
class Collection:
    """A generic profile-defined list (LAN ports, SSIDs, future collections)."""

    name: str
    items: List[Dict[str, Any]] = field(default_factory=list)
    loaded: bool = False
    availability: Availability = Availability.NOT_LOADED
    source: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "items": [dict(item) for item in self.items],
            "count": len(self.items) if self.loaded else None,
            "loaded": self.loaded,
            "availability": self.availability.value,
            "source": self.source,
            "error": self.error,
        }


@dataclass
class VoipLine:
    """A telephony line plus its explicit native-dialer call action."""

    line_id: str
    phone_number: Metric
    registration_state: Metric
    line_status: Metric
    call_action: CallAction = field(init=False)

    def __post_init__(self) -> None:
        if self.phone_number.availability is Availability.AVAILABLE and self.phone_number.value is not None:
            self.call_action = build_call_action(self.phone_number.value)
        else:
            self.call_action = CallAction(False, None, _voip_reason(self.phone_number))

    def to_dict(self) -> dict:
        return {
            "line_id": self.line_id,
            "phone_number": self.phone_number.to_dict(),
            "registration_state": self.registration_state.to_dict(),
            "line_status": self.line_status.to_dict(),
            "call_action": self.call_action.to_dict(),
        }


@dataclass
class UniversalModel:
    """Complete normalized snapshot consumed by the universal dashboard."""

    gateway: Optional[str] = None
    profile_id: Optional[str] = None
    compatibility: Compatibility = field(default_factory=Compatibility)
    domains: Dict[Domain, Dict[str, Metric]] = field(
        default_factory=lambda: {domain: {} for domain in Domain}
    )
    devices: List[ConnectedDevice] = field(default_factory=list)
    devices_loaded: bool = False
    collections: Dict[str, Collection] = field(default_factory=dict)
    voip_lines: List[VoipLine] = field(default_factory=list)
    endpoint_errors: List[Dict[str, Any]] = field(default_factory=list)
    updated_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        for domain in Domain:
            self.domains.setdefault(domain, {})

    # ---------- metric access ----------
    def set_metric(
        self,
        domain: Any,
        metric_id: str,
        *,
        value: Any = None,
        availability: Any = _MISSING,
        source_value: Any = _MISSING,
        source: Optional[str] = None,
        observed_at: Optional[datetime] = None,
        error: Optional[str] = None,
    ) -> Metric:
        """Store one metric and return it."""

        metric = make_metric(
            domain,
            metric_id,
            value=value,
            availability=availability,
            source_value=source_value,
            source=source,
            observed_at=observed_at,
            error=error,
        )
        self.domains[metric.domain][metric_id] = metric
        return metric

    def get_metric(self, domain: Any, metric_id: str) -> Metric:
        """Return a metric; absent values are explicitly NOT_LOADED."""

        domain = _coerce_domain(domain)
        _validate_spec(domain, metric_id)
        metric = self.domains[domain].get(metric_id)
        if metric is not None:
            return metric
        return make_metric(domain, metric_id)

    def metric_ids(self, domain: Any) -> List[str]:
        domain = _coerce_domain(domain)
        return sorted(self.domains[domain])

    # ---------- aggregate helpers ----------
    def set_devices(
        self,
        devices: Optional[List[ConnectedDevice]],
        *,
        loaded: bool = True,
        source: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        """Store the device list and derive its count only when loaded."""

        self.devices = list(devices or [])
        self.devices_loaded = loaded

        if loaded:
            self.set_metric(
                Domain.CONNECTED_DEVICES,
                "devices.count",
                value=len(self.devices),
                source_value=len(self.devices),
                source=source,
                error=error,
            )
        else:
            self.set_metric(
                Domain.CONNECTED_DEVICES,
                "devices.count",
                availability=Availability.ERROR if error else Availability.NOT_LOADED,
                source_value=None,
                source=source,
                error=error,
            )

    def set_collection(
        self,
        name: str,
        items: Optional[List[Dict[str, Any]]],
        *,
        loaded: bool = True,
        source: Optional[str] = None,
        error: Optional[str] = None,
    ) -> Collection:
        """Store a generic collection without ever inventing its contents."""

        collection = Collection(
            name=name,
            items=list(items or []),
            loaded=loaded,
            availability=(
                Availability.AVAILABLE if loaded
                else Availability.ERROR if error
                else Availability.NOT_LOADED
            ),
            source=source,
            error=error,
        )
        self.collections[name] = collection
        return collection

    def get_collection(self, name: str) -> Collection:
        """Return a collection; an absent list is explicitly NOT_LOADED."""

        return self.collections.get(name, Collection(name=name))

    def add_endpoint_error(
        self,
        endpoint_id: str,
        *,
        error: str,
        source: Optional[str] = None,
    ) -> None:
        """Record an endpoint failure without fabricating metric values."""

        self.endpoint_errors.append({
            "endpoint": endpoint_id,
            "source": source,
            "error": error,
        })

    def set_primary_voip_line(
        self,
        *,
        phone_number: Any = None,
        phone_availability: Any = _MISSING,
        phone_source_value: Any = _MISSING,
        registration_state: Any = None,
        registration_availability: Any = _MISSING,
        line_status: Any = None,
        line_status_availability: Any = _MISSING,
        source: Optional[str] = None,
        error: Optional[str] = None,
    ) -> VoipLine:
        """Store the primary telephony line and its call action."""

        phone_metric = self.set_metric(
            Domain.VOIP,
            "voip.phone_number",
            value=phone_number,
            availability=phone_availability,
            source_value=phone_source_value,
            source=source,
            error=error,
        )
        registration_metric = self.set_metric(
            Domain.VOIP,
            "voip.registration_state",
            value=registration_state,
            availability=registration_availability,
            source=source,
            error=error,
        )
        status_metric = self.set_metric(
            Domain.VOIP,
            "voip.line_status",
            value=line_status,
            availability=line_status_availability,
            source=source,
            error=error,
        )

        line = VoipLine(
            line_id="primary",
            phone_number=phone_metric,
            registration_state=registration_metric,
            line_status=status_metric,
        )
        self.voip_lines = [line]
        return line

    def primary_call_action(self) -> CallAction:
        """Call action for the primary line; disabled unless explicitly valid."""

        if self.voip_lines:
            return self.voip_lines[0].call_action

        metric = self.get_metric(Domain.VOIP, "voip.phone_number")
        if metric.availability is Availability.AVAILABLE and metric.value is not None:
            return build_call_action(metric.value)
        return CallAction(False, None, _voip_reason(metric))

    # ---------- serialization ----------
    def to_dict(self) -> dict:
        return {
            "gateway": self.gateway,
            "profile_id": self.profile_id,
            "compatibility": self.compatibility.to_dict(),
            "domains": {
                domain.value: {
                    metric_id: metric.to_dict()
                    for metric_id, metric in sorted(metrics.items())
                }
                for domain, metrics in self.domains.items()
            },
            "devices": [device.to_dict() for device in self.devices],
            "devices_loaded": self.devices_loaded,
            "collections": {
                name: collection.to_dict()
                for name, collection in sorted(self.collections.items())
            },
            "voip_lines": [line.to_dict() for line in self.voip_lines],
            "call_action": self.primary_call_action().to_dict(),
            "endpoint_errors": list(self.endpoint_errors),
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
