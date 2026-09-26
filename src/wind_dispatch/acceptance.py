"""贯通结算单价、送出通道、机组可用量、提名和情景分析的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import SupplyService


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    service = SupplyService(connection, FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)))
    for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor")):
        service.create_user(user_id, user_id, role)
    for index, close in enumerate(("108", "105", "102", "100", "98", "96"), start=18):
        service.record_quote("plan", {"market_index": "PEAK_VALLEY", "trade_date": f"2026-09-{index}", "close_cny": close, "source_revision": f"rev-{index}", "observed_at": f"2026-09-{index}T21:00:00Z"})
    service.create_facility("plan", {"facility_id": "fanshi-one", "name": "北部海上风电场", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_mwh": "500000"})
    service.create_facility("plan", {"facility_id": "fanshi-two", "name": "帆石二场", "kind": "offshore-station", "timezone": "Asia/Shanghai", "capacity_mwh": "800000"})
    service.create_route("plan", {"route_id": "fanshi-export", "origin_id": "fanshi-one", "destination_id": "fanshi-two", "product": "turbine-18mw", "daily_capacity": "100000", "loss_basis_points": 25, "transit_hours": 36})
    service.add_inventory_lot("dispatch", {"lot_id": "lot-001", "facility_id": "fanshi-one", "product": "turbine-18mw", "grade": "PEAK_VALLEY", "quantity_mwh": "150000", "unit_cost_cny": "91.25", "received_at": "2026-09-24T06:00:00Z"})
    service.submit_nomination("dispatch", {"nomination_id": "nom-001", "route_id": "fanshi-export", "shipper_id": "station-east", "service_date": "2026-09-25", "requested_mwh": "80000", "priority": 10, "idempotency_key": "nom-key-001"})
    allocation = service.allocate("dispatch", "fanshi-export", "2026-09-25")
    transfer = service.dispatch_transfer("dispatch", "transfer-001", "nom-001", "lot-001", 2)
    service.create_scenario("plan", {"scenario_id": "grid-recovery", "name": "关键机组检修恢复与需求回落", "market_index_drop_percent": "9", "route_capacity_changes": {"fanshi-export": "20"}, "demand_changes": {"fanshi-one:turbine-18mw": "-5"}})
    service.approve_scenario("risk", "grid-recovery", 1)
    scenario = service.run_scenario("plan", "grid-recovery", "2026-09-23")
    service.create_ramp_snapshot("dispatch", {
        "snapshot_id": "snap-peak-0926",
        "command_id": "peak-cmd-0926",
        "projects": [
            {"project_id": "alpha", "units": [{"unit_id": f"a-{number:02d}", "product": "turbine-18mw", "available_mw": "18", "ramp_mw_per_min": "3", "ready": True} for number in range(1, 31)]},
            {"project_id": "beta", "units": [{"unit_id": f"b-{number:02d}", "product": "turbine-18mw", "available_mw": "18", "ramp_mw_per_min": "3", "ready": True} for number in range(1, 26)]},
        ],
        "sea_state": {"wind_speed_mps": "11.5", "wave_height_m": "1.4", "observed_at": "2026-09-24T07:00:00Z"},
        "corridors": [{"corridor_id": "cor-export", "route_id": "fanshi-export", "thermal_limit_mw": "1500"}],
        "compensation": [{"station_id": "stat-500kv-east", "reactive_support_mvar": "260", "power_factor_limit": "0.95"}],
        "reserve": [{"reserve_id": "res-spin", "spinning_reserve_mw": "260"}],
    })
    plan = service.create_ramp_plan("dispatch", {
        "plan_id": "ramp-plan-0926",
        "snapshot_id": "snap-peak-0926",
        "trajectory": [
            {"offset_minutes": 0, "target_mw": "0"},
            {"offset_minutes": 10, "target_mw": "180"},
            {"offset_minutes": 25, "target_mw": "420"},
            {"offset_minutes": 40, "target_mw": "600"},
        ],
        "derating": {"strategy_id": "derate-autumn", "rules": [{"product": "turbine-18mw", "wind_speed_above_mps": "10", "derate_percent": "15"}]},
    })
    service.confirm_ramp_plan("dispatch", "ramp-plan-0926", 1)
    service.record_ramp_receipt("dispatch", "ramp-plan-0926", {"receipt_key": "rcpt-prepare", "phase": "prepare", "actual_mw": "0", "note": "准备就绪"})
    service.advance_ramp_plan("dispatch", "ramp-plan-0926", 2)
    service.record_ramp_receipt("dispatch", "ramp-plan-0926", {"receipt_key": "rcpt-trial", "phase": "trial_send", "actual_mw": "181"})
    replayed = service.record_ramp_receipt("dispatch", "ramp-plan-0926", {"receipt_key": "rcpt-trial", "phase": "trial_send", "actual_mw": "181"})
    service.advance_ramp_plan("dispatch", "ramp-plan-0926", 3)
    fallback = service.abort_ramp_plan("risk", "ramp-plan-0926", "海缆温度越限预警", 4)
    service.advance_ramp_plan("dispatch", "ramp-plan-0926", 5)
    service.record_ramp_receipt("dispatch", "ramp-plan-0926", {"receipt_key": "rcpt-expand", "phase": "expand", "actual_mw": "419"})
    service.advance_ramp_plan("dispatch", "ramp-plan-0926", 6)
    service.record_ramp_receipt("dispatch", "ramp-plan-0926", {"receipt_key": "rcpt-steady", "phase": "steady", "actual_mw": "600"})
    completed = service.advance_ramp_plan("dispatch", "ramp-plan-0926", 7)
    ramp = {
        "plan_id": plan["plan_id"],
        "state": completed["state"],
        "phases": len(completed["phases"]),
        "receipt_replayed": replayed["replayed"],
        "fallback_safe_phase": fallback["rollback"]["safe_phase"],
        "reservations_released": all(item["state"] == "released" for item in completed["reservations"]),
    }
    result = {"status": "ok", "price": service.price_summary("PEAK_VALLEY"), "allocation_id": allocation["allocation_id"], "transfer": transfer, "scenario_run_id": scenario["run_id"], "ramp": ramp, "audit": service.audit_chain("audit"), "workspace": workspace.name}
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行海上风电场调度服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
