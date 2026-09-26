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
TURBINE_PRODUCTS = {"turbine-18mw", "turbine-16mw", "turbine-14mw"}
RAMP_PHASES = ("prepare", "trial_send", "expand", "steady")


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


def optional_decimal(
    value: object,
    field: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal | None:
    if value is None:
        return None
    return decimal_value(value, field, minimum=minimum, maximum=maximum)


@dataclass(frozen=True, slots=True)
class SeaStateObservation:
    wind_speed_mps: Decimal
    wave_height_m: Decimal
    observed_at: str

    @classmethod
    def from_dict(cls, raw: object) -> "SeaStateObservation":
        if not isinstance(raw, Mapping):
            raise ValidationFailed("sea_state 必须是对象")
        observed_at = required_text(raw.get("observed_at"), "sea_state.observed_at", 40)
        try:
            parse_utc(observed_at, "sea_state.observed_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        return cls(
            wind_speed_mps=decimal_value(
                raw.get("wind_speed_mps"), "sea_state.wind_speed_mps",
                minimum=Decimal("0"), maximum=Decimal("70"),
            ),
            wave_height_m=decimal_value(
                raw.get("wave_height_m"), "sea_state.wave_height_m",
                minimum=Decimal("0"), maximum=Decimal("30"),
            ),
            observed_at=observed_at,
        )


@dataclass(frozen=True, slots=True)
class UnitCapability:
    unit_id: str
    product: str
    available_mw: Decimal
    ramp_mw_per_min: Decimal
    ready: bool

    @classmethod
    def from_dict(cls, raw: object, field: str) -> "UnitCapability":
        if not isinstance(raw, Mapping):
            raise ValidationFailed(f"{field} 必须是对象")
        product = required_text(raw.get("product"), f"{field}.product", 32)
        if product not in TURBINE_PRODUCTS:
            raise ValidationFailed(f"{field}.product 必须是风电机组类型")
        ready = raw.get("ready", True)
        if not isinstance(ready, bool):
            raise ValidationFailed(f"{field}.ready 必须是布尔值")
        return cls(
            unit_id=identifier(raw.get("unit_id"), f"{field}.unit_id"),
            product=product,
            available_mw=decimal_value(
                raw.get("available_mw"), f"{field}.available_mw",
                minimum=Decimal("0.001"), maximum=Decimal("1000"),
            ),
            ramp_mw_per_min=decimal_value(
                raw.get("ramp_mw_per_min"), f"{field}.ramp_mw_per_min",
                minimum=Decimal("0.001"), maximum=Decimal("200"),
            ),
            ready=ready,
        )


@dataclass(frozen=True, slots=True)
class ProjectCapability:
    project_id: str
    units: tuple[UnitCapability, ...]

    @classmethod
    def from_dict(cls, raw: object) -> "ProjectCapability":
        if not isinstance(raw, Mapping):
            raise ValidationFailed("projects 元素必须是对象")
        project_id = identifier(raw.get("project_id"), "projects.project_id")
        units_raw = raw.get("units")
        if not isinstance(units_raw, list) or not 1 <= len(units_raw) <= 200:
            raise ValidationFailed("projects.units 必须包含 1 到 200 台机组")
        units = tuple(
            UnitCapability.from_dict(item, f"{project_id}.units") for item in units_raw
        )
        if len({unit.unit_id for unit in units}) != len(units):
            raise ValidationFailed("机组编号不能重复")
        return cls(project_id=project_id, units=units)


@dataclass(frozen=True, slots=True)
class CorridorCapability:
    corridor_id: str
    route_id: str
    thermal_limit_mw: Decimal

    @classmethod
    def from_dict(cls, raw: object) -> "CorridorCapability":
        if not isinstance(raw, Mapping):
            raise ValidationFailed("corridors 元素必须是对象")
        return cls(
            corridor_id=identifier(raw.get("corridor_id"), "corridors.corridor_id"),
            route_id=identifier(raw.get("route_id"), "corridors.route_id"),
            thermal_limit_mw=decimal_value(
                raw.get("thermal_limit_mw"), "corridors.thermal_limit_mw",
                minimum=Decimal("0.001"), maximum=Decimal("10000"),
            ),
        )


@dataclass(frozen=True, slots=True)
class CompensationCapability:
    station_id: str
    reactive_support_mvar: Decimal
    power_factor_limit: Decimal

    @classmethod
    def from_dict(cls, raw: object) -> "CompensationCapability":
        if not isinstance(raw, Mapping):
            raise ValidationFailed("compensation 元素必须是对象")
        return cls(
            station_id=identifier(raw.get("station_id"), "compensation.station_id"),
            reactive_support_mvar=decimal_value(
                raw.get("reactive_support_mvar"), "compensation.reactive_support_mvar",
                minimum=Decimal("0.001"), maximum=Decimal("2000"),
            ),
            power_factor_limit=decimal_value(
                raw.get("power_factor_limit"), "compensation.power_factor_limit",
                minimum=Decimal("0.8"), maximum=Decimal("0.999"),
            ),
        )


@dataclass(frozen=True, slots=True)
class ReserveCapability:
    reserve_id: str
    spinning_reserve_mw: Decimal

    @classmethod
    def from_dict(cls, raw: object) -> "ReserveCapability":
        if not isinstance(raw, Mapping):
            raise ValidationFailed("reserve 元素必须是对象")
        return cls(
            reserve_id=identifier(raw.get("reserve_id"), "reserve.reserve_id"),
            spinning_reserve_mw=decimal_value(
                raw.get("spinning_reserve_mw"), "reserve.spinning_reserve_mw",
                minimum=Decimal("0"), maximum=Decimal("5000"),
            ),
        )


@dataclass(frozen=True, slots=True)
class RampSnapshot:
    """调度人员固定的机组能力、海况观测和通道设施快照。"""

    snapshot_id: str
    command_id: str
    projects: tuple[ProjectCapability, ...]
    sea_state: SeaStateObservation
    corridors: tuple[CorridorCapability, ...]
    compensation: tuple[CompensationCapability, ...]
    reserve: tuple[ReserveCapability, ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RampSnapshot":
        projects_raw = raw.get("projects")
        if not isinstance(projects_raw, list) or not 1 <= len(projects_raw) <= 8:
            raise ValidationFailed("projects 必须包含 1 到 8 个子项目")
        projects = tuple(ProjectCapability.from_dict(item) for item in projects_raw)
        if len({project.project_id for project in projects}) != len(projects):
            raise ValidationFailed("子项目编号不能重复")
        corridors_raw = raw.get("corridors")
        if not isinstance(corridors_raw, list) or not 1 <= len(corridors_raw) <= 8:
            raise ValidationFailed("corridors 必须包含 1 到 8 条送出通道")
        corridors = tuple(CorridorCapability.from_dict(item) for item in corridors_raw)
        if len({corridor.corridor_id for corridor in corridors}) != len(corridors):
            raise ValidationFailed("送出通道编号不能重复")
        compensation_raw = raw.get("compensation")
        if not isinstance(compensation_raw, list) or not 1 <= len(compensation_raw) <= 4:
            raise ValidationFailed("compensation 必须包含 1 到 4 座无功补偿站")
        compensation = tuple(CompensationCapability.from_dict(item) for item in compensation_raw)
        if len({station.station_id for station in compensation}) != len(compensation):
            raise ValidationFailed("无功补偿站编号不能重复")
        reserve_raw = raw.get("reserve")
        if not isinstance(reserve_raw, list) or not 1 <= len(reserve_raw) <= 4:
            raise ValidationFailed("reserve 必须包含 1 到 4 项旋转备用")
        reserve = tuple(ReserveCapability.from_dict(item) for item in reserve_raw)
        if len({item.reserve_id for item in reserve}) != len(reserve):
            raise ValidationFailed("旋转备用编号不能重复")
        return cls(
            snapshot_id=identifier(raw.get("snapshot_id"), "snapshot_id"),
            command_id=identifier(raw.get("command_id"), "command_id"),
            projects=projects,
            sea_state=SeaStateObservation.from_dict(raw.get("sea_state")),
            corridors=corridors,
            compensation=compensation,
            reserve=reserve,
        )


@dataclass(frozen=True, slots=True)
class TrajectoryPoint:
    offset_minutes: int
    target_mw: Decimal

    @classmethod
    def from_dict(cls, raw: object) -> "TrajectoryPoint":
        if not isinstance(raw, Mapping):
            raise ValidationFailed("trajectory 元素必须是对象")
        offset = raw.get("offset_minutes")
        if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 43200:
            raise ValidationFailed("trajectory.offset_minutes 必须是 0 到 43200 的整数")
        return cls(
            offset_minutes=offset,
            target_mw=decimal_value(
                raw.get("target_mw"), "trajectory.target_mw",
                minimum=Decimal("0"), maximum=Decimal("10000"),
            ),
        )


@dataclass(frozen=True, slots=True)
class DeratingRule:
    product: str
    derate_percent: Decimal
    wind_speed_above_mps: Decimal | None
    wave_height_above_m: Decimal | None

    @classmethod
    def from_dict(cls, raw: object) -> "DeratingRule":
        if not isinstance(raw, Mapping):
            raise ValidationFailed("derating.rules 元素必须是对象")
        product = required_text(raw.get("product"), "derating.rules.product", 32)
        if product not in TURBINE_PRODUCTS:
            raise ValidationFailed("derating.rules.product 必须是风电机组类型")
        wind = optional_decimal(
            raw.get("wind_speed_above_mps"), "derating.rules.wind_speed_above_mps",
            minimum=Decimal("0"), maximum=Decimal("70"),
        )
        wave = optional_decimal(
            raw.get("wave_height_above_m"), "derating.rules.wave_height_above_m",
            minimum=Decimal("0"), maximum=Decimal("30"),
        )
        if wind is None and wave is None:
            raise ValidationFailed("降额规则必须包含风速或浪高阈值")
        return cls(
            product=product,
            derate_percent=decimal_value(
                raw.get("derate_percent"), "derating.rules.derate_percent",
                minimum=Decimal("0.1"), maximum=Decimal("95"),
            ),
            wind_speed_above_mps=wind,
            wave_height_above_m=wave,
        )


@dataclass(frozen=True, slots=True)
class DeratingStrategy:
    strategy_id: str
    rules: tuple[DeratingRule, ...]

    @classmethod
    def from_dict(cls, raw: object) -> "DeratingStrategy":
        if not isinstance(raw, Mapping):
            raise ValidationFailed("derating 必须是对象")
        rules_raw = raw.get("rules", [])
        if not isinstance(rules_raw, list) or len(rules_raw) > 16:
            raise ValidationFailed("derating.rules 最多包含 16 条规则")
        return cls(
            strategy_id=identifier(raw.get("strategy_id"), "derating.strategy_id"),
            rules=tuple(DeratingRule.from_dict(item) for item in rules_raw),
        )


@dataclass(frozen=True, slots=True)
class RampPlanRequest:
    plan_id: str
    snapshot_id: str
    trajectory: tuple[TrajectoryPoint, ...]
    derating: DeratingStrategy

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RampPlanRequest":
        trajectory_raw = raw.get("trajectory")
        if not isinstance(trajectory_raw, list) or not 1 <= len(trajectory_raw) <= 96:
            raise ValidationFailed("trajectory 必须包含 1 到 96 个轨迹点")
        trajectory = tuple(TrajectoryPoint.from_dict(item) for item in trajectory_raw)
        for previous, current in zip(trajectory, trajectory[1:]):
            if current.offset_minutes <= previous.offset_minutes:
                raise ValidationFailed("目标功率轨迹时间必须严格递增")
            if current.target_mw < previous.target_mw:
                raise ValidationFailed("目标功率轨迹功率不能下降")
        if trajectory[-1].target_mw <= Decimal("0"):
            raise ValidationFailed("目标功率轨迹末端功率必须大于零")
        return cls(
            plan_id=identifier(raw.get("plan_id"), "plan_id"),
            snapshot_id=identifier(raw.get("snapshot_id"), "snapshot_id"),
            trajectory=trajectory,
            derating=DeratingStrategy.from_dict(raw.get("derating")),
        )


@dataclass(frozen=True, slots=True)
class RampReceiptRequest:
    receipt_key: str
    phase: str
    actual_mw: Decimal
    note: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RampReceiptRequest":
        phase = required_text(raw.get("phase"), "phase", 32)
        if phase not in RAMP_PHASES:
            raise ValidationFailed("phase 必须是 prepare、trial_send、expand 或 steady")
        note = raw.get("note", "")
        if not isinstance(note, str) or len(note.strip()) > 256:
            raise ValidationFailed("note 不能超过 256 个字符")
        return cls(
            receipt_key=identifier(raw.get("receipt_key"), "receipt_key"),
            phase=phase,
            actual_mw=decimal_value(
                raw.get("actual_mw"), "actual_mw",
                minimum=Decimal("0"), maximum=Decimal("10000"),
            ),
            note=note.strip(),
        )
