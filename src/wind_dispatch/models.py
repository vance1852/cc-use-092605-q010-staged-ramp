"""海上风电场调度领域输入契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .clock import parse_utc
from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
POWER_PRICE_INDEXES = {"PEAK_VALLEY", "MARKET_SETTLED", "GRID_COMMITTED", "DAY_AHEAD", "REGULATED", "CUSTOM"}
PRODUCTS = {"turbine-18mw", "turbine-16mw", "turbine-14mw", "reactive-compensator", "subsea-cable", "maintenance-vessel"}
ROUTE_KINDS = {"export-corridor", "offshore-station", "station", "storage", "compensation-station"}


def required_text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, field: str) -> str:
    result = required_text(value, field, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationFailed(f"{field} 格式不正确")
    return result


def decimal_value(
    value: object,
    field: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal:
    if isinstance(value, bool):
        raise ValidationFailed(f"{field} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationFailed(f"{field} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValidationFailed(f"{field} 必须是有限数值")
    if minimum is not None and result < minimum:
        raise ValidationFailed(f"{field} 不能小于 {minimum}")
    if maximum is not None and result > maximum:
        raise ValidationFailed(f"{field} 不能大于 {maximum}")
    return result


def positive_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationFailed(f"{field} 必须是正整数")
    return value


def date_text(value: object, field: str) -> str:
    result = required_text(value, field, 10)
    try:
        return date.fromisoformat(result).isoformat()
    except ValueError as exc:
        raise ValidationFailed(f"{field} 必须是 YYYY-MM-DD 日期") from exc


@dataclass(frozen=True, slots=True)
class IndexQuote:
    market_index: str
    trade_date: str
    close_cny: Decimal
    source_revision: str
    observed_at: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "IndexQuote":
        market_index = required_text(raw.get("market_index"), "market_index", 16).upper()
        if market_index not in POWER_PRICE_INDEXES - {"CUSTOM"}:
            raise ValidationFailed("market_index 必须是 PEAK_VALLEY、MARKET_SETTLED、GRID_COMMITTED、DAY_AHEAD 或 REGULATED")
        observed_at = required_text(raw.get("observed_at"), "observed_at", 40)
        try:
            parse_utc(observed_at, "observed_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        return cls(
            market_index=market_index,
            trade_date=date_text(raw.get("trade_date"), "trade_date"),
            close_cny=decimal_value(raw.get("close_cny"), "close_cny", minimum=Decimal("0.01")),
            source_revision=identifier(raw.get("source_revision"), "source_revision"),
            observed_at=observed_at,
        )


@dataclass(frozen=True, slots=True)
class Facility:
    facility_id: str
    name: str
    kind: str
    timezone: str
    capacity_mwh: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Facility":
        kind = required_text(raw.get("kind"), "kind", 24)
        if kind not in ROUTE_KINDS:
            raise ValidationFailed("kind 不是受支持的设施类型")
        timezone = required_text(raw.get("timezone"), "timezone", 64)
        if "/" not in timezone and timezone != "UTC":
            raise ValidationFailed("timezone 必须是 IANA 时区或 UTC")
        return cls(
            facility_id=identifier(raw.get("facility_id"), "facility_id"),
            name=required_text(raw.get("name"), "name"),
            kind=kind,
            timezone=timezone,
            capacity_mwh=decimal_value(
                raw.get("capacity_mwh"), "capacity_mwh", minimum=Decimal("0")
            ),
        )


@dataclass(frozen=True, slots=True)
class Route:
    route_id: str
    origin_id: str
    destination_id: str
    product: str
    daily_capacity: Decimal
    loss_basis_points: int
    transit_hours: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Route":
        product = required_text(raw.get("product"), "product", 32)
        if product not in PRODUCTS:
            raise ValidationFailed("product 不是受支持的机组类型")
        loss = raw.get("loss_basis_points", 0)
        if isinstance(loss, bool) or not isinstance(loss, int) or not 0 <= loss <= 1000:
            raise ValidationFailed("loss_basis_points 必须是 0 到 1000 的整数")
        origin = identifier(raw.get("origin_id"), "origin_id")
        destination = identifier(raw.get("destination_id"), "destination_id")
        if origin == destination:
            raise ValidationFailed("送出通道起点和终点不能相同")
        return cls(
            route_id=identifier(raw.get("route_id"), "route_id"),
            origin_id=origin,
            destination_id=destination,
            product=product,
            daily_capacity=decimal_value(
                raw.get("daily_capacity"), "daily_capacity", minimum=Decimal("0.001")
            ),
            loss_basis_points=loss,
            transit_hours=positive_integer(raw.get("transit_hours"), "transit_hours"),
        )


@dataclass(frozen=True, slots=True)
class InventoryLot:
    lot_id: str
    facility_id: str
    product: str
    grade: str
    quantity_mwh: Decimal
    unit_cost_cny: Decimal
    received_at: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "InventoryLot":
        product = required_text(raw.get("product"), "product", 32)
        if product not in PRODUCTS:
            raise ValidationFailed("product 不是受支持的机组类型")
        received_at = required_text(raw.get("received_at"), "received_at", 40)
        try:
            parse_utc(received_at, "received_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        return cls(
            lot_id=identifier(raw.get("lot_id"), "lot_id"),
            facility_id=identifier(raw.get("facility_id"), "facility_id"),
            product=product,
            grade=required_text(raw.get("grade"), "grade", 32).upper(),
            quantity_mwh=decimal_value(
                raw.get("quantity_mwh"), "quantity_mwh", minimum=Decimal("0.001")
            ),
            unit_cost_cny=decimal_value(
                raw.get("unit_cost_cny"), "unit_cost_cny", minimum=Decimal("0")
            ),
            received_at=received_at,
        )


@dataclass(frozen=True, slots=True)
class NominationRequest:
    nomination_id: str
    route_id: str
    shipper_id: str
    service_date: str
    requested_mwh: Decimal
    priority: int
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "NominationRequest":
        priority = raw.get("priority", 100)
        if isinstance(priority, bool) or not isinstance(priority, int) or not 1 <= priority <= 999:
            raise ValidationFailed("priority 必须是 1 到 999 的整数")
        return cls(
            nomination_id=identifier(raw.get("nomination_id"), "nomination_id"),
            route_id=identifier(raw.get("route_id"), "route_id"),
            shipper_id=identifier(raw.get("shipper_id"), "shipper_id"),
            service_date=date_text(raw.get("service_date"), "service_date"),
            requested_mwh=decimal_value(
                raw.get("requested_mwh"), "requested_mwh", minimum=Decimal("0.001")
            ),
            priority=priority,
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )


@dataclass(frozen=True, slots=True)
class SeaStateObservation:
    wind_speed_mps: Decimal
    wave_height_m: Decimal

    @classmethod
    def from_dict(cls, raw: object) -> "SeaStateObservation":
        if not isinstance(raw, Mapping):
            raise ValidationFailed("sea_state 必须是对象")
        return cls(
            wind_speed_mps=decimal_value(
                raw.get("wind_speed_mps"), "wind_speed_mps", minimum=Decimal("0"), maximum=Decimal("60")
            ),
            wave_height_m=decimal_value(
                raw.get("wave_height_m"), "wave_height_m", minimum=Decimal("0"), maximum=Decimal("20")
            ),
        )


@dataclass(frozen=True, slots=True)
class UnitCapability:
    unit_id: str
    project: str
    rated_mw: Decimal
    available: bool
    ramp_mw_per_min: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "UnitCapability":
        available = raw.get("available", True)
        if not isinstance(available, bool):
            raise ValidationFailed("available 必须是布尔值")
        rated = decimal_value(raw.get("rated_mw"), "rated_mw", minimum=Decimal("0.001"), maximum=Decimal("30"))
        return cls(
            unit_id=identifier(raw.get("unit_id"), "unit_id"),
            project=identifier(raw.get("project"), "project"),
            rated_mw=rated,
            available=available,
            ramp_mw_per_min=decimal_value(
                raw.get("ramp_mw_per_min"), "ramp_mw_per_min", minimum=Decimal("0.001"), maximum=rated
            ),
        )


@dataclass(frozen=True, slots=True)
class CorridorLimit:
    corridor_id: str
    cable_thermal_limit_mw: Decimal
    reactive_limit_mvar: Decimal
    spinning_reserve_mw: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CorridorLimit":
        return cls(
            corridor_id=identifier(raw.get("corridor_id"), "corridor_id"),
            cable_thermal_limit_mw=decimal_value(
                raw.get("cable_thermal_limit_mw"), "cable_thermal_limit_mw",
                minimum=Decimal("0.001"), maximum=Decimal("5000"),
            ),
            reactive_limit_mvar=decimal_value(
                raw.get("reactive_limit_mvar"), "reactive_limit_mvar",
                minimum=Decimal("0.001"), maximum=Decimal("2000"),
            ),
            spinning_reserve_mw=decimal_value(
                raw.get("spinning_reserve_mw"), "spinning_reserve_mw",
                minimum=Decimal("0"), maximum=Decimal("500"),
            ),
        )


@dataclass(frozen=True, slots=True)
class RehearsalSnapshotRequest:
    snapshot_id: str
    label: str
    observed_at: str
    units: tuple[UnitCapability, ...]
    sea_state: SeaStateObservation
    corridors: tuple[CorridorLimit, ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RehearsalSnapshotRequest":
        units_raw = raw.get("units")
        if not isinstance(units_raw, list) or not 1 <= len(units_raw) <= 500:
            raise ValidationFailed("units 必须包含 1 到 500 台机组")
        units = tuple(UnitCapability.from_dict(item) for item in units_raw)
        if len({unit.unit_id for unit in units}) != len(units):
            raise ValidationFailed("units 存在重复机组编号")
        corridors_raw = raw.get("corridors")
        if not isinstance(corridors_raw, list) or not 1 <= len(corridors_raw) <= 20:
            raise ValidationFailed("corridors 必须包含 1 到 20 条通道")
        corridors = tuple(CorridorLimit.from_dict(item) for item in corridors_raw)
        if len({corridor.corridor_id for corridor in corridors}) != len(corridors):
            raise ValidationFailed("corridors 存在重复通道编号")
        observed_at = required_text(raw.get("observed_at"), "observed_at", 40)
        try:
            parse_utc(observed_at, "observed_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        return cls(
            snapshot_id=identifier(raw.get("snapshot_id"), "snapshot_id"),
            label=required_text(raw.get("label"), "label"),
            observed_at=observed_at,
            units=units,
            sea_state=SeaStateObservation.from_dict(raw.get("sea_state")),
            corridors=corridors,
        )


@dataclass(frozen=True, slots=True)
class TrajectoryPoint:
    offset_minutes: int
    target_mw: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "TrajectoryPoint":
        offset = raw.get("offset_minutes")
        if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 1440:
            raise ValidationFailed("offset_minutes 必须是 0 到 1440 的整数")
        return cls(
            offset_minutes=offset,
            target_mw=decimal_value(
                raw.get("target_mw"), "target_mw", minimum=Decimal("0"), maximum=Decimal("10000")
            ),
        )


@dataclass(frozen=True, slots=True)
class DeratingPolicy:
    cable_derate_percent: Decimal
    reactive_derate_percent: Decimal
    min_spinning_reserve_mw: Decimal
    safety_margin_percent: Decimal
    trial_fraction: Decimal
    min_power_factor: Decimal

    @classmethod
    def from_dict(cls, raw: object) -> "DeratingPolicy":
        if raw is None:
            raw = {}
        if not isinstance(raw, Mapping):
            raise ValidationFailed("derating 必须是对象")
        return cls(
            cable_derate_percent=decimal_value(
                raw.get("cable_derate_percent", "5"), "cable_derate_percent",
                minimum=Decimal("0"), maximum=Decimal("50"),
            ),
            reactive_derate_percent=decimal_value(
                raw.get("reactive_derate_percent", "10"), "reactive_derate_percent",
                minimum=Decimal("0"), maximum=Decimal("50"),
            ),
            min_spinning_reserve_mw=decimal_value(
                raw.get("min_spinning_reserve_mw", "0"), "min_spinning_reserve_mw",
                minimum=Decimal("0"), maximum=Decimal("1000"),
            ),
            safety_margin_percent=decimal_value(
                raw.get("safety_margin_percent", "5"), "safety_margin_percent",
                minimum=Decimal("0"), maximum=Decimal("50"),
            ),
            trial_fraction=decimal_value(
                raw.get("trial_fraction", "0.25"), "trial_fraction",
                minimum=Decimal("0.01"), maximum=Decimal("0.5"),
            ),
            min_power_factor=decimal_value(
                raw.get("min_power_factor", "0.95"), "min_power_factor",
                minimum=Decimal("0.85"), maximum=Decimal("0.999"),
            ),
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "cable_derate_percent": format(self.cable_derate_percent, "f"),
            "reactive_derate_percent": format(self.reactive_derate_percent, "f"),
            "min_spinning_reserve_mw": format(self.min_spinning_reserve_mw, "f"),
            "safety_margin_percent": format(self.safety_margin_percent, "f"),
            "trial_fraction": format(self.trial_fraction, "f"),
            "min_power_factor": format(self.min_power_factor, "f"),
        }


@dataclass(frozen=True, slots=True)
class RehearsalPlanRequest:
    plan_id: str
    snapshot_id: str
    command_id: str
    trajectory: tuple[TrajectoryPoint, ...]
    derating: DeratingPolicy
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RehearsalPlanRequest":
        trajectory_raw = raw.get("target_trajectory")
        if not isinstance(trajectory_raw, list) or not 1 <= len(trajectory_raw) <= 24:
            raise ValidationFailed("target_trajectory 必须包含 1 到 24 个轨迹点")
        points = tuple(TrajectoryPoint.from_dict(item) for item in trajectory_raw)
        offsets = [point.offset_minutes for point in points]
        if len(set(offsets)) != len(offsets) or offsets != sorted(offsets):
            raise ValidationFailed("target_trajectory 的时间偏移必须严格递增")
        targets = [point.target_mw for point in points]
        if targets != sorted(targets):
            raise ValidationFailed("target_trajectory 的目标功率必须单调不减")
        if targets[-1] <= 0:
            raise ValidationFailed("target_trajectory 的峰值功率必须大于零")
        return cls(
            plan_id=identifier(raw.get("plan_id"), "plan_id"),
            snapshot_id=identifier(raw.get("snapshot_id"), "snapshot_id"),
            command_id=identifier(raw.get("command_id"), "command_id"),
            trajectory=points,
            derating=DeratingPolicy.from_dict(raw.get("derating")),
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )


@dataclass(frozen=True, slots=True)
class SupplyScenario:
    scenario_id: str
    name: str
    market_index_drop_percent: Decimal
    route_capacity_changes: Mapping[str, Decimal]
    demand_changes: Mapping[str, Decimal]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SupplyScenario":
        route_changes = raw.get("route_capacity_changes", {})
        demand_changes = raw.get("demand_changes", {})
        if not isinstance(route_changes, Mapping) or not isinstance(demand_changes, Mapping):
            raise ValidationFailed("情景变化必须是对象")
        parsed_routes = {
            identifier(key, "route_capacity_changes 键"): decimal_value(
                value, f"route_capacity_changes.{key}", minimum=Decimal("-100"), maximum=Decimal("500")
            )
            for key, value in route_changes.items()
        }
        parsed_demand = {
            identifier(key, "demand_changes 键"): decimal_value(
                value, f"demand_changes.{key}", minimum=Decimal("-100"), maximum=Decimal("500")
            )
            for key, value in demand_changes.items()
        }
        return cls(
            scenario_id=identifier(raw.get("scenario_id"), "scenario_id"),
            name=required_text(raw.get("name"), "name"),
            market_index_drop_percent=decimal_value(
                raw.get("market_index_drop_percent", 0),
                "market_index_drop_percent",
                minimum=Decimal("-500"),
                maximum=Decimal("100"),
            ),
            route_capacity_changes=parsed_routes,
            demand_changes=parsed_demand,
        )
