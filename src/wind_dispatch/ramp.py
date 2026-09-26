"""调峰指令目标功率轨迹的分阶段计划与容量边界计算。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Mapping, Sequence

from .errors import ValidationFailed
from .models import RAMP_PHASES, DeratingStrategy, RampSnapshot, TrajectoryPoint


ZERO = Decimal("0")
HUNDRED = Decimal("100")
MAX_DERATE_PERCENT = Decimal("95")
MW_QUANTUM = Decimal("0.001")
FACTOR_QUANTUM = Decimal("0.0001")
MINUTE_QUANTUM = Decimal("0.1")

PHASE_TITLES = {"prepare": "准备", "trial_send": "试送", "expand": "扩容", "steady": "稳定运行"}
PHASE_FRACTIONS = (Decimal("0"), Decimal("0.3"), Decimal("0.7"), Decimal("1"))
SOURCE_KINDS = ("unit_capability", "corridor_thermal", "reactive_support", "spinning_reserve")
SOURCE_TITLES = {
    "unit_capability": "机组能力",
    "corridor_thermal": "海缆热容量",
    "reactive_support": "无功补偿",
    "spinning_reserve": "旋转备用",
}


def quantize_mw(value: Decimal) -> Decimal:
    return value.quantize(MW_QUANTUM, rounding=ROUND_HALF_UP)


def mw_text(value: Decimal) -> str:
    return format(quantize_mw(value), "f")


def reactive_factor(power_factor: Decimal) -> Decimal:
    """按功率因数下限计算每兆瓦有功所需的无功支撑（Mvar/MW）。"""
    ratio = (Decimal(1) - power_factor * power_factor).sqrt() / power_factor
    return ratio.quantize(FACTOR_QUANTUM, rounding=ROUND_HALF_UP)


def derate_percent_by_product(
    snapshot: RampSnapshot,
    derating: DeratingStrategy,
) -> dict[str, Decimal]:
    """按海况观测逐机型汇总降额策略中生效的降额百分比。"""
    products = sorted({unit.product for project in snapshot.projects for unit in project.units})
    result: dict[str, Decimal] = {}
    for product in products:
        total = ZERO
        for rule in derating.rules:
            if rule.product != product:
                continue
            wind_hit = (
                rule.wind_speed_above_mps is None
                or snapshot.sea_state.wind_speed_mps > rule.wind_speed_above_mps
            )
            wave_hit = (
                rule.wave_height_above_m is None
                or snapshot.sea_state.wave_height_m > rule.wave_height_above_m
            )
            if wind_hit and wave_hit:
                total += rule.derate_percent
        result[product] = min(total, MAX_DERATE_PERCENT)
    return result


@dataclass(frozen=True, slots=True)
class CapacityTotals:
    """快照中四类容量来源的可用量与机组爬坡能力汇总。"""

    unit_mw: Decimal
    thermal_mw: Decimal
    reactive_mvar: Decimal
    reserve_mw: Decimal
    ramp_mw_per_min: Decimal
    power_factor_limit: Decimal
    reactive_factor: Decimal
    projects: tuple[Mapping[str, object], ...]
    derating: tuple[Mapping[str, str], ...]


def capacity_totals(snapshot: RampSnapshot, derating: DeratingStrategy) -> CapacityTotals:
    derate_by_product = derate_percent_by_product(snapshot, derating)
    projects: list[Mapping[str, object]] = []
    unit_total = ZERO
    ramp_total = ZERO
    for project in snapshot.projects:
        raw_total = ZERO
        effective = ZERO
        ready_units = 0
        for unit in project.units:
            if not unit.ready:
                continue
            ready_units += 1
            raw_total += unit.available_mw
            effective += unit.available_mw * (HUNDRED - derate_by_product[unit.product]) / HUNDRED
            ramp_total += unit.ramp_mw_per_min
        unit_total += effective
        projects.append({
            "project_id": project.project_id,
            "ready_units": ready_units,
            "available_mw": mw_text(raw_total),
            "effective_mw": mw_text(effective),
        })
    thermal_total = sum((corridor.thermal_limit_mw for corridor in snapshot.corridors), ZERO)
    reactive_total = sum((station.reactive_support_mvar for station in snapshot.compensation), ZERO)
    reserve_total = sum((item.spinning_reserve_mw for item in snapshot.reserve), ZERO)
    power_factor = min(station.power_factor_limit for station in snapshot.compensation)
    return CapacityTotals(
        unit_mw=quantize_mw(unit_total),
        thermal_mw=quantize_mw(thermal_total),
        reactive_mvar=quantize_mw(reactive_total),
        reserve_mw=quantize_mw(reserve_total),
        ramp_mw_per_min=ramp_total,
        power_factor_limit=power_factor,
        reactive_factor=reactive_factor(power_factor),
        projects=tuple(projects),
        derating=tuple(
            {"product": product, "derate_percent": format(derate_by_product[product], "f")}
            for product in sorted(derate_by_product)
        ),
    )


def entry_conditions(
    totals: CapacityTotals,
    target_mw: Decimal,
    margin_mw: Decimal,
) -> list[dict[str, object]]:
    """计算进入某一阶段前四类容量来源必须满足的条件。"""
    required_mvar = quantize_mw(target_mw * totals.reactive_factor)
    available = {
        "unit_capability": totals.unit_mw,
        "corridor_thermal": totals.thermal_mw,
        "reactive_support": totals.reactive_mvar,
        "spinning_reserve": totals.reserve_mw,
    }
    required = {
        "unit_capability": (target_mw, "MW"),
        "corridor_thermal": (target_mw, "MW"),
        "reactive_support": (required_mvar, "Mvar"),
        "spinning_reserve": (margin_mw, "MW"),
    }
    conditions: list[dict[str, object]] = []
    for kind in SOURCE_KINDS:
        needed, unit = required[kind]
        conditions.append({
            "kind": kind,
            "title": SOURCE_TITLES[kind],
            "unit": unit,
            "required": mw_text(needed),
            "available": mw_text(available[kind]),
            "satisfied": available[kind] >= needed,
        })
    return conditions


def check_trajectory_slope(trajectory: Sequence[TrajectoryPoint], ramp_mw_per_min: Decimal) -> None:
    if ramp_mw_per_min <= ZERO:
        raise ValidationFailed("快照中没有可用机组爬坡能力")
    for previous, current in zip(trajectory, trajectory[1:]):
        rise = current.target_mw - previous.target_mw
        if rise <= ZERO:
            continue
        minutes = Decimal(current.offset_minutes - previous.offset_minutes)
        if rise / minutes > ramp_mw_per_min:
            raise ValidationFailed("目标功率轨迹爬坡速率超过机组爬坡能力")


def _split_reservation(
    kind: str,
    unit: str,
    total: Decimal,
    sources: Sequence[tuple[str, Decimal]],
) -> list[dict[str, str]]:
    """把某类容量的预留总量按来源能力占比分摊，末位来源吸收舍入差。"""
    capacity = sum((cap for _, cap in sources), ZERO)
    rows: list[dict[str, str]] = []
    allocated = ZERO
    ordered = sorted(sources)
    for index, (source_id, cap) in enumerate(ordered):
        if index == len(ordered) - 1:
            share = max(ZERO, quantize_mw(total - allocated))
        elif capacity > ZERO:
            share = quantize_mw(total * cap / capacity)
        else:
            share = ZERO
        allocated = quantize_mw(allocated + share)
        rows.append({
            "source_kind": kind,
            "source_id": source_id,
            "reserved_amount": mw_text(share),
            "unit": unit,
        })
    return rows


def reservation_split(
    snapshot: RampSnapshot,
    totals: CapacityTotals,
    peak_mw: Decimal,
    max_margin_mw: Decimal,
) -> list[dict[str, str]]:
    """确认计划时需要预留的容量，按来源逐项拆分。"""
    rows: list[dict[str, str]] = []
    rows += _split_reservation(
        "unit_capability", "MW", quantize_mw(peak_mw),
        [(str(item["project_id"]), Decimal(str(item["effective_mw"]))) for item in totals.projects],
    )
    rows += _split_reservation(
        "corridor_thermal", "MW", quantize_mw(peak_mw),
        [(corridor.corridor_id, corridor.thermal_limit_mw) for corridor in snapshot.corridors],
    )
    rows += _split_reservation(
        "reactive_support", "Mvar", quantize_mw(peak_mw * totals.reactive_factor),
        [(station.station_id, station.reactive_support_mvar) for station in snapshot.compensation],
    )
    rows += _split_reservation(
        "spinning_reserve", "MW", quantize_mw(max_margin_mw),
        [(item.reserve_id, item.spinning_reserve_mw) for item in snapshot.reserve],
    )
    return rows


def reservation_blocking(
    sources: Sequence[Mapping[str, object]],
    requirements: Mapping[str, Mapping[str, str]],
) -> list[dict[str, object]]:
    """比较预留需求与快照可用量，列出不足以预留的容量来源。"""
    available_by_kind = {str(source["kind"]): Decimal(str(source["available"])) for source in sources}
    blocking: list[dict[str, object]] = []
    for kind in SOURCE_KINDS:
        requirement = requirements[kind]
        required = Decimal(requirement["amount"])
        available = available_by_kind[kind]
        if available < required:
            blocking.append({
                "kind": kind,
                "title": SOURCE_TITLES[kind],
                "unit": requirement["unit"],
                "required": requirement["amount"],
                "available": mw_text(available),
                "message": f"{SOURCE_TITLES[kind]}不足以完成预留",
            })
    return blocking


def build_phase_plan(
    snapshot: RampSnapshot,
    trajectory: Sequence[TrajectoryPoint],
    derating: DeratingStrategy,
) -> dict[str, object]:
    """把目标功率轨迹切成准备、试送、扩容、稳定运行四个可执行阶段。"""
    totals = capacity_totals(snapshot, derating)
    check_trajectory_slope(trajectory, totals.ramp_mw_per_min)
    peak = trajectory[-1].target_mw
    targets = [quantize_mw(peak * fraction) for fraction in PHASE_FRACTIONS]
    phases: list[dict[str, object]] = []
    previous = ZERO
    for index, (name, target) in enumerate(zip(RAMP_PHASES, targets)):
        margin = quantize_mw(target - previous)
        conditions = entry_conditions(totals, target, margin)
        ramp_minutes: str | None
        if margin == ZERO:
            ramp_minutes = "0.0"
        elif totals.ramp_mw_per_min > ZERO:
            ramp_minutes = format(
                (margin / totals.ramp_mw_per_min).quantize(MINUTE_QUANTUM, rounding=ROUND_HALF_UP), "f"
            )
        else:
            ramp_minutes = None
        phases.append({
            "phase": name,
            "sequence": index,
            "title": PHASE_TITLES[name],
            "target_mw": mw_text(target),
            "rollback_margin_mw": mw_text(margin),
            "rollback_to": None if index == 0 else RAMP_PHASES[index - 1],
            "ramp_minutes": ramp_minutes,
            "entry_conditions": conditions,
            "satisfied": all(condition["satisfied"] for condition in conditions),
        })
        previous = target
    factor = totals.reactive_factor
    reactive_ceiling = ZERO if factor <= ZERO else quantize_mw(totals.reactive_mvar / factor)
    ceilings = [
        {"kind": "unit_capability", "ceiling_mw": mw_text(totals.unit_mw)},
        {"kind": "corridor_thermal", "ceiling_mw": mw_text(totals.thermal_mw)},
        {"kind": "reactive_support", "ceiling_mw": mw_text(reactive_ceiling)},
    ]
    ceilings.sort(key=lambda item: (Decimal(item["ceiling_mw"]), item["kind"]))
    lowest = Decimal(ceilings[0]["ceiling_mw"])
    binding = [item["kind"] for item in ceilings if Decimal(item["ceiling_mw"]) == lowest]
    first_blocked = next((phase for phase in phases if not phase["satisfied"]), None)
    first_boundary = {
        "phase": None if first_blocked is None else first_blocked["phase"],
        "constraints": []
        if first_blocked is None
        else [
            str(condition["kind"])
            for condition in first_blocked["entry_conditions"]  # type: ignore[index]
            if not condition["satisfied"]  # type: ignore[index]
        ],
        "binding_sources": binding,
        "ceilings": ceilings,
    }
    max_margin = max(Decimal(str(phase["rollback_margin_mw"])) for phase in phases)
    requirements = {
        "unit_capability": {"amount": mw_text(peak), "unit": "MW"},
        "corridor_thermal": {"amount": mw_text(peak), "unit": "MW"},
        "reactive_support": {"amount": mw_text(peak * factor), "unit": "Mvar"},
        "spinning_reserve": {"amount": mw_text(max_margin), "unit": "MW"},
    }
    sources = [
        {
            "kind": "unit_capability",
            "title": SOURCE_TITLES["unit_capability"],
            "unit": "MW",
            "available": mw_text(totals.unit_mw),
            "details": list(totals.projects),
        },
        {
            "kind": "corridor_thermal",
            "title": SOURCE_TITLES["corridor_thermal"],
            "unit": "MW",
            "available": mw_text(totals.thermal_mw),
            "details": [
                {
                    "corridor_id": corridor.corridor_id,
                    "route_id": corridor.route_id,
                    "thermal_limit_mw": mw_text(corridor.thermal_limit_mw),
                }
                for corridor in snapshot.corridors
            ],
        },
        {
            "kind": "reactive_support",
            "title": SOURCE_TITLES["reactive_support"],
            "unit": "Mvar",
            "available": mw_text(totals.reactive_mvar),
            "details": [
                {
                    "station_id": station.station_id,
                    "reactive_support_mvar": mw_text(station.reactive_support_mvar),
                    "power_factor_limit": format(station.power_factor_limit, "f"),
                }
                for station in snapshot.compensation
            ],
        },
        {
            "kind": "spinning_reserve",
            "title": SOURCE_TITLES["spinning_reserve"],
            "unit": "MW",
            "available": mw_text(totals.reserve_mw),
            "details": [
                {
                    "reserve_id": item.reserve_id,
                    "spinning_reserve_mw": mw_text(item.spinning_reserve_mw),
                }
                for item in snapshot.reserve
            ],
        },
    ]
    capacity = {
        "sources": sources,
        "requirements": requirements,
        "reservation_split": reservation_split(snapshot, totals, peak, max_margin),
        "first_boundary": first_boundary,
        "reactive_factor": format(factor, "f"),
        "power_factor_limit": format(totals.power_factor_limit, "f"),
        "derating": list(totals.derating),
        "aggregate_ramp_mw_per_min": format(totals.ramp_mw_per_min, "f"),
    }
    return {
        "peak_target_mw": mw_text(peak),
        "phases": phases,
        "capacity": capacity,
    }
