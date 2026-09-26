"""结算单价、机组可用量、送出通道、提名和调峰阶段计划的事务用例。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta
from decimal import Decimal
from typing import Any, Iterable, Mapping

from .clock import SystemClock, parse_utc, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .models import (
    RAMP_PHASES,
    IndexQuote,
    Facility,
    InventoryLot,
    NominationRequest,
    RampPlanRequest,
    RampReceiptRequest,
    RampSnapshot,
    Route,
    SupplyScenario,
)
from .planning import (
    AllocationRequest,
    PricePoint,
    allocate_capacity,
    canonical_json,
    decimal_text,
    delivered_after_loss,
    digest,
    effective_capacity,
    latest_streak,
    moving_average,
    quantize_volume,
    scenario_projection,
    weighted_inventory_cost,
)
from .ramp import PHASE_TITLES, build_phase_plan, mw_text, reservation_blocking
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "planner": {"quote.write", "catalog.write", "scenario.write", "scenario.run", "ramp.read"},
    "dispatcher": {
        "nomination.write",
        "allocation.run",
        "transfer.write",
        "inventory.write",
        "ramp.write",
        "ramp.confirm",
        "ramp.receipt",
        "ramp.advance",
        "ramp.abort",
        "ramp.read",
    },
    "risk": {"outage.write", "scenario.approve", "report.read", "ramp.abort", "ramp.read"},
    "auditor": {"report.read", "audit.read", "ramp.read"},
}


class SupplyService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM supply_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM supply_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO supply_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO supply_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    def record_quote(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "quote.write")
        quote = IndexQuote.from_dict(raw)
        previous = self.connection.execute(
            "SELECT quote_id,source_revision FROM market_index_quotes WHERE market_index=? AND trade_date=? "
            "ORDER BY quote_id DESC LIMIT 1",
            (quote.market_index, quote.trade_date),
        ).fetchone()
        if previous is not None and previous["source_revision"] == quote.source_revision:
            raise Conflict("同一来源修订已登记")
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO market_index_quotes(market_index,trade_date,close_cny,source_revision,observed_at,"
                    "supersedes_quote_id,recorded_by,recorded_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        quote.market_index,
                        quote.trade_date,
                        decimal_text(quote.close_cny),
                        quote.source_revision,
                        quote.observed_at,
                        None if previous is None else previous["quote_id"],
                        actor_id,
                        self._now(),
                    ),
                )
                quote_id = int(cursor.lastrowid)
                self._audit(
                    "quote",
                    str(quote_id),
                    "quote.recorded",
                    actor_id,
                    {"market_index": quote.market_index, "trade_date": quote.trade_date},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("结算单价版本冲突") from exc
        return {"quote_id": quote_id, "market_index": quote.market_index, "trade_date": quote.trade_date}

    def price_summary(self, market_index: str, sessions: int = 20) -> dict[str, Any]:
        rows = self.connection.execute(
            "SELECT q.trade_date,q.close_cny FROM market_index_quotes q "
            "JOIN (SELECT trade_date,max(quote_id) quote_id FROM market_index_quotes "
            "WHERE market_index=? GROUP BY trade_date) latest ON latest.quote_id=q.quote_id "
            "ORDER BY q.trade_date DESC LIMIT ?",
            (market_index.upper(), sessions),
        ).fetchall()
        points = [PricePoint(row["trade_date"], Decimal(row["close_cny"])) for row in rows]
        if not points:
            raise NotFound("没有基准结算单价")
        streak = latest_streak(points)
        average = moving_average(points, min(5, len(points)))
        latest = max(points, key=lambda item: item.trade_date)
        return {
            "market_index": market_index.upper(),
            "latest": {"trade_date": latest.trade_date, "close_cny": decimal_text(latest.close)},
            "latest_streak": None if streak is None else streak.as_dict(),
            "moving_average": None if average is None else decimal_text(average),
            "observations": len(points),
        }

    def create_facility(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        facility = Facility.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO facilities(facility_id,name,kind,timezone,capacity_mwh,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        facility.facility_id,
                        facility.name,
                        facility.kind,
                        facility.timezone,
                        decimal_text(facility.capacity_mwh),
                        self._now(),
                    ),
                )
                self._audit("facility", facility.facility_id, "facility.created", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("设施编号已经存在") from exc
        return dict(raw)

    def create_route(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        route = Route.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO routes(route_id,origin_id,destination_id,product,daily_capacity,"
                    "loss_basis_points,transit_hours,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        route.route_id,
                        route.origin_id,
                        route.destination_id,
                        route.product,
                        decimal_text(route.daily_capacity),
                        route.loss_basis_points,
                        route.transit_hours,
                        self._now(),
                    ),
                )
                self._audit("route", route.route_id, "route.created", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("送出通道编号冲突或设施不存在") from exc
        return self.route(route.route_id)

    def route(self, route_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM routes WHERE route_id=?", (route_id,)).fetchone()
        if row is None:
            raise NotFound("送出通道不存在")
        return dict(row)

    def announce_outage(
        self,
        actor_id: str,
        route_id: str,
        starts_at: str,
        ends_at: str | None,
        capacity_percent: object,
        reason: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "outage.write")
        self.route(route_id)
        try:
            start = parse_utc(starts_at, "starts_at")
            end = None if ends_at is None else parse_utc(ends_at, "ends_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        if end is not None and end <= start:
            raise ValidationFailed("ends_at 必须晚于 starts_at")
        percentage = Decimal(str(capacity_percent))
        if percentage < 0 or percentage > 100:
            raise ValidationFailed("capacity_percent 必须在 0 到 100 之间")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO route_outages(route_id,starts_at,ends_at,capacity_percent,reason,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (route_id, utc_text(start), None if end is None else utc_text(end), decimal_text(percentage), reason, actor_id, self._now()),
            )
            outage_id = int(cursor.lastrowid)
            self._audit("route", route_id, "outage.announced", actor_id, {"outage_id": outage_id})
        return {"outage_id": outage_id, "route_id": route_id, "state": "announced"}

    def add_inventory_lot(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "inventory.write")
        lot = InventoryLot.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO inventory_lots(lot_id,facility_id,product,grade,quantity_mwh,available_mwh,"
                    "unit_cost_cny,received_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        lot.lot_id,
                        lot.facility_id,
                        lot.product,
                        lot.grade,
                        decimal_text(lot.quantity_mwh),
                        decimal_text(lot.quantity_mwh),
                        decimal_text(lot.unit_cost_cny),
                        lot.received_at,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit("inventory_lot", lot.lot_id, "inventory.received", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("风机资源批次冲突或设施不存在") from exc
        return self.inventory_lot(lot.lot_id)

    def inventory_lot(self, lot_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM inventory_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if row is None:
            raise NotFound("风机资源批次不存在")
        return dict(row)

    def inventory_summary(self, facility_id: str, product: str) -> dict[str, Any]:
        rows = self.connection.execute(
            "SELECT * FROM inventory_lots WHERE facility_id=? AND product=? ORDER BY received_at,lot_id",
            (facility_id, product),
        ).fetchall()
        return {"facility_id": facility_id, "product": product, **weighted_inventory_cost(rows)}

    def submit_nomination(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "nomination.write")
        nomination = NominationRequest.from_dict(raw)
        request_digest = digest(raw)
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM supply_idempotency WHERE scope='nomination' AND idempotency_key=?",
            (nomination.idempotency_key,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_digest:
                raise Conflict("幂等键对应不同提名内容")
            return json.loads(stored["response_json"])
        route = self.route(nomination.route_id)
        if route["state"] != "active":
            raise InvalidState("送出通道当前不可提名")
        response = {
            "nomination_id": nomination.nomination_id,
            "route_id": nomination.route_id,
            "state": "submitted",
            "revision": 1,
        }
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO nominations(nomination_id,route_id,shipper_id,service_date,requested_mwh,"
                    "priority,idempotency_key,submitted_by,submitted_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        nomination.nomination_id,
                        nomination.route_id,
                        nomination.shipper_id,
                        nomination.service_date,
                        decimal_text(nomination.requested_mwh),
                        nomination.priority,
                        nomination.idempotency_key,
                        actor_id,
                        self._now(),
                    ),
                )
                self.connection.execute(
                    "INSERT INTO supply_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                    "VALUES('nomination',?,?,?,?)",
                    (nomination.idempotency_key, request_digest, canonical_json(response), self._now()),
                )
                self._audit("nomination", nomination.nomination_id, "nomination.submitted", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("提名编号或幂等键冲突") from exc
        return response

    def _capacity_for_date(self, route: sqlite3.Row, service_date: str) -> Decimal:
        start = service_date + "T00:00:00Z"
        end = service_date + "T23:59:59Z"
        rows = self.connection.execute(
            "SELECT capacity_percent FROM route_outages WHERE route_id=? AND state IN ('announced','active') "
            "AND starts_at<=? AND (ends_at IS NULL OR ends_at>=?) ORDER BY outage_id",
            (route["route_id"], end, start),
        ).fetchall()
        percentages = [Decimal(row["capacity_percent"]) for row in rows]
        return effective_capacity(Decimal(route["daily_capacity"]), percentages)

    def allocate(self, actor_id: str, route_id: str, service_date: str) -> dict[str, Any]:
        self._require(actor_id, "allocation.run")
        route = self.connection.execute("SELECT * FROM routes WHERE route_id=?", (route_id,)).fetchone()
        if route is None:
            raise NotFound("送出通道不存在")
        nominations = self.connection.execute(
            "SELECT * FROM nominations WHERE route_id=? AND service_date=? AND state='submitted' "
            "ORDER BY priority,submitted_at,nomination_id",
            (route_id, service_date),
        ).fetchall()
        if not nominations:
            raise InvalidState("没有待分配提名")
        requests = [
            AllocationRequest(
                row["nomination_id"],
                Decimal(row["requested_mwh"]),
                int(row["priority"]),
                row["submitted_at"],
            )
            for row in nominations
        ]
        available = self._capacity_for_date(route, service_date)
        input_value = [dict(row) for row in nominations]
        input_sha256 = digest({"route": dict(route), "nominations": input_value, "capacity": str(available)})
        result_rows = allocate_capacity(available, requests)
        result = {
            "route_id": route_id,
            "service_date": service_date,
            "available_capacity": decimal_text(available),
            "allocations": result_rows,
        }
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO allocation_runs(route_id,service_date,input_sha256,available_capacity,result_json,"
                "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (route_id, service_date, input_sha256, decimal_text(available), canonical_json(result), actor_id, self._now()),
            )
            for item in result_rows:
                state = "allocated" if Decimal(item["allocated_mwh"]) > 0 else "cancelled"
                self.connection.execute(
                    "UPDATE nominations SET allocated_mwh=?,state=?,revision=revision+1 "
                    "WHERE nomination_id=? AND state='submitted'",
                    (item["allocated_mwh"], state, item["nomination_id"]),
                )
            allocation_id = int(cursor.lastrowid)
            self._audit("route", route_id, "allocation.completed", actor_id, {"allocation_id": allocation_id})
        return {"allocation_id": allocation_id, **result}

    def dispatch_transfer(
        self,
        actor_id: str,
        transfer_id: str,
        nomination_id: str,
        lot_id: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        self._require(actor_id, "transfer.write")
        nomination = self.connection.execute(
            "SELECT n.*,r.loss_basis_points,r.transit_hours,r.origin_id FROM nominations n "
            "JOIN routes r ON r.route_id=n.route_id WHERE n.nomination_id=?",
            (nomination_id,),
        ).fetchone()
        if nomination is None:
            raise NotFound("提名不存在")
        if nomination["state"] != "allocated" or nomination["revision"] != expected_revision:
            raise InvalidState("提名不是当前可并网版本")
        lot = self.connection.execute("SELECT * FROM inventory_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if lot is None:
            raise NotFound("风机资源批次不存在")
        allocated = Decimal(nomination["allocated_mwh"])
        available = Decimal(lot["available_mwh"])
        if lot["facility_id"] != nomination["origin_id"] or lot["product"] != self.route(nomination["route_id"])["product"]:
            raise Conflict("风机资源批次与送出通道起点或机组类型不匹配")
        if available < allocated:
            raise Conflict("机组可用量不足以完成分配")
        expected_delivery = delivered_after_loss(allocated, int(nomination["loss_basis_points"]))
        departed_at = self._now()
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE inventory_lots SET available_mwh=?,revision=revision+1 WHERE lot_id=? AND revision=?",
                (decimal_text(quantize_volume(available - allocated)), lot_id, lot["revision"]),
            )
            self.connection.execute(
                "UPDATE nominations SET state='in_transit',revision=revision+1 WHERE nomination_id=? AND revision=?",
                (nomination_id, expected_revision),
            )
            self.connection.execute(
                "INSERT INTO transfers(transfer_id,nomination_id,inventory_lot_id,generated_mwh,"
                "expected_delivered_mwh,departed_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    transfer_id,
                    nomination_id,
                    lot_id,
                    decimal_text(allocated),
                    decimal_text(expected_delivery),
                    departed_at,
                    actor_id,
                    departed_at,
                ),
            )
            self._audit("transfer", transfer_id, "transfer.dispatched", actor_id, {"nomination_id": nomination_id})
        return {
            "transfer_id": transfer_id,
            "state": "in_transit",
            "generated_mwh": decimal_text(allocated),
            "expected_delivered_mwh": decimal_text(expected_delivery),
            "expected_arrival": utc_text(parse_utc(departed_at) + timedelta(hours=int(nomination["transit_hours"]))),
        }

    def create_scenario(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "scenario.write")
        scenario = SupplyScenario.from_dict(raw)
        definition = canonical_json(raw)
        content_sha256 = hashlib.sha256(definition.encode("utf-8")).hexdigest()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO supply_scenarios(scenario_id,name,definition_json,content_sha256,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (scenario.scenario_id, scenario.name, definition, content_sha256, actor_id, self._now()),
                )
                self._audit("scenario", scenario.scenario_id, "scenario.created", actor_id, {"sha256": content_sha256})
        except sqlite3.IntegrityError as exc:
            raise Conflict("情景编号或内容已经存在") from exc
        return {"scenario_id": scenario.scenario_id, "state": "draft", "sha256": content_sha256}

    def approve_scenario(self, actor_id: str, scenario_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "scenario.approve")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE supply_scenarios SET state='approved',revision=revision+1 "
                "WHERE scenario_id=? AND state='draft' AND revision=?",
                (scenario_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("情景不是当前草稿版本")
            self._audit("scenario", scenario_id, "scenario.approved", actor_id, {})
        return {"scenario_id": scenario_id, "state": "approved", "revision": expected_revision + 1}

    def run_scenario(self, actor_id: str, scenario_id: str, as_of_date: str) -> dict[str, Any]:
        self._require(actor_id, "scenario.run")
        row = self.connection.execute(
            "SELECT * FROM supply_scenarios WHERE scenario_id=?", (scenario_id,)
        ).fetchone()
        if row is None:
            raise NotFound("情景不存在")
        if row["state"] != "approved":
            raise InvalidState("只有已批准情景可以运行")
        scenario = SupplyScenario.from_dict(json.loads(row["definition_json"]))
        price_row = self.connection.execute(
            "SELECT close_cny FROM market_index_quotes WHERE trade_date<=? ORDER BY trade_date DESC,quote_id DESC LIMIT 1",
            (as_of_date,),
        ).fetchone()
        if price_row is None:
            raise InvalidState("截止日期没有可用结算单价")
        routes = self.connection.execute("SELECT * FROM routes WHERE state='active' ORDER BY route_id").fetchall()
        inventory = self.connection.execute(
            "SELECT facility_id,product,sum(CAST(available_mwh AS REAL)) available_mwh "
            "FROM inventory_lots GROUP BY facility_id,product ORDER BY facility_id,product"
        ).fetchall()
        input_value = {
            "scenario_sha256": row["content_sha256"],
            "as_of_date": as_of_date,
            "price": price_row["close_cny"],
            "routes": [dict(item) for item in routes],
            "inventory": [dict(item) for item in inventory],
        }
        input_sha256 = digest(input_value)
        existing = self.connection.execute(
            "SELECT run_id,result_json FROM scenario_runs WHERE scenario_id=? AND as_of_date=? AND input_sha256=?",
            (scenario_id, as_of_date, input_sha256),
        ).fetchone()
        if existing is not None:
            return {"run_id": existing["run_id"], **json.loads(existing["result_json"]), "replayed": True}
        result = scenario_projection(
            current_price=Decimal(price_row["close_cny"]),
            market_index_drop_percent=scenario.market_index_drop_percent,
            routes=routes,
            inventory=inventory,
            route_capacity_changes=scenario.route_capacity_changes,
            demand_changes=scenario.demand_changes,
        )
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO scenario_runs(scenario_id,as_of_date,input_sha256,result_json,created_by,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (scenario_id, as_of_date, input_sha256, canonical_json(result), actor_id, self._now()),
            )
            run_id = int(cursor.lastrowid)
            self._audit("scenario", scenario_id, "scenario.executed", actor_id, {"run_id": run_id})
        return {"run_id": run_id, **result, "replayed": False}

    def _basis_fingerprint(self) -> str:
        """机组能力、海况相关的通道设施等底层数据的当前版本指纹。"""
        basis = {
            "facilities": [dict(row) for row in self.connection.execute(
                "SELECT * FROM facilities ORDER BY facility_id"
            ).fetchall()],
            "routes": [dict(row) for row in self.connection.execute(
                "SELECT * FROM routes ORDER BY route_id"
            ).fetchall()],
            "route_outages": [dict(row) for row in self.connection.execute(
                "SELECT * FROM route_outages ORDER BY outage_id"
            ).fetchall()],
            "inventory_lots": [dict(row) for row in self.connection.execute(
                "SELECT * FROM inventory_lots ORDER BY lot_id"
            ).fetchall()],
        }
        return digest(basis)

    def _ramp_plan(self, plan_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM ramp_plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if row is None:
            raise NotFound("调峰阶段计划不存在")
        return row

    def _ramp_snapshot(self, snapshot_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM ramp_snapshots WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()
        if row is None:
            raise NotFound("调峰预演快照不存在")
        return row

    def _require_basis_fresh(self, snapshot: sqlite3.Row) -> str:
        current = self._basis_fingerprint()
        if current != snapshot["basis_sha256"]:
            raise Conflict("机组能力、海况或通道设施版本已变化，旧计划不能继续推进")
        return current

    def create_ramp_snapshot(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "ramp.write")
        snapshot = RampSnapshot.from_dict(raw)
        for corridor in snapshot.corridors:
            self.route(corridor.route_id)
        content = canonical_json(raw)
        content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
        basis_sha256 = self._basis_fingerprint()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO ramp_snapshots(snapshot_id,command_id,content_json,content_sha256,"
                    "basis_sha256,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        snapshot.snapshot_id,
                        snapshot.command_id,
                        content,
                        content_sha256,
                        basis_sha256,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit(
                    "ramp_snapshot",
                    snapshot.snapshot_id,
                    "ramp.snapshot_created",
                    actor_id,
                    {"command_id": snapshot.command_id, "basis_sha256": basis_sha256},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("快照编号或内容已经存在") from exc
        return {
            "snapshot_id": snapshot.snapshot_id,
            "command_id": snapshot.command_id,
            "basis_sha256": basis_sha256,
            "content_sha256": content_sha256,
        }

    def create_ramp_plan(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "ramp.write")
        request = RampPlanRequest.from_dict(raw)
        snapshot_row = self._ramp_snapshot(request.snapshot_id)
        snapshot = RampSnapshot.from_dict(json.loads(snapshot_row["content_json"]))
        plan = build_phase_plan(snapshot, request.trajectory, request.derating)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO ramp_plans(plan_id,snapshot_id,command_id,request_json,phases_json,"
                    "capacity_json,peak_target_mw,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        request.plan_id,
                        request.snapshot_id,
                        snapshot_row["command_id"],
                        canonical_json(raw),
                        canonical_json(plan["phases"]),
                        canonical_json(plan["capacity"]),
                        plan["peak_target_mw"],
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit(
                    "ramp_plan",
                    request.plan_id,
                    "ramp.plan_created",
                    actor_id,
                    {"snapshot_id": request.snapshot_id, "peak_target_mw": plan["peak_target_mw"]},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("阶段计划编号已经存在") from exc
        return self._ramp_status(self._ramp_plan(request.plan_id))

    def confirm_ramp_plan(self, actor_id: str, plan_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "ramp.confirm")
        plan = self._ramp_plan(plan_id)
        if plan["state"] != "draft" or plan["revision"] != expected_revision:
            raise InvalidState("计划不是当前草稿版本")
        snapshot = self._ramp_snapshot(plan["snapshot_id"])
        self._require_basis_fresh(snapshot)
        capacity = json.loads(plan["capacity_json"])
        blocking = reservation_blocking(capacity["sources"], capacity["requirements"])
        if blocking:
            kinds = "、".join(item["title"] for item in blocking)
            raise InvalidState(f"容量不足以预留：{kinds}")
        now = self._now()
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE ramp_plans SET state='confirmed',phase_index=0,revision=revision+1,confirmed_at=? "
                "WHERE plan_id=? AND state='draft' AND revision=?",
                (now, plan_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("计划不是当前草稿版本")
            for item in capacity["reservation_split"]:
                self.connection.execute(
                    "INSERT INTO ramp_reservations(plan_id,source_kind,source_id,reserved_amount,unit,"
                    "created_at) VALUES(?,?,?,?,?,?)",
                    (
                        plan_id,
                        item["source_kind"],
                        item["source_id"],
                        item["reserved_amount"],
                        item["unit"],
                        now,
                    ),
                )
            self._audit(
                "ramp_plan",
                plan_id,
                "ramp.plan_confirmed",
                actor_id,
                {"reservations": len(capacity["reservation_split"])},
            )
        return self._ramp_status(self._ramp_plan(plan_id))

    def record_ramp_receipt(self, actor_id: str, plan_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "ramp.receipt")
        receipt = RampReceiptRequest.from_dict(raw)
        plan = self._ramp_plan(plan_id)
        request_digest = digest(raw)
        stored = self.connection.execute(
            "SELECT * FROM ramp_receipts WHERE plan_id=? AND receipt_key=?",
            (plan_id, receipt.receipt_key),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_digest:
                raise Conflict("回执键对应不同回执内容")
            return {
                "receipt_id": stored["receipt_id"],
                "plan_id": plan_id,
                "phase": stored["phase"],
                "actual_mw": stored["actual_mw"],
                "replayed": True,
            }
        if plan["state"] not in ("confirmed", "fallback"):
            raise InvalidState("计划当前不能接收现场回执")
        current_phase = RAMP_PHASES[int(plan["phase_index"])]
        if receipt.phase != current_phase:
            raise InvalidState(f"回执阶段必须是当前阶段 {current_phase}")
        duplicate = self.connection.execute(
            "SELECT 1 FROM ramp_receipts WHERE plan_id=? AND phase=?",
            (plan_id, receipt.phase),
        ).fetchone()
        if duplicate is not None:
            raise Conflict("该阶段已有现场回执")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO ramp_receipts(plan_id,phase,receipt_key,actual_mw,note,request_sha256,"
                "created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    plan_id,
                    receipt.phase,
                    receipt.receipt_key,
                    mw_text(receipt.actual_mw),
                    receipt.note,
                    request_digest,
                    actor_id,
                    self._now(),
                ),
            )
            receipt_id = int(cursor.lastrowid)
            self._audit(
                "ramp_plan",
                plan_id,
                "ramp.receipt_recorded",
                actor_id,
                {"receipt_id": receipt_id, "phase": receipt.phase},
            )
        return {
            "receipt_id": receipt_id,
            "plan_id": plan_id,
            "phase": receipt.phase,
            "actual_mw": mw_text(receipt.actual_mw),
            "replayed": False,
        }

    def advance_ramp_plan(self, actor_id: str, plan_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "ramp.advance")
        plan = self._ramp_plan(plan_id)
        if plan["state"] not in ("confirmed", "fallback") or plan["revision"] != expected_revision:
            raise InvalidState("计划不是当前可推进版本")
        snapshot = self._ramp_snapshot(plan["snapshot_id"])
        self._require_basis_fresh(snapshot)
        phases = json.loads(plan["phases_json"])
        index = int(plan["phase_index"])
        current_phase = RAMP_PHASES[index]
        receipt = self.connection.execute(
            "SELECT * FROM ramp_receipts WHERE plan_id=? AND phase=?",
            (plan_id, current_phase),
        ).fetchone()
        if receipt is None:
            raise InvalidState(f"缺少 {current_phase} 阶段的现场回执")
        now = self._now()
        next_index = index + 1
        with transaction(self.connection, immediate=True):
            if next_index >= len(phases):
                cursor = self.connection.execute(
                    "UPDATE ramp_plans SET state='completed',revision=revision+1,closed_at=? "
                    "WHERE plan_id=? AND revision=? AND state IN ('confirmed','fallback')",
                    (now, plan_id, expected_revision),
                )
                if cursor.rowcount != 1:
                    raise InvalidState("计划不是当前可推进版本")
                self._release_ramp_reservations(plan_id, now)
                self._audit("ramp_plan", plan_id, "ramp.plan_completed", actor_id, {})
            else:
                unmet = [
                    condition
                    for condition in phases[next_index]["entry_conditions"]
                    if not condition["satisfied"]
                ]
                if unmet:
                    kinds = "、".join(condition["title"] for condition in unmet)
                    raise InvalidState(f"下一阶段进入条件未满足：{kinds}")
                cursor = self.connection.execute(
                    "UPDATE ramp_plans SET state='confirmed',phase_index=?,revision=revision+1 "
                    "WHERE plan_id=? AND revision=? AND state IN ('confirmed','fallback')",
                    (next_index, plan_id, expected_revision),
                )
                if cursor.rowcount != 1:
                    raise InvalidState("计划不是当前可推进版本")
                self._audit(
                    "ramp_plan",
                    plan_id,
                    "ramp.plan_advanced",
                    actor_id,
                    {"from": current_phase, "to": RAMP_PHASES[next_index]},
                )
        return self._ramp_status(self._ramp_plan(plan_id))

    def abort_ramp_plan(
        self,
        actor_id: str,
        plan_id: str,
        reason: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        self._require(actor_id, "ramp.abort")
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationFailed("回退原因不能为空")
        plan = self._ramp_plan(plan_id)
        if plan["state"] not in ("confirmed", "fallback") or plan["revision"] != expected_revision:
            raise InvalidState("计划不是当前可回退版本")
        index = int(plan["phase_index"])
        evidenced = {
            RAMP_PHASES.index(row["phase"])
            for row in self.connection.execute(
                "SELECT DISTINCT phase FROM ramp_receipts WHERE plan_id=?", (plan_id,)
            ).fetchall()
        }
        safe = max((item for item in evidenced if item <= index), default=0)
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE ramp_plans SET state='fallback',phase_index=?,revision=revision+1 "
                "WHERE plan_id=? AND revision=? AND state IN ('confirmed','fallback')",
                (safe, plan_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("计划不是当前可回退版本")
            self._audit(
                "ramp_plan",
                plan_id,
                "ramp.plan_aborted",
                actor_id,
                {
                    "reason": reason.strip(),
                    "from": RAMP_PHASES[index],
                    "to": RAMP_PHASES[safe],
                },
            )
        return self._ramp_status(self._ramp_plan(plan_id))

    def retire_ramp_plan(self, actor_id: str, plan_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "ramp.write")
        plan = self._ramp_plan(plan_id)
        if plan["state"] in ("completed", "retired") or plan["revision"] != expected_revision:
            raise InvalidState("计划已经关闭")
        now = self._now()
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE ramp_plans SET state='retired',revision=revision+1,closed_at=? "
                "WHERE plan_id=? AND revision=? AND state IN ('draft','confirmed','fallback')",
                (now, plan_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("计划已经关闭")
            self._release_ramp_reservations(plan_id, now)
            self._audit("ramp_plan", plan_id, "ramp.plan_retired", actor_id, {})
        return self._ramp_status(self._ramp_plan(plan_id))

    def _release_ramp_reservations(self, plan_id: str, released_at: str) -> None:
        self.connection.execute(
            "UPDATE ramp_reservations SET state='released',released_at=? "
            "WHERE plan_id=? AND state='held'",
            (released_at, plan_id),
        )

    def ramp_plan_status(self, actor_id: str, plan_id: str) -> dict[str, Any]:
        self._require(actor_id, "ramp.read")
        return self._ramp_status(self._ramp_plan(plan_id))

    def _ramp_status(self, plan: sqlite3.Row) -> dict[str, Any]:
        plan_id = plan["plan_id"]
        snapshot = self._ramp_snapshot(plan["snapshot_id"])
        basis_sha256 = self._basis_fingerprint()
        stale = basis_sha256 != snapshot["basis_sha256"]
        phases = json.loads(plan["phases_json"])
        capacity = json.loads(plan["capacity_json"])
        state = plan["state"]
        index = int(plan["phase_index"])
        receipts = [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM ramp_receipts WHERE plan_id=? ORDER BY receipt_id", (plan_id,)
            ).fetchall()
        ]
        evidenced_phases = {row["phase"] for row in receipts}
        reservations = [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM ramp_reservations WHERE plan_id=? ORDER BY reservation_id", (plan_id,)
            ).fetchall()
        ]
        held_by_kind: dict[str, Decimal] = {}
        for row in reservations:
            if row["state"] == "held":
                held_by_kind[row["source_kind"]] = held_by_kind.get(row["source_kind"], Decimal("0")) + Decimal(
                    row["reserved_amount"]
                )
        phase_views = []
        for position, phase in enumerate(phases):
            if state == "completed" or position < index:
                phase_status = "completed"
            elif state == "retired":
                phase_status = "abandoned"
            elif position == index and state in ("confirmed", "fallback"):
                phase_status = "current"
            else:
                phase_status = "pending"
            receipt = next((row for row in receipts if row["phase"] == phase["phase"]), None)
            phase_views.append({
                **phase,
                "status": phase_status,
                "receipt": None
                if receipt is None
                else {
                    "receipt_id": receipt["receipt_id"],
                    "actual_mw": receipt["actual_mw"],
                    "note": receipt["note"],
                    "received_by": receipt["created_by"],
                    "received_at": receipt["created_at"],
                },
            })

        def phase_ref(position: int) -> dict[str, Any]:
            phase = phases[position]
            return {
                "phase": phase["phase"],
                "sequence": phase["sequence"],
                "title": phase["title"],
                "target_mw": phase["target_mw"],
            }

        current_phase = None
        if state in ("confirmed", "fallback"):
            current_phase = phase_ref(index)
        elif state == "completed":
            current_phase = phase_ref(len(phases) - 1)
        next_phase = None
        if state == "draft":
            next_phase = phase_ref(0)
        elif state in ("confirmed", "fallback") and index + 1 < len(phases):
            next_phase = phase_ref(index + 1)

        blocking: list[dict[str, Any]] = []
        if state in ("draft", "confirmed", "fallback") and stale:
            blocking.append({
                "kind": "basis_version",
                "title": "底层版本",
                "message": "机组能力、海况或通道设施版本已变化，旧计划不能继续推进",
                "required": snapshot["basis_sha256"],
                "available": basis_sha256,
            })
        if state == "draft":
            blocking.extend(reservation_blocking(capacity["sources"], capacity["requirements"]))
        elif state in ("confirmed", "fallback"):
            current_name = RAMP_PHASES[index]
            if current_name not in evidenced_phases:
                blocking.append({
                    "kind": "field_receipt",
                    "title": "现场回执",
                    "message": f"缺少 {current_name} 阶段的现场回执",
                    "required": current_name,
                    "available": None,
                })
            if index + 1 < len(phases):
                for condition in phases[index + 1]["entry_conditions"]:
                    if not condition["satisfied"]:
                        blocking.append({
                            "kind": condition["kind"],
                            "title": condition["title"],
                            "unit": condition["unit"],
                            "message": "下一阶段进入条件未满足",
                            "required": condition["required"],
                            "available": condition["available"],
                        })

        rollback = None
        if state in ("confirmed", "fallback"):
            evidenced_indexes = {RAMP_PHASES.index(phase) for phase in evidenced_phases}
            safe = max((item for item in evidenced_indexes if item <= index), default=0)
            rollback = {
                "safe_phase": RAMP_PHASES[safe],
                "safe_sequence": safe,
                "title": PHASE_TITLES[RAMP_PHASES[safe]],
                "rollback_margin_mw": phases[index]["rollback_margin_mw"],
                "accepted_receipts": len(receipts),
            }

        next_conditions: dict[str, Any] = {}
        if state == "draft":
            next_conditions = {item["kind"]: item for item in phases[0]["entry_conditions"]}
        elif state in ("confirmed", "fallback") and index + 1 < len(phases):
            next_conditions = {item["kind"]: item for item in phases[index + 1]["entry_conditions"]}
        binding = set(capacity["first_boundary"]["binding_sources"])
        sources = []
        for source in capacity["sources"]:
            condition = next_conditions.get(source["kind"])
            sources.append({
                **source,
                "reserved": mw_text(held_by_kind.get(source["kind"], Decimal("0"))),
                "required_next": None if condition is None else condition["required"],
                "blocking_next": any(item["kind"] == source["kind"] for item in blocking),
                "binding": source["kind"] in binding,
            })
        return {
            "plan_id": plan_id,
            "command_id": plan["command_id"],
            "snapshot_id": plan["snapshot_id"],
            "state": state,
            "revision": plan["revision"],
            "stale": stale,
            "basis_sha256": snapshot["basis_sha256"],
            "peak_target_mw": plan["peak_target_mw"],
            "aggregate_ramp_mw_per_min": capacity["aggregate_ramp_mw_per_min"],
            "current_phase": current_phase,
            "next_phase": next_phase,
            "first_boundary": capacity["first_boundary"],
            "capacity_sources": sources,
            "blocking_constraints": blocking,
            "rollback": rollback,
            "phases": phase_views,
            "reservations": [
                {
                    "source_kind": row["source_kind"],
                    "source_id": row["source_id"],
                    "reserved_amount": row["reserved_amount"],
                    "unit": row["unit"],
                    "state": row["state"],
                }
                for row in reservations
            ],
            "receipts": [
                {
                    "receipt_id": row["receipt_id"],
                    "phase": row["phase"],
                    "receipt_key": row["receipt_key"],
                    "actual_mw": row["actual_mw"],
                    "note": row["note"],
                    "created_by": row["created_by"],
                    "created_at": row["created_at"],
                }
                for row in receipts
            ],
        }

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute("SELECT * FROM supply_audit_events ORDER BY event_id").fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}
