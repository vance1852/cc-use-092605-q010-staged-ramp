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
    units = [
        {"unit_id": f"WTG-{project}{index:02d}", "project": project, "rated_mw": "18", "available": True, "ramp_mw_per_min": "3"}
        for project in ("alpha", "beta")
        for index in range(1, 57)
    ]
    service.create_rehearsal_snapshot("dispatch", {
        "snapshot_id": "snap-peak-001",
        "label": "调峰指令前运行快照",
        "observed_at": "2026-09-24T07:30:00Z",
        "units": units,
        "sea_state": {"wind_speed_mps": "12.5", "wave_height_m": "2.1"},
        "corridors": [
            {"corridor_id": "export-alpha", "cable_thermal_limit_mw": "1200", "reactive_limit_mvar": "400", "spinning_reserve_mw": "80"},
            {"corridor_id": "export-beta", "cable_thermal_limit_mw": "1200", "reactive_limit_mvar": "400", "spinning_reserve_mw": "70"},
        ],
    })
    plan = service.create_rehearsal_plan("dispatch", {
        "plan_id": "rehearsal-peak-001",
        "snapshot_id": "snap-peak-001",
        "command_id": "peak-2026-09-26",
        "target_trajectory": [
            {"offset_minutes": 0, "target_mw": "0"},
            {"offset_minutes": 30, "target_mw": "600"},
            {"offset_minutes": 60, "target_mw": "1200"},
            {"offset_minutes": 120, "target_mw": "1800"},
        ],
        "derating": {
            "cable_derate_percent": "5",
            "reactive_derate_percent": "10",
            "min_spinning_reserve_mw": "150",
            "safety_margin_percent": "5",
            "trial_fraction": "0.25",
            "min_power_factor": "0.95",
        },
        "idempotency_key": "rehearsal-key-001",
    })
    service.confirm_rehearsal_plan("dispatch", "rehearsal-peak-001", 1)
    for stage in range(4):
        service.record_rehearsal_receipt("dispatch", "rehearsal-peak-001", {"receipt_id": f"rcpt-{stage}", "stage_index": stage, "outcome": "executed", "detail": {"operator": "offshore-shift"}})
        if stage < 3:
            status = service.rehearsal_plan_status("dispatch", "rehearsal-peak-001")
            service.advance_rehearsal_plan("dispatch", "rehearsal-peak-001", status["revision"])
    rehearsal = service.rehearsal_plan_status("dispatch", "rehearsal-peak-001")
    result = {"status": "ok", "price": service.price_summary("PEAK_VALLEY"), "allocation_id": allocation["allocation_id"], "transfer": transfer, "scenario_run_id": scenario["run_id"], "rehearsal": {"plan_id": rehearsal["plan_id"], "state": rehearsal["state"], "current_stage": rehearsal["current_stage"]["code"], "ceiling_mw": rehearsal["ceiling_mw"], "binding_constraints": rehearsal["binding_constraints"], "capacity_sources": len(rehearsal["capacity_sources"]), "stages": [{"code": stage["code"], "target_mw": stage["target_mw"], "rollback_margin_mw": stage["rollback_margin_mw"]} for stage in rehearsal["stages"]]}, "audit": service.audit_chain("audit"), "workspace": workspace.name}
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
