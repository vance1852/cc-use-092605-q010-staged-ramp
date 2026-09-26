from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from wind_dispatch.acceptance import run as acceptance_run
from wind_dispatch.api import JsonApplication
from wind_dispatch.clock import FrozenClock
from wind_dispatch.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from wind_dispatch.service import SupplyService


ROOT = Path(__file__).resolve().parents[1]


def snapshot_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "snapshot_id": "snap-1",
        "command_id": "cmd-1",
        "projects": [
            {
                "project_id": "alpha",
                "units": [
                    {"unit_id": f"a-{number}", "product": "turbine-18mw", "available_mw": "18", "ramp_mw_per_min": "4", "ready": True}
                    for number in range(20)
                ],
            },
            {
                "project_id": "beta",
                "units": [
                    {"unit_id": f"b-{number}", "product": "turbine-18mw", "available_mw": "18", "ramp_mw_per_min": "4", "ready": True}
                    for number in range(20)
                ],
            },
        ],
        "sea_state": {"wind_speed_mps": "8", "wave_height_m": "1.0", "observed_at": "2026-09-24T07:00:00Z"},
        "corridors": [{"corridor_id": "cor-1", "route_id": "fanshi-export", "thermal_limit_mw": "800"}],
        "compensation": [{"station_id": "stat-1", "reactive_support_mvar": "300", "power_factor_limit": "0.95"}],
        "reserve": [{"reserve_id": "res-1", "spinning_reserve_mw": "300"}],
    }
    payload.update(overrides)
    return payload


def plan_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "plan_id": "plan-1",
        "snapshot_id": "snap-1",
        "trajectory": [
            {"offset_minutes": 0, "target_mw": "0"},
            {"offset_minutes": 10, "target_mw": "180"},
            {"offset_minutes": 25, "target_mw": "420"},
            {"offset_minutes": 40, "target_mw": "600"},
        ],
        "derating": {"strategy_id": "d-1", "rules": []},
    }
    payload.update(overrides)
    return payload


class RampPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = SupplyService(self.connection, self.clock)
        for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor")):
            self.service.create_user(user_id, user_id, role)
        self.service.create_facility("plan", {"facility_id": "fanshi-one", "name": "北部海上风电场", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_mwh": "500000"})
        self.service.create_facility("plan", {"facility_id": "fanshi-two", "name": "帆石二场", "kind": "offshore-station", "timezone": "Asia/Shanghai", "capacity_mwh": "800000"})
        self.service.create_route("plan", {"route_id": "fanshi-export", "origin_id": "fanshi-one", "destination_id": "fanshi-two", "product": "turbine-18mw", "daily_capacity": "100000", "loss_basis_points": 25, "transit_hours": 36})

    def tearDown(self) -> None:
        self.connection.close()

    def make_plan(self, snapshot_overrides: dict[str, object] | None = None, plan_overrides: dict[str, object] | None = None) -> dict[str, object]:
        self.service.create_ramp_snapshot("dispatch", snapshot_payload(**(snapshot_overrides or {})))
        return self.service.create_ramp_plan("dispatch", plan_payload(**(plan_overrides or {})))

    def make_confirmed_plan(self) -> dict[str, object]:
        self.make_plan()
        return self.service.confirm_ramp_plan("dispatch", "plan-1", 1)

    def test_phase_plan_splits_trajectory_into_executable_phases(self) -> None:
        plan = self.make_plan()
        self.assertEqual(plan["state"], "draft")
        self.assertEqual(plan["peak_target_mw"], "600.000")
        phases = plan["phases"]
        self.assertEqual([phase["phase"] for phase in phases], ["prepare", "trial_send", "expand", "steady"])
        self.assertEqual([phase["target_mw"] for phase in phases], ["0.000", "180.000", "420.000", "600.000"])
        self.assertEqual([phase["rollback_margin_mw"] for phase in phases], ["0.000", "180.000", "240.000", "180.000"])
        self.assertEqual(phases[2]["rollback_to"], "trial_send")
        self.assertEqual(phases[1]["title"], "试送")
        self.assertEqual(phases[3]["title"], "稳定运行")
        self.assertTrue(all(phase["satisfied"] for phase in phases))
        self.assertIsNone(plan["first_boundary"]["phase"])
        self.assertIsNone(plan["current_phase"])
        self.assertEqual(plan["next_phase"]["phase"], "prepare")

    def test_thermal_boundary_is_identified_before_steady(self) -> None:
        plan = self.make_plan({"corridors": [{"corridor_id": "cor-1", "route_id": "fanshi-export", "thermal_limit_mw": "350"}]})
        self.assertEqual(plan["first_boundary"]["phase"], "expand")
        self.assertEqual(plan["first_boundary"]["constraints"], ["corridor_thermal"])
        ceilings = plan["first_boundary"]["ceilings"]
        self.assertEqual(ceilings[0], {"kind": "corridor_thermal", "ceiling_mw": "350.000"})
        self.assertEqual(plan["first_boundary"]["binding_sources"], ["corridor_thermal"])
        expand = plan["phases"][2]
        thermal = next(item for item in expand["entry_conditions"] if item["kind"] == "corridor_thermal")
        self.assertFalse(thermal["satisfied"])
        self.assertEqual(thermal["required"], "420.000")
        self.assertEqual(thermal["available"], "350.000")

    def test_derating_strategy_reduces_unit_capability(self) -> None:
        derating = {"strategy_id": "d-wind", "rules": [{"product": "turbine-18mw", "wind_speed_above_mps": "10", "derate_percent": "15"}]}
        plan = self.make_plan(
            {"sea_state": {"wind_speed_mps": "12", "wave_height_m": "1.0", "observed_at": "2026-09-24T07:00:00Z"}},
            {"derating": derating},
        )
        unit_source = next(item for item in plan["capacity_sources"] if item["kind"] == "unit_capability")
        self.assertEqual(unit_source["available"], "612.000")
        self.assertEqual(unit_source["details"][0]["effective_mw"], "306.000")
        self.assertTrue(plan["phases"][3]["satisfied"])
        tougher = {"strategy_id": "d-wind", "rules": [{"product": "turbine-18mw", "wind_speed_above_mps": "10", "derate_percent": "25"}]}
        self.service.create_ramp_snapshot("dispatch", snapshot_payload(snapshot_id="snap-2", sea_state={"wind_speed_mps": "12", "wave_height_m": "1.0", "observed_at": "2026-09-24T07:00:00Z"}))
        plan2 = self.service.create_ramp_plan("dispatch", plan_payload(plan_id="plan-2", snapshot_id="snap-2", derating=tougher))
        self.assertEqual(plan2["first_boundary"]["phase"], "steady")
        self.assertEqual(plan2["first_boundary"]["constraints"], ["unit_capability"])

    def test_reactive_boundary_is_identified(self) -> None:
        plan = self.make_plan({"compensation": [{"station_id": "stat-1", "reactive_support_mvar": "150", "power_factor_limit": "0.95"}]})
        self.assertEqual(plan["first_boundary"]["phase"], "steady")
        self.assertEqual(plan["first_boundary"]["constraints"], ["reactive_support"])
        steady = plan["phases"][3]
        reactive = next(item for item in steady["entry_conditions"] if item["kind"] == "reactive_support")
        self.assertEqual(reactive["unit"], "Mvar")
        self.assertEqual(reactive["required"], "197.220")
        self.assertEqual(reactive["available"], "150.000")

    def test_reserve_boundary_blocks_trial_phase(self) -> None:
        plan = self.make_plan({"reserve": [{"reserve_id": "res-1", "spinning_reserve_mw": "100"}]})
        self.assertEqual(plan["first_boundary"]["phase"], "trial_send")
        self.assertEqual(plan["first_boundary"]["constraints"], ["spinning_reserve"])
        trial = plan["phases"][1]
        reserve = next(item for item in trial["entry_conditions"] if item["kind"] == "spinning_reserve")
        self.assertEqual(reserve["required"], "180.000")
        self.assertEqual(reserve["available"], "100.000")

    def test_confirm_reserves_capacity_from_each_source(self) -> None:
        status = self.make_confirmed_plan()
        self.assertEqual(status["state"], "confirmed")
        self.assertEqual(status["current_phase"]["phase"], "prepare")
        held = [item for item in status["reservations"] if item["state"] == "held"]
        self.assertEqual(len(held), 5)
        by_kind: dict[str, Decimal] = {}
        for item in held:
            by_kind[item["source_kind"]] = by_kind.get(item["source_kind"], Decimal("0")) + Decimal(item["reserved_amount"])
        self.assertEqual(by_kind["unit_capability"], Decimal("600.000"))
        self.assertEqual(by_kind["corridor_thermal"], Decimal("600.000"))
        self.assertEqual(by_kind["reactive_support"], Decimal("197.220"))
        self.assertEqual(by_kind["spinning_reserve"], Decimal("240.000"))
        unit_rows = [item for item in held if item["source_kind"] == "unit_capability"]
        self.assertEqual({item["source_id"] for item in unit_rows}, {"alpha", "beta"})
        for source in status["capacity_sources"]:
            self.assertEqual(Decimal(source["reserved"]), by_kind[source["kind"]])

    def test_confirm_is_blocked_when_capacity_is_insufficient(self) -> None:
        self.make_plan({"corridors": [{"corridor_id": "cor-1", "route_id": "fanshi-export", "thermal_limit_mw": "350"}]})
        with self.assertRaises(InvalidState):
            self.service.confirm_ramp_plan("dispatch", "plan-1", 1)
        status = self.service.ramp_plan_status("dispatch", "plan-1")
        self.assertEqual(status["state"], "draft")
        kinds = {item["kind"] for item in status["blocking_constraints"]}
        self.assertIn("corridor_thermal", kinds)
        self.assertEqual(status["reservations"], [])

    def test_underlying_version_change_blocks_confirm_and_advance(self) -> None:
        self.make_plan()
        self.service.announce_outage("risk", "fanshi-export", "2026-09-25T00:00:00Z", "2026-09-25T23:59:59Z", "50", "检修")
        with self.assertRaises(Conflict):
            self.service.confirm_ramp_plan("dispatch", "plan-1", 1)
        status = self.service.ramp_plan_status("dispatch", "plan-1")
        self.assertTrue(status["stale"])
        self.assertEqual(status["blocking_constraints"][0]["kind"], "basis_version")

    def test_underlying_version_change_blocks_advance_but_allows_fallback_and_retire(self) -> None:
        self.make_confirmed_plan()
        self.service.record_ramp_receipt("dispatch", "plan-1", {"receipt_key": "rcpt-p", "phase": "prepare", "actual_mw": "0"})
        self.service.announce_outage("risk", "fanshi-export", "2026-09-25T00:00:00Z", "2026-09-25T23:59:59Z", "50", "检修")
        with self.assertRaises(Conflict):
            self.service.advance_ramp_plan("dispatch", "plan-1", 2)
        status = self.service.ramp_plan_status("dispatch", "plan-1")
        self.assertTrue(status["stale"])
        self.assertIn("basis_version", {item["kind"] for item in status["blocking_constraints"]})
        fallback = self.service.abort_ramp_plan("risk", "plan-1", "通道检修", 2)
        self.assertEqual(fallback["state"], "fallback")
        retired = self.service.retire_ramp_plan("dispatch", "plan-1", 3)
        self.assertEqual(retired["state"], "retired")
        self.assertTrue(all(item["state"] == "released" for item in retired["reservations"]))

    def test_receipt_replay_counts_once_and_conflicts_on_different_payload(self) -> None:
        self.make_confirmed_plan()
        payload = {"receipt_key": "rcpt-p", "phase": "prepare", "actual_mw": "0", "note": "准备就绪"}
        first = self.service.record_ramp_receipt("dispatch", "plan-1", payload)
        second = self.service.record_ramp_receipt("dispatch", "plan-1", dict(payload))
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["receipt_id"], second["receipt_id"])
        rows = self.connection.execute("SELECT * FROM ramp_receipts WHERE plan_id='plan-1'").fetchall()
        self.assertEqual(len(rows), 1)
        with self.assertRaises(Conflict):
            self.service.record_ramp_receipt("dispatch", "plan-1", dict(payload, actual_mw="1"))

    def test_advance_requires_field_receipt(self) -> None:
        self.make_confirmed_plan()
        with self.assertRaises(InvalidState):
            self.service.advance_ramp_plan("dispatch", "plan-1", 1)
        status = self.service.ramp_plan_status("dispatch", "plan-1")
        kinds = {item["kind"] for item in status["blocking_constraints"]}
        self.assertIn("field_receipt", kinds)

    def test_full_lifecycle_fallback_preserves_evidence_and_completes(self) -> None:
        self.make_confirmed_plan()
        self.service.record_ramp_receipt("dispatch", "plan-1", {"receipt_key": "rcpt-prepare", "phase": "prepare", "actual_mw": "0"})
        self.service.advance_ramp_plan("dispatch", "plan-1", 2)
        self.service.record_ramp_receipt("dispatch", "plan-1", {"receipt_key": "rcpt-trial", "phase": "trial_send", "actual_mw": "180"})
        self.service.advance_ramp_plan("dispatch", "plan-1", 3)
        fallback = self.service.abort_ramp_plan("risk", "plan-1", "海缆温度越限预警", 4)
        self.assertEqual(fallback["state"], "fallback")
        self.assertEqual(fallback["current_phase"]["phase"], "trial_send")
        self.assertEqual(fallback["rollback"]["safe_phase"], "trial_send")
        self.assertEqual(fallback["rollback"]["accepted_receipts"], 2)
        self.assertEqual(len(fallback["receipts"]), 2)
        resumed = self.service.advance_ramp_plan("dispatch", "plan-1", 5)
        self.assertEqual(resumed["state"], "confirmed")
        self.assertEqual(resumed["current_phase"]["phase"], "expand")
        self.service.record_ramp_receipt("dispatch", "plan-1", {"receipt_key": "rcpt-expand", "phase": "expand", "actual_mw": "420"})
        self.service.advance_ramp_plan("dispatch", "plan-1", 6)
        self.service.record_ramp_receipt("dispatch", "plan-1", {"receipt_key": "rcpt-steady", "phase": "steady", "actual_mw": "600"})
        completed = self.service.advance_ramp_plan("dispatch", "plan-1", 7)
        self.assertEqual(completed["state"], "completed")
        self.assertEqual(completed["current_phase"]["phase"], "steady")
        self.assertIsNone(completed["next_phase"])
        self.assertEqual(len(completed["receipts"]), 4)
        self.assertTrue(all(item["state"] == "released" for item in completed["reservations"]))
        self.assertEqual(completed["blocking_constraints"], [])
        self.assertTrue(self.service.audit_chain("audit")["valid"])

    def test_abort_without_evidence_falls_back_to_prepare(self) -> None:
        self.make_confirmed_plan()
        fallback = self.service.abort_ramp_plan("dispatch", "plan-1", "机组通讯中断", 2)
        self.assertEqual(fallback["state"], "fallback")
        self.assertEqual(fallback["rollback"]["safe_phase"], "prepare")
        self.assertEqual(fallback["current_phase"]["phase"], "prepare")

    def test_status_explains_sources_blocking_and_rollback(self) -> None:
        status = self.make_confirmed_plan()
        self.assertEqual([item["kind"] for item in status["capacity_sources"]], ["unit_capability", "corridor_thermal", "reactive_support", "spinning_reserve"])
        unit = status["capacity_sources"][0]
        self.assertEqual(unit["available"], "720.000")
        self.assertEqual(unit["reserved"], "600.000")
        self.assertEqual({item["project_id"] for item in unit["details"]}, {"alpha", "beta"})
        self.assertEqual(status["rollback"]["safe_phase"], "prepare")
        self.assertEqual(status["rollback"]["rollback_margin_mw"], "0.000")
        self.assertEqual([item["kind"] for item in status["blocking_constraints"]], ["field_receipt"])
        phases = {item["phase"]: item for item in status["phases"]}
        self.assertEqual(phases["prepare"]["status"], "current")
        self.assertEqual(phases["steady"]["status"], "pending")

    def test_trajectory_steeper_than_ramp_capability_is_rejected(self) -> None:
        self.service.create_ramp_snapshot("dispatch", snapshot_payload())
        with self.assertRaises(ValidationFailed):
            self.service.create_ramp_plan("dispatch", plan_payload(trajectory=[{"offset_minutes": 0, "target_mw": "0"}, {"offset_minutes": 1, "target_mw": "500"}]))

    def test_snapshot_requires_existing_route_and_unique_content(self) -> None:
        with self.assertRaises(NotFound):
            self.service.create_ramp_snapshot("dispatch", snapshot_payload(corridors=[{"corridor_id": "cor-x", "route_id": "missing", "thermal_limit_mw": "800"}]))
        self.service.create_ramp_snapshot("dispatch", snapshot_payload())
        with self.assertRaises(Conflict):
            self.service.create_ramp_snapshot("dispatch", snapshot_payload())
        with self.assertRaises(NotFound):
            self.service.create_ramp_plan("dispatch", plan_payload(snapshot_id="missing"))

    def test_permissions_are_enforced(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.create_ramp_snapshot("plan", snapshot_payload())
        self.make_plan()
        with self.assertRaises(Forbidden):
            self.service.confirm_ramp_plan("plan", "plan-1", 1)
        with self.assertRaises(Forbidden):
            self.service.record_ramp_receipt("plan", "plan-1", {"receipt_key": "k", "phase": "prepare", "actual_mw": "0"})
        status = self.service.ramp_plan_status("audit", "plan-1")
        self.assertEqual(status["plan_id"], "plan-1")

    def test_api_exposes_ramp_lifecycle(self) -> None:
        app = JsonApplication(self.service)
        headers = {"X-Actor-Id": "dispatch"}
        response = app.handle("POST", "/ramp/snapshots", headers, json.dumps(snapshot_payload()).encode("utf-8"))
        self.assertEqual(response.status, 201)
        response = app.handle("POST", "/ramp/plans", headers, json.dumps(plan_payload()).encode("utf-8"))
        self.assertEqual(response.status, 201)
        self.assertEqual(response.body["state"], "draft")
        response = app.handle("POST", "/ramp/plans/plan-1/confirm", headers, json.dumps({"expected_revision": 1}).encode("utf-8"))
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["state"], "confirmed")
        response = app.handle("POST", "/ramp/plans/plan-1/receipts", headers, json.dumps({"receipt_key": "rcpt-p", "phase": "prepare", "actual_mw": "0"}).encode("utf-8"))
        self.assertEqual(response.status, 201)
        response = app.handle("POST", "/ramp/plans/plan-1/advance", headers, json.dumps({"expected_revision": 2}).encode("utf-8"))
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["current_phase"]["phase"], "trial_send")
        response = app.handle("GET", "/ramp/plans/plan-1", {"X-Actor-Id": "audit"})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["state"], "confirmed")
        self.assertEqual(len(response.body["capacity_sources"]), 4)
        response = app.handle("POST", "/ramp/plans/plan-1/abort", {"X-Actor-Id": "risk"}, json.dumps({"reason": "异常", "expected_revision": 3}).encode("utf-8"))
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["state"], "fallback")
        response = app.handle("POST", "/ramp/plans/plan-1/retire", headers, json.dumps({"expected_revision": 4}).encode("utf-8"))
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["state"], "retired")

    def test_offline_acceptance_covers_ramp_rehearsal(self) -> None:
        result = acceptance_run(ROOT)
        self.assertEqual(result["status"], "ok")
        ramp = result["ramp"]
        self.assertEqual(ramp["state"], "completed")
        self.assertEqual(ramp["phases"], 4)
        self.assertTrue(ramp["receipt_replayed"])
        self.assertEqual(ramp["fallback_safe_phase"], "trial_send")
        self.assertTrue(ramp["reservations_released"])
        self.assertTrue(result["audit"]["valid"])


if __name__ == "__main__":
    unittest.main()
