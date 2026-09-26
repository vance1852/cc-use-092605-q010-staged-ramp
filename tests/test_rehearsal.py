from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from wind_dispatch.api import JsonApplication
from wind_dispatch.clock import FrozenClock
from wind_dispatch.errors import Conflict, Forbidden, InvalidState, ValidationFailed
from wind_dispatch.rehearsal import (
    build_stage_plan,
    constraint_ceilings,
    power_factor_ratio,
    safe_rollback_target,
    sea_state_factor,
)
from wind_dispatch.service import SupplyService


UNITS = [
    {
        "unit_id": f"WTG-{index:02d}",
        "project": "alpha" if index <= 4 else "beta",
        "rated_mw": "18",
        "available": True,
        "ramp_mw_per_min": "3",
    }
    for index in range(1, 9)
]

CORRIDORS = [
    {"corridor_id": "cable-a", "cable_thermal_limit_mw": "120", "reactive_limit_mvar": "60", "spinning_reserve_mw": "10"},
    {"corridor_id": "cable-b", "cable_thermal_limit_mw": "150", "reactive_limit_mvar": "50", "spinning_reserve_mw": "5"},
]

SEA = {"wind_speed_mps": "12", "wave_height_m": "2"}


def json_body(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")

DERATING = {
    "cable_derate_percent": "0",
    "reactive_derate_percent": "0",
    "min_spinning_reserve_mw": "20",
    "safety_margin_percent": "10",
    "trial_fraction": "0.25",
    "min_power_factor": "0.95",
}

TRAJECTORY = [
    {"offset_minutes": 0, "target_mw": "0"},
    {"offset_minutes": 10, "target_mw": "60"},
    {"offset_minutes": 20, "target_mw": "110"},
]


class RehearsalMathTests(unittest.TestCase):
    def test_sea_state_factor_steps(self) -> None:
        self.assertEqual(sea_state_factor(Decimal("10"), Decimal("1")), Decimal("1"))
        self.assertEqual(sea_state_factor(Decimal("16"), Decimal("1")), Decimal("0.90"))
        self.assertEqual(sea_state_factor(Decimal("22"), Decimal("1")), Decimal("0.70"))
        self.assertEqual(sea_state_factor(Decimal("26"), Decimal("1")), Decimal("0"))
        self.assertEqual(sea_state_factor(Decimal("10"), Decimal("5")), Decimal("0.80"))
        self.assertEqual(sea_state_factor(Decimal("10"), Decimal("7")), Decimal("0.50"))

    def test_power_factor_ratio(self) -> None:
        self.assertEqual(power_factor_ratio(Decimal("0.95")), Decimal("3.042435"))

    def test_ceilings_identify_first_boundary(self) -> None:
        result = constraint_ceilings(units=UNITS, corridors=CORRIDORS, sea_state=SEA, derating=DERATING)
        self.assertEqual(result["overall_ceiling_mw"], Decimal("120.000"))
        self.assertEqual(result["binding_constraints"], ["cable_thermal"])
        ceilings = {item["source_kind"]: item["ceiling_mw"] for item in result["constraints"]}
        self.assertEqual(ceilings["turbine_capability"], Decimal("144.000"))
        self.assertEqual(ceilings["reactive_compensation"], Decimal("152.122"))
        self.assertEqual(ceilings["spinning_reserve"], Decimal("124.000"))
        self.assertEqual(result["pools"]["reactive_compensation"]["capacity"], Decimal("50.000"))

    def test_reactive_boundary_binds_when_cable_is_roomy(self) -> None:
        corridors = [
            {"corridor_id": "cable-a", "cable_thermal_limit_mw": "500", "reactive_limit_mvar": "30", "spinning_reserve_mw": "0"},
        ]
        derating = {**DERATING, "min_spinning_reserve_mw": "0"}
        result = constraint_ceilings(units=UNITS, corridors=corridors, sea_state=SEA, derating=derating)
        self.assertEqual(result["binding_constraints"], ["reactive_compensation"])
        self.assertEqual(result["overall_ceiling_mw"], Decimal("91.273"))

    def test_stage_plan_caps_target_and_names_blockers(self) -> None:
        ceilings = constraint_ceilings(units=UNITS, corridors=CORRIDORS, sea_state=SEA, derating=DERATING)
        plan = build_stage_plan(trajectory=TRAJECTORY, ceilings=ceilings, derating=DERATING)
        stages = {stage["code"]: stage for stage in plan["stages"]}
        self.assertEqual(plan["peak_target_mw"], "110.000")
        self.assertEqual(stages["preparation"]["rollback_margin_mw"], "20.000")
        self.assertEqual(stages["trial_delivery"]["target_mw"], "27.500")
        self.assertTrue(stages["trial_delivery"]["reachable"])
        self.assertEqual(stages["expansion"]["target_mw"], "100.000")
        self.assertEqual(stages["expansion"]["intended_target_mw"], "110.000")
        self.assertFalse(stages["expansion"]["reachable"])
        self.assertEqual(
            [item["source_kind"] for item in stages["expansion"]["blocking_constraints"]],
            ["cable_thermal", "spinning_reserve"],
        )
        self.assertEqual(stages["expansion"]["rollback_margin_mw"], "20.000")
        self.assertEqual(stages["stable_operation"]["target_mw"], "100.000")

    def test_safe_rollback_target(self) -> None:
        ceilings = constraint_ceilings(units=UNITS, corridors=CORRIDORS, sea_state=SEA, derating=DERATING)
        stages = build_stage_plan(trajectory=TRAJECTORY, ceilings=ceilings, derating=DERATING)["stages"]
        target = safe_rollback_target(
            stages=stages, executed_indexes={0, 1}, current_index=2, effective_ceiling_mw=Decimal("50")
        )
        self.assertEqual(target, 1)
        self.assertEqual(
            safe_rollback_target(stages=stages, executed_indexes={0, 1}, current_index=2, effective_ceiling_mw=Decimal("10")),
            -1,
        )
        self.assertEqual(
            safe_rollback_target(stages=stages, executed_indexes={0}, current_index=2, effective_ceiling_mw=Decimal("50")),
            0,
        )
        self.assertEqual(
            safe_rollback_target(stages=stages, executed_indexes={0}, current_index=0, effective_ceiling_mw=Decimal("999")),
            -1,
        )


class RehearsalServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc))
        self.service = SupplyService(self.connection, self.clock)
        for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor")):
            self.service.create_user(user_id, user_id, role)
        self.service.create_rehearsal_snapshot("dispatch", {
            "snapshot_id": "snap-1",
            "label": "调峰指令前运行快照",
            "observed_at": "2026-09-26T07:30:00Z",
            "units": UNITS,
            "sea_state": SEA,
            "corridors": CORRIDORS,
        })

    def tearDown(self) -> None:
        self.connection.close()

    def _plan_payload(self, plan_id: str = "plan-1", key: str = "reh-key-1") -> dict[str, object]:
        return {
            "plan_id": plan_id,
            "snapshot_id": "snap-1",
            "command_id": "peak-001",
            "target_trajectory": TRAJECTORY,
            "derating": DERATING,
            "idempotency_key": key,
        }

    def _create_plan(self, plan_id: str = "plan-1", key: str = "reh-key-1") -> dict[str, object]:
        return self.service.create_rehearsal_plan("dispatch", self._plan_payload(plan_id, key))

    def _confirm(self, plan_id: str = "plan-1") -> dict[str, object]:
        return self.service.confirm_rehearsal_plan("dispatch", plan_id, 1)

    def _execute_current(self, plan_id: str, receipt_id: str) -> dict[str, object]:
        status = self.service.rehearsal_plan_status("dispatch", plan_id)
        return self.service.record_rehearsal_receipt("dispatch", plan_id, {
            "receipt_id": receipt_id,
            "stage_index": status["current_stage"]["index"],
            "outcome": "executed",
            "detail": {"operator": "offshore-shift"},
        })

    def _advance(self, plan_id: str) -> dict[str, object]:
        status = self.service.rehearsal_plan_status("dispatch", plan_id)
        return self.service.advance_rehearsal_plan("dispatch", plan_id, status["revision"])

    def test_plan_creation_is_deterministic_and_idempotent(self) -> None:
        plan = self._create_plan()
        self.assertEqual(plan["state"], "draft")
        self.assertEqual(plan["ceiling_mw"], "120.000")
        self.assertEqual(plan["binding_constraints"], ["cable_thermal"])
        self.assertEqual([stage["code"] for stage in plan["stages"]], ["preparation", "trial_delivery", "expansion", "stable_operation"])
        replay = self._create_plan()
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["plan_id"], plan["plan_id"])
        changed = self._plan_payload()
        changed["command_id"] = "peak-002"
        with self.assertRaises(Conflict):
            self.service.create_rehearsal_plan("dispatch", changed)

    def test_full_lifecycle_completes_and_releases_capacity(self) -> None:
        self._create_plan()
        confirmed = self._confirm()
        self.assertEqual(confirmed["current_stage_index"], 0)
        reservations = {item["source_kind"]: item for item in confirmed["reservations"]}
        self.assertEqual(reservations["turbine_capability"]["amount"], "120.000")
        self.assertEqual(reservations["cable_thermal"]["amount"], "100.000")
        self.assertEqual(reservations["spinning_reserve"]["amount"], "20.000")
        self.assertEqual(reservations["reactive_compensation"]["unit"], "MVAR")
        for stage in range(4):
            receipt = self._execute_current("plan-1", f"rcpt-{stage}")
            self.assertFalse(receipt["replayed"])
            if stage < 3:
                advanced = self._advance("plan-1")
                self.assertEqual(advanced["current_stage_index"], stage + 1)
        status = self.service.rehearsal_plan_status("audit", "plan-1")
        self.assertEqual(status["state"], "completed")
        self.assertEqual(status["current_stage"]["code"], "stable_operation")
        self.assertTrue(status["current_stage"]["executed"])
        self.assertTrue(all(item["state"] == "released" for item in status["capacity_sources"]))
        self.assertEqual(len(status["events"]), 9)
        self.assertTrue(self.service.audit_chain("audit")["valid"])

    def test_duplicate_receipt_counts_only_once(self) -> None:
        self._create_plan()
        self._confirm()
        body = {"receipt_id": "rcpt-1", "stage_index": 0, "outcome": "executed", "detail": {"operator": "a"}}
        first = self.service.record_rehearsal_receipt("dispatch", "plan-1", body)
        second = self.service.record_rehearsal_receipt("dispatch", "plan-1", body)
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["plan_revision"], second["plan_revision"])
        rows = self.connection.execute("SELECT * FROM rehearsal_receipts WHERE plan_id='plan-1'").fetchall()
        self.assertEqual(len(rows), 1)
        with self.assertRaises(Conflict):
            self.service.record_rehearsal_receipt("dispatch", "plan-1", {**body, "detail": {"operator": "b"}})
        with self.assertRaises(Conflict):
            self.service.record_rehearsal_receipt("dispatch", "plan-1", {"receipt_id": "rcpt-2", "stage_index": 0, "outcome": "executed"})

    def test_advance_requires_receipt_and_matching_revision(self) -> None:
        self._create_plan()
        self._confirm()
        with self.assertRaises(InvalidState):
            self.service.advance_rehearsal_plan("dispatch", "plan-1", 2)
        self._execute_current("plan-1", "rcpt-0")
        with self.assertRaises(InvalidState):
            self.service.advance_rehearsal_plan("dispatch", "plan-1", 2)
        advanced = self.service.advance_rehearsal_plan("dispatch", "plan-1", 3)
        self.assertEqual(advanced["current_stage"], "trial_delivery")

    def test_stale_snapshot_blocks_confirm_and_advance(self) -> None:
        self._create_plan()
        self._create_plan("plan-2", "reh-key-2")
        self._confirm()
        self._execute_current("plan-1", "rcpt-0")
        self.service.register_source_update("dispatch", "sea_state", "新风浪观测到站")
        with self.assertRaises(InvalidState):
            self._advance("plan-1")
        with self.assertRaises(InvalidState):
            self.service.confirm_rehearsal_plan("dispatch", "plan-2", 1)
        with self.assertRaises(InvalidState):
            self._create_plan("plan-3", "reh-key-3")
        status = self.service.rehearsal_plan_status("risk", "plan-1")
        self.assertTrue(status["snapshot"]["stale"])
        self.assertEqual(status["snapshot"]["current_revisions"]["sea_state"], 1)
        self.assertIn("snapshot_stale", [item["code"] for item in status["blocking_constraints"]])
        fresh = self.service.create_rehearsal_snapshot("dispatch", {
            "snapshot_id": "snap-2",
            "label": "更新后快照",
            "observed_at": "2026-09-26T08:30:00Z",
            "units": UNITS,
            "sea_state": {**SEA, "wind_speed_mps": "13"},
            "corridors": CORRIDORS,
        })
        self.assertEqual(fresh["source_revisions"]["sea_state"], 1)

    def test_rollback_preserves_evidence_and_retreats_to_safe_stage(self) -> None:
        self._create_plan()
        self._confirm()
        for stage in range(3):
            self._execute_current("plan-1", f"rcpt-{stage}")
            self._advance("plan-1")
        with self.assertRaises(Forbidden):
            self.service.rollback_rehearsal_plan("dispatch", "plan-1", {"reason": "海缆温度越限"})
        result = self.service.rollback_rehearsal_plan("risk", "plan-1", {
            "reason": "海缆温度越限",
            "ceiling_override_mw": "50",
        })
        self.assertEqual(result["current_stage_index"], 1)
        self.assertEqual(result["rolled_back_from"], 3)
        status = self.service.rehearsal_plan_status("dispatch", "plan-1")
        receipts = {row["receipt_id"]: row["state"] for row in status["receipts"]}
        self.assertEqual(receipts["rcpt-2"], "superseded")
        self.assertEqual(receipts["rcpt-0"], "counted")
        self.assertEqual(receipts["rcpt-1"], "counted")
        event_types = [event["event_type"] for event in status["events"]]
        self.assertIn("retreated", event_types)
        self.assertIn("rolled_back", event_types)
        self.assertEqual(status["rollback"]["target_stage_index"], 0)
        # 回退后可重新推进：试送回执仍生效，扩容需重新执行
        self._advance("plan-1")
        self._execute_current("plan-1", "rcpt-2b")
        self._advance("plan-1")
        self._execute_current("plan-1", "rcpt-3")
        final = self.service.rehearsal_plan_status("dispatch", "plan-1")
        self.assertEqual(final["state"], "completed")

    def test_rollback_to_unsafe_target_is_rejected(self) -> None:
        self._create_plan()
        self._confirm()
        self._execute_current("plan-1", "rcpt-0")
        self._advance("plan-1")
        with self.assertRaises(InvalidState):
            self.service.rollback_rehearsal_plan("risk", "plan-1", {"reason": "x", "to_stage_index": 2})
        result = self.service.rollback_rehearsal_plan("risk", "plan-1", {"reason": "机组脱网", "to_stage_index": 0})
        self.assertEqual(result["current_stage_index"], 0)
        status = self.service.rehearsal_plan_status("dispatch", "plan-1")
        self.assertEqual(status["rollback"]["target_stage_index"], -1)
        self.assertEqual(status["rollback"]["target_stage"], "abort")

    def test_abort_releases_reservations(self) -> None:
        self._create_plan()
        self._confirm()
        result = self.service.rollback_rehearsal_plan("risk", "plan-1", {"reason": "指令撤销"})
        self.assertEqual(result["state"], "aborted")
        self.assertEqual(result["current_stage_index"], -1)
        status = self.service.rehearsal_plan_status("dispatch", "plan-1")
        self.assertIsNone(status["current_stage"])
        self.assertTrue(all(item["state"] == "released" for item in status["capacity_sources"]))
        with self.assertRaises(InvalidState):
            self._advance("plan-1")

    def test_capacity_pool_conflict_between_plans(self) -> None:
        self._create_plan()
        self._create_plan("plan-2", "reh-key-2")
        self._confirm()
        with self.assertRaises(Conflict):
            self.service.confirm_rehearsal_plan("dispatch", "plan-2", 1)

    def test_permissions_and_ramp_validation(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.create_rehearsal_snapshot("plan", {
                "snapshot_id": "snap-x",
                "label": "x",
                "observed_at": "2026-09-26T07:30:00Z",
                "units": UNITS,
                "sea_state": SEA,
                "corridors": CORRIDORS,
            })
        payload = self._plan_payload("plan-ramp", "reh-key-ramp")
        payload["target_trajectory"] = [
            {"offset_minutes": 0, "target_mw": "0"},
            {"offset_minutes": 1, "target_mw": "100"},
        ]
        with self.assertRaises(ValidationFailed):
            self.service.create_rehearsal_plan("dispatch", payload)
        with self.assertRaises(Forbidden):
            self.service.rehearsal_plan_status("plan", "plan-1")

    def test_api_routes_expose_rehearsal_flow(self) -> None:
        app = JsonApplication(self.service)
        headers = {"X-Actor-Id": "dispatch"}
        snapshot = app.handle("POST", "/rehearsal/snapshots", headers, json_body({
            "snapshot_id": "snap-api",
            "label": "接口快照",
            "observed_at": "2026-09-26T07:30:00Z",
            "units": UNITS,
            "sea_state": SEA,
            "corridors": CORRIDORS,
        }))
        self.assertEqual(snapshot.status, 201)
        plan = app.handle("POST", "/rehearsal/plans", headers, json_body({
            "plan_id": "plan-api",
            "snapshot_id": "snap-api",
            "command_id": "peak-api",
            "target_trajectory": TRAJECTORY,
            "derating": DERATING,
            "idempotency_key": "reh-key-api",
        }))
        self.assertEqual(plan.status, 201)
        confirmed = app.handle("POST", "/rehearsal/plans/plan-api/confirm", headers, json_body({"expected_revision": 1}))
        self.assertEqual(confirmed.status, 200)
        receipt = app.handle("POST", "/rehearsal/plans/plan-api/receipts", headers, json_body({
            "receipt_id": "rcpt-api-0",
            "stage_index": 0,
            "outcome": "executed",
        }))
        self.assertEqual(receipt.status, 201)
        status = app.handle("GET", "/rehearsal/plans/plan-api", {"X-Actor-Id": "audit"})
        self.assertEqual(status.status, 200)
        for key in ("current_stage", "capacity_sources", "blocking_constraints", "rollback"):
            self.assertIn(key, status.body)
        self.assertEqual(status.body["current_stage"]["code"], "preparation")
        self.assertEqual(len(status.body["capacity_sources"]), 4)
        self.assertEqual(status.body["rollback"]["target_stage"], "abort")
        missing = app.handle("GET", "/rehearsal/plans/none", headers)
        self.assertEqual(missing.status, 404)


if __name__ == "__main__":
    unittest.main()
