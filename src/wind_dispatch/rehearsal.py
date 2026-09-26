"""调度预演阶段计划的确定性计算：约束上限、阶段划分、容量预留与回退目标。"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Mapping, Sequence

ZERO = Decimal("0")
HUNDRED = Decimal("100")

REVISION_KINDS = ("units", "sea_state", "corridors")
STAGE_CODES = ("preparation", "trial_delivery", "expansion", "stable_operation")
STAGE_LABELS = {
    "preparation": "准备",
    "trial_delivery": "试送",
    "expansion": "扩容",
    "stable_operation": "稳定运行",
}
CONSTRAINT_KINDS = ("turbine_capability", "cable_thermal", "reactive_compensation", "spinning_reserve")


def quantize_value(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


def _text(value: Decimal) -> str:
    return format(value, "f")


def sea_state_factor(wind_speed_mps: Decimal, wave_height_m: Decimal) -> Decimal:
    """海况对机组能力的降额因子，取风速与浪高两者中更严格的限制。"""
    if wind_speed_mps >= Decimal("25"):
        wind = Decimal("0")
    elif wind_speed_mps >= Decimal("20"):
        wind = Decimal("0.70")
    elif wind_speed_mps >= Decimal("15"):
        wind = Decimal("0.90")
    else:
        wind = Decimal("1")
    if wave_height_m >= Decimal("6"):
        wave = Decimal("0.50")
    elif wave_height_m >= Decimal("4"):
        wave = Decimal("0.80")
    elif wave_height_m >= Decimal("2.5"):
        wave = Decimal("0.90")
    else:
        wave = Decimal("1")
    return min(wind, wave)


def power_factor_ratio(min_power_factor: Decimal) -> Decimal:
    """单位无功可支撑的有功功率：pf / sin(arccos pf)。"""
    sine = (Decimal(1) - min_power_factor * min_power_factor).sqrt()
    return (min_power_factor / sine).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


def total_ramp_mw_per_min(units: Sequence[Mapping[str, object]]) -> Decimal:
    return sum((_decimal(unit["ramp_mw_per_min"]) for unit in units if unit["available"]), ZERO)


def constraint_ceilings(
    *,
    units: Sequence[Mapping[str, object]],
    corridors: Sequence[Mapping[str, object]],
    sea_state: Mapping[str, object],
    derating: Mapping[str, object],
) -> dict[str, object]:
    """计算机组能力、海缆热容量、无功补偿和旋转备用四条边界的有功上限。"""
    factor = sea_state_factor(_decimal(sea_state["wind_speed_mps"]), _decimal(sea_state["wave_height_m"]))
    available = [unit for unit in units if unit["available"]]
    capability = quantize_value(sum((_decimal(unit["rated_mw"]) for unit in available), ZERO) * factor)
    cable_derate = _decimal(derating["cable_derate_percent"]) / HUNDRED
    reactive_derate = _decimal(derating["reactive_derate_percent"]) / HUNDRED
    ratio = power_factor_ratio(_decimal(derating["min_power_factor"]))
    ordered = sorted(corridors, key=lambda item: str(item["corridor_id"]))
    cable_rows = [
        (
            str(corridor["corridor_id"]),
            _decimal(corridor["cable_thermal_limit_mw"]),
            quantize_value(_decimal(corridor["cable_thermal_limit_mw"]) * (1 - cable_derate)),
        )
        for corridor in ordered
    ]
    reactive_rows = [
        (
            str(corridor["corridor_id"]),
            _decimal(corridor["reactive_limit_mvar"]),
            quantize_value(_decimal(corridor["reactive_limit_mvar"]) * ratio * (1 - reactive_derate)),
        )
        for corridor in ordered
    ]
    cable_id, cable_limit, cable_ceiling = min(cable_rows, key=lambda item: (item[2], item[0]))
    reactive_id, reactive_limit, reactive_ceiling = min(reactive_rows, key=lambda item: (item[2], item[0]))
    required_reserve = max(
        _decimal(derating["min_spinning_reserve_mw"]),
        sum((_decimal(corridor["spinning_reserve_mw"]) for corridor in ordered), ZERO),
    )
    reserve_ceiling = quantize_value(max(ZERO, capability - required_reserve))
    constraints = [
        {
            "source_kind": "turbine_capability",
            "ceiling_mw": capability,
            "unit": "MW",
            "basis": f"{len(available)} 台可用机组（共 {len(units)} 台）× 海况因子 {factor}",
        },
        {
            "source_kind": "cable_thermal",
            "ceiling_mw": cable_ceiling,
            "unit": "MW",
            "basis": f"海缆 {cable_id} 热容量 {cable_limit}MW，降额 {derating['cable_derate_percent']}%",
        },
        {
            "source_kind": "reactive_compensation",
            "ceiling_mw": reactive_ceiling,
            "unit": "MW",
            "basis": f"500kV 补偿站 {reactive_id} 无功 {reactive_limit}Mvar × 功率因数比 {ratio}，降额 {derating['reactive_derate_percent']}%",
        },
        {
            "source_kind": "spinning_reserve",
            "ceiling_mw": reserve_ceiling,
            "unit": "MW",
            "basis": f"机组能力 {capability}MW − 旋转备用 {required_reserve}MW",
        },
    ]
    overall = min(item["ceiling_mw"] for item in constraints)
    reactive_capacity = min(
        quantize_value(_decimal(corridor["reactive_limit_mvar"]) * (1 - reactive_derate))
        for corridor in ordered
    )
    return {
        "constraints": constraints,
        "overall_ceiling_mw": overall,
        "binding_constraints": [item["source_kind"] for item in constraints if item["ceiling_mw"] == overall],
        "sea_factor": factor,
        "required_reserve_mw": quantize_value(required_reserve),
        "power_factor_ratio": ratio,
        "pools": {
            "turbine_capability": {"capacity": capability, "unit": "MW"},
            "cable_thermal": {"capacity": cable_ceiling, "unit": "MW"},
            "reactive_compensation": {"capacity": reactive_capacity, "unit": "MVAR"},
        },
    }


def build_stage_plan(
    *,
    trajectory: Sequence[Mapping[str, object]],
    ceilings: Mapping[str, object],
    derating: Mapping[str, object],
) -> dict[str, object]:
    """按目标功率轨迹与约束上限划分准备、试送、扩容、稳定运行四个阶段。"""
    ceiling = ceilings["overall_ceiling_mw"]
    constraints = ceilings["constraints"]
    peak = max(_decimal(point["target_mw"]) for point in trajectory)
    safety = _decimal(derating["safety_margin_percent"]) / HUNDRED
    min_reserve = _decimal(derating["min_spinning_reserve_mw"])
    fraction = _decimal(derating["trial_fraction"])

    def margin(target: Decimal) -> Decimal:
        return quantize_value(max(min_reserve, quantize_value(target * safety)))

    def capped(target: Decimal) -> Decimal:
        return min(target, quantize_value(max(ZERO, ceiling - margin(target))))

    trial_raw = quantize_value(peak * fraction)
    intended = {
        "preparation": ZERO,
        "trial_delivery": trial_raw,
        "expansion": peak,
        "stable_operation": peak,
    }
    actual = {code: quantize_value(capped(intended[code])) for code in STAGE_CODES}
    actual["preparation"] = quantize_value(ZERO)
    stages: list[dict[str, object]] = []
    for index, code in enumerate(STAGE_CODES):
        target = actual[code]
        intended_target = quantize_value(intended[code])
        intended_required = quantize_value(intended_target + margin(intended_target))
        blockers = [
            {
                "source_kind": item["source_kind"],
                "ceiling_mw": _text(item["ceiling_mw"]),
                "required_mw": _text(intended_required),
            }
            for item in constraints
            if item["ceiling_mw"] < intended_required
        ]
        entry_conditions = ["snapshot_fresh", "reservations_active", "headroom_sufficient"]
        if index >= 1:
            entry_conditions.append("previous_stage_executed")
        stages.append({
            "index": index,
            "code": code,
            "label": STAGE_LABELS[code],
            "target_mw": _text(target),
            "intended_target_mw": _text(intended_target),
            "rollback_margin_mw": _text(margin(target)),
            "required_headroom_mw": _text(quantize_value(target + margin(target))),
            "reachable": not blockers,
            "blocking_constraints": blockers,
            "entry_conditions": entry_conditions,
        })
    return {
        "ceiling_mw": _text(ceiling),
        "binding_constraints": list(ceilings["binding_constraints"]),
        "peak_target_mw": _text(quantize_value(peak)),
        "stages": stages,
    }


def required_reservations(
    *,
    stages: Sequence[Mapping[str, object]],
    ceilings: Mapping[str, object],
) -> list[dict[str, str]]:
    """确认计划时需要原子预留的四类容量，按扩容阶段目标计算。"""
    expansion = next(stage for stage in stages if stage["code"] == "expansion")
    target = _decimal(expansion["target_mw"])
    margin = _decimal(expansion["rollback_margin_mw"])
    ratio = ceilings["power_factor_ratio"]
    reserve = ceilings["required_reserve_mw"]
    return [
        {
            "source_kind": "turbine_capability",
            "pool": "turbine_capability",
            "amount": _text(quantize_value(target + margin)),
            "unit": "MW",
        },
        {
            "source_kind": "cable_thermal",
            "pool": "cable_thermal",
            "amount": _text(target),
            "unit": "MW",
        },
        {
            "source_kind": "reactive_compensation",
            "pool": "reactive_compensation",
            "amount": _text(quantize_value(target / ratio)),
            "unit": "MVAR",
        },
        {
            "source_kind": "spinning_reserve",
            "pool": "turbine_capability",
            "amount": _text(reserve),
            "unit": "MW",
        },
    ]


def safe_rollback_target(
    *,
    stages: Sequence[Mapping[str, object]],
    executed_indexes: object,
    current_index: int,
    effective_ceiling_mw: Decimal,
) -> int:
    """异常发生后仍可安全停留的最高阶段；没有安全阶段时返回 -1（全面退出）。"""
    best = -1
    for index in range(0, max(current_index, 0)):
        if index > 0 and index not in executed_indexes:
            continue
        stage = stages[index]
        required = _decimal(stage["target_mw"]) + _decimal(stage["rollback_margin_mw"])
        if required <= effective_ceiling_mw:
            best = index
    return best
