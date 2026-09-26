"""结算单价、机组可用量、送出通道和提名的事务用例。"""

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
    IndexQuote,
    Facility,
    InventoryLot,
    NominationRequest,
    RehearsalPlanRequest,
    RehearsalSnapshotRequest,
    Route,
    SupplyScenario,
    decimal_value,
    identifier,
    required_text,
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
from .rehearsal import (
    REVISION_KINDS,
    build_stage_plan,
    constraint_ceilings,
    required_reservations,
    safe_rollback_target,
    total_ramp_mw_per_min,
)
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "planner": {"quote.write", "catalog.write", "scenario.write", "scenario.run"},
    "dispatcher": {
        "nomination.write", "allocation.run", "transfer.write", "inventory.write",
        "rehearsal.source", "rehearsal.write", "rehearsal.confirm", "rehearsal.advance",
        "rehearsal.receipt", "rehearsal.read",
    },
    "risk": {"outage.write", "scenario.approve", "report.read", "rehearsal.rollback", "rehearsal.read"},
    "auditor": {"report.read", "audit.read", "rehearsal.read"},
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

    # ---------- 调度预演阶段计划 ----------

    def register_source_update(self, actor_id: str, source_kind: str, note: str = "") -> dict[str, Any]:
        self._require(actor_id, "rehearsal.source")
        if source_kind not in REVISION_KINDS:
            raise ValidationFailed("source_kind 必须是 units、sea_state 或 corridors")
        with transaction(self.connection, immediate=True):
            row = self.connection.execute(
                "SELECT revision FROM rehearsal_source_states WHERE source_kind=?", (source_kind,)
            ).fetchone()
            revision = 1 if row is None else int(row["revision"]) + 1
            if row is None:
                self.connection.execute(
                    "INSERT INTO rehearsal_source_states(source_kind,revision,note,updated_by,updated_at) "
                    "VALUES(?,?,?,?,?)",
                    (source_kind, revision, note, actor_id, self._now()),
                )
            else:
                self.connection.execute(
                    "UPDATE rehearsal_source_states SET revision=?,note=?,updated_by=?,updated_at=? "
                    "WHERE source_kind=?",
                    (revision, note, actor_id, self._now(), source_kind),
                )
            self._audit(
                "rehearsal_source", source_kind, "rehearsal.source_updated", actor_id,
                {"revision": revision, "note": note},
            )
        return {"source_kind": source_kind, "revision": revision}

    def _source_revisions(self) -> dict[str, int]:
        rows = self.connection.execute(
            "SELECT source_kind,revision FROM rehearsal_source_states"
        ).fetchall()
        revisions = {row["source_kind"]: int(row["revision"]) for row in rows}
        return {kind: revisions.get(kind, 0) for kind in REVISION_KINDS}

    def create_rehearsal_snapshot(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "rehearsal.write")
        snapshot = RehearsalSnapshotRequest.from_dict(raw)
        definition = canonical_json(raw)
        content_sha256 = hashlib.sha256(definition.encode("utf-8")).hexdigest()
        revisions = self._source_revisions()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO rehearsal_snapshots(snapshot_id,label,observed_at,definition_json,content_sha256,"
                    "units_revision,sea_state_revision,corridors_revision,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        snapshot.snapshot_id,
                        snapshot.label,
                        snapshot.observed_at,
                        definition,
                        content_sha256,
                        revisions["units"],
                        revisions["sea_state"],
                        revisions["corridors"],
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit(
                    "rehearsal_snapshot", snapshot.snapshot_id, "rehearsal.snapshot_created",
                    actor_id, {"sha256": content_sha256},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("快照编号或内容已经存在") from exc
        return {
            "snapshot_id": snapshot.snapshot_id,
            "revision": 1,
            "source_revisions": revisions,
            "sha256": content_sha256,
        }

    def _rehearsal_snapshot(self, snapshot_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM rehearsal_snapshots WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()
        if row is None:
            raise NotFound("运行快照不存在")
        return row

    def _rehearsal_plan(self, plan_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM rehearsal_plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if row is None:
            raise NotFound("预演计划不存在")
        return row

    def _snapshot_revisions(self, snapshot: sqlite3.Row) -> dict[str, int]:
        return {kind: int(snapshot[f"{kind}_revision"]) for kind in REVISION_KINDS}

    def _snapshot_fresh(self, snapshot: sqlite3.Row) -> bool:
        return self._snapshot_revisions(snapshot) == self._source_revisions()

    def _snapshot_ceilings(self, snapshot: sqlite3.Row, derating: Mapping[str, Any]) -> dict[str, Any]:
        definition = json.loads(snapshot["definition_json"])
        return constraint_ceilings(
            units=definition["units"],
            corridors=definition["corridors"],
            sea_state=definition["sea_state"],
            derating=derating,
        )

    def create_rehearsal_plan(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "rehearsal.write")
        plan_request = RehearsalPlanRequest.from_dict(raw)
        request_digest = digest(raw)
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM supply_idempotency "
            "WHERE scope='rehearsal_plan' AND idempotency_key=?",
            (plan_request.idempotency_key,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_digest:
                raise Conflict("幂等键对应不同预演计划内容")
            return {**json.loads(stored["response_json"]), "replayed": True}
        snapshot = self._rehearsal_snapshot(plan_request.snapshot_id)
        if not self._snapshot_fresh(snapshot):
            raise InvalidState("快照对应的底层版本已变化，请重新固定快照")
        definition = json.loads(snapshot["definition_json"])
        derating = plan_request.derating.as_dict()
        ceilings = constraint_ceilings(
            units=definition["units"],
            corridors=definition["corridors"],
            sea_state=definition["sea_state"],
            derating=derating,
        )
        total_ramp = total_ramp_mw_per_min(definition["units"])
        points = list(plan_request.trajectory)
        for left, right in zip(points, points[1:]):
            slope = (right.target_mw - left.target_mw) / Decimal(right.offset_minutes - left.offset_minutes)
            if slope > total_ramp:
                raise ValidationFailed("目标轨迹爬坡速率超出机组能力")
        stage_plan = build_stage_plan(
            trajectory=[{"offset_minutes": p.offset_minutes, "target_mw": p.target_mw} for p in points],
            ceilings=ceilings,
            derating=derating,
        )
        response = {
            "plan_id": plan_request.plan_id,
            "snapshot_id": snapshot["snapshot_id"],
            "command_id": plan_request.command_id,
            "state": "draft",
            "revision": 1,
            "ceiling_mw": stage_plan["ceiling_mw"],
            "binding_constraints": stage_plan["binding_constraints"],
            "stages": stage_plan["stages"],
            "replayed": False,
        }
        trajectory_json = canonical_json([
            {"offset_minutes": p.offset_minutes, "target_mw": decimal_text(p.target_mw)} for p in points
        ])
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO rehearsal_plans(plan_id,snapshot_id,command_id,trajectory_json,derating_json,"
                    "stages_json,ceiling_mw,binding_constraints_json,idempotency_key,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        plan_request.plan_id,
                        snapshot["snapshot_id"],
                        plan_request.command_id,
                        trajectory_json,
                        canonical_json(derating),
                        canonical_json(stage_plan["stages"]),
                        stage_plan["ceiling_mw"],
                        canonical_json(stage_plan["binding_constraints"]),
                        plan_request.idempotency_key,
                        actor_id,
                        self._now(),
                    ),
                )
                self.connection.execute(
                    "INSERT INTO supply_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                    "VALUES('rehearsal_plan',?,?,?,?)",
                    (plan_request.idempotency_key, request_digest, canonical_json(response), self._now()),
                )
                self._audit(
                    "rehearsal_plan", plan_request.plan_id, "rehearsal.plan_created", actor_id,
                    {"snapshot_id": snapshot["snapshot_id"], "request_sha256": request_digest},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("预演计划编号或幂等键冲突") from exc
        return response

    def _active_pool_total(self, pool: str) -> Decimal:
        rows = self.connection.execute(
            "SELECT amount FROM rehearsal_reservations WHERE state='active' AND pool=?", (pool,)
        ).fetchall()
        return sum((Decimal(row["amount"]) for row in rows), Decimal("0"))

    def _counted_receipt_stages(self, plan_id: str) -> set[int]:
        rows = self.connection.execute(
            "SELECT stage_index FROM rehearsal_receipts WHERE plan_id=? AND state='counted'", (plan_id,)
        ).fetchall()
        return {int(row["stage_index"]) for row in rows}

    def _stage_event(
        self, plan_id: str, stage_index: int, event_type: str, actor_id: str, detail: Mapping[str, Any]
    ) -> None:
        self.connection.execute(
            "INSERT INTO rehearsal_stage_events(plan_id,stage_index,event_type,detail_json,actor_id,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (plan_id, stage_index, event_type, canonical_json(detail), actor_id, self._now()),
        )

    def confirm_rehearsal_plan(self, actor_id: str, plan_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "rehearsal.confirm")
        with transaction(self.connection, immediate=True):
            plan = self._rehearsal_plan(plan_id)
            if plan["state"] != "draft" or int(plan["revision"]) != expected_revision:
                raise InvalidState("计划不是当前草稿版本")
            snapshot = self._rehearsal_snapshot(plan["snapshot_id"])
            if not self._snapshot_fresh(snapshot):
                raise InvalidState("底层版本已变化，旧计划不能继续推进")
            ceilings = self._snapshot_ceilings(snapshot, json.loads(plan["derating_json"]))
            stages = json.loads(plan["stages_json"])
            reservations = required_reservations(stages=stages, ceilings=ceilings)
            pools = ceilings["pools"]
            for pool_name in ("turbine_capability", "cable_thermal", "reactive_compensation"):
                active = self._active_pool_total(pool_name)
                incoming = sum(
                    (Decimal(item["amount"]) for item in reservations if item["pool"] == pool_name),
                    Decimal("0"),
                )
                capacity = Decimal(str(pools[pool_name]["capacity"]))
                if active + incoming > capacity:
                    raise Conflict(
                        f"容量池 {pool_name} 不足：已预留 {active}，本次需要 {incoming}，上限 {capacity}"
                    )
            now = self._now()
            for item in reservations:
                self.connection.execute(
                    "INSERT INTO rehearsal_reservations(plan_id,source_kind,pool,amount,unit,state,created_at) "
                    "VALUES(?,?,?,?,?,'active',?)",
                    (plan_id, item["source_kind"], item["pool"], item["amount"], item["unit"], now),
                )
            self.connection.execute(
                "UPDATE rehearsal_plans SET state='confirmed',current_stage_index=0,revision=revision+1 "
                "WHERE plan_id=?",
                (plan_id,),
            )
            self._stage_event(plan_id, 0, "entered", actor_id, {"reason": "plan_confirmed"})
            self._audit(
                "rehearsal_plan", plan_id, "rehearsal.plan_confirmed", actor_id,
                {"reservations": reservations},
            )
        return {
            "plan_id": plan_id,
            "state": "confirmed",
            "current_stage_index": 0,
            "revision": expected_revision + 1,
            "reservations": reservations,
        }

    def advance_rehearsal_plan(self, actor_id: str, plan_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "rehearsal.advance")
        with transaction(self.connection, immediate=True):
            plan = self._rehearsal_plan(plan_id)
            if plan["state"] not in ("confirmed", "in_progress"):
                raise InvalidState("计划当前不可推进")
            if int(plan["revision"]) != expected_revision:
                raise InvalidState("计划版本已变化")
            snapshot = self._rehearsal_snapshot(plan["snapshot_id"])
            if not self._snapshot_fresh(snapshot):
                raise InvalidState("底层版本已变化，旧计划不能继续推进")
            stages = json.loads(plan["stages_json"])
            current = int(plan["current_stage_index"])
            if current not in self._counted_receipt_stages(plan_id):
                raise InvalidState("当前阶段尚未收到现场回执")
            next_index = current + 1
            if next_index >= len(stages):
                raise InvalidState("计划已到达最终阶段")
            self.connection.execute(
                "UPDATE rehearsal_plans SET state='in_progress',current_stage_index=?,revision=revision+1 "
                "WHERE plan_id=?",
                (next_index, plan_id),
            )
            self._stage_event(plan_id, next_index, "entered", actor_id, {})
            self._audit(
                "rehearsal_plan", plan_id, "rehearsal.stage_advanced", actor_id,
                {"stage_index": next_index},
            )
        stage = stages[next_index]
        return {
            "plan_id": plan_id,
            "state": "in_progress",
            "current_stage_index": next_index,
            "current_stage": stage["code"],
            "revision": expected_revision + 1,
        }

    def record_rehearsal_receipt(
        self, actor_id: str, plan_id: str, raw: Mapping[str, Any]
    ) -> dict[str, Any]:
        self._require(actor_id, "rehearsal.receipt")
        receipt_id = identifier(raw.get("receipt_id"), "receipt_id")
        stage_index = raw.get("stage_index")
        if isinstance(stage_index, bool) or not isinstance(stage_index, int) or stage_index < 0:
            raise ValidationFailed("stage_index 必须是非负整数")
        outcome = required_text(raw.get("outcome"), "outcome", 16)
        if outcome != "executed":
            raise ValidationFailed("outcome 仅支持 executed")
        detail = raw.get("detail", {})
        if not isinstance(detail, Mapping):
            raise ValidationFailed("detail 必须是对象")
        request_digest = digest(raw)
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM rehearsal_receipts WHERE plan_id=? AND receipt_id=?",
            (plan_id, receipt_id),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_digest:
                raise Conflict("回执编号对应不同内容")
            return {**json.loads(stored["response_json"]), "replayed": True}
        try:
            with transaction(self.connection, immediate=True):
                plan = self._rehearsal_plan(plan_id)
                if plan["state"] not in ("confirmed", "in_progress"):
                    raise InvalidState("计划当前不可登记回执")
                current = int(plan["current_stage_index"])
                if stage_index != current:
                    raise Conflict("回执阶段与计划当前阶段不符")
                if current in self._counted_receipt_stages(plan_id):
                    raise Conflict("当前阶段已有生效回执")
                stages = json.loads(plan["stages_json"])
                completed = stage_index == len(stages) - 1
                new_state = "completed" if completed else "in_progress"
                response = {
                    "receipt_id": receipt_id,
                    "plan_id": plan_id,
                    "stage_index": stage_index,
                    "plan_state": new_state,
                    "plan_revision": int(plan["revision"]) + 1,
                    "replayed": False,
                }
                now = self._now()
                self.connection.execute(
                    "INSERT INTO rehearsal_receipts(receipt_id,plan_id,stage_index,outcome,detail_json,"
                    "request_sha256,response_json,state,received_by,received_at) "
                    "VALUES(?,?,?,?,?,?,?,'counted',?,?)",
                    (
                        receipt_id,
                        plan_id,
                        stage_index,
                        outcome,
                        canonical_json(detail),
                        request_digest,
                        canonical_json(response),
                        actor_id,
                        now,
                    ),
                )
                self.connection.execute(
                    "UPDATE rehearsal_plans SET state=?,revision=revision+1 WHERE plan_id=?",
                    (new_state, plan_id),
                )
                self._stage_event(plan_id, stage_index, "executed", actor_id, {"receipt_id": receipt_id})
                if completed:
                    self.connection.execute(
                        "UPDATE rehearsal_reservations SET state='released',released_at=? "
                        "WHERE plan_id=? AND state='active'",
                        (now, plan_id),
                    )
                    self._stage_event(plan_id, stage_index, "completed", actor_id, {})
                self._audit(
                    "rehearsal_plan", plan_id, "rehearsal.receipt_recorded", actor_id,
                    {"receipt_id": receipt_id, "stage_index": stage_index},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("回执编号冲突") from exc
        return response

    def rollback_rehearsal_plan(
        self, actor_id: str, plan_id: str, raw: Mapping[str, Any]
    ) -> dict[str, Any]:
        self._require(actor_id, "rehearsal.rollback")
        reason = required_text(raw.get("reason"), "reason", 256)
        override = raw.get("ceiling_override_mw")
        override_decimal = None if override is None else decimal_value(
            override, "ceiling_override_mw", minimum=Decimal("0")
        )
        requested_target = raw.get("to_stage_index")
        if requested_target is not None and (
            isinstance(requested_target, bool) or not isinstance(requested_target, int) or requested_target < -1
        ):
            raise ValidationFailed("to_stage_index 必须是 -1 或更大的整数")
        with transaction(self.connection, immediate=True):
            plan = self._rehearsal_plan(plan_id)
            if plan["state"] not in ("confirmed", "in_progress"):
                raise InvalidState("计划当前不可回退")
            stages = json.loads(plan["stages_json"])
            current = int(plan["current_stage_index"])
            planned_ceiling = Decimal(plan["ceiling_mw"])
            effective = planned_ceiling if override_decimal is None else min(planned_ceiling, override_decimal)
            executed = self._counted_receipt_stages(plan_id)
            target = safe_rollback_target(
                stages=stages,
                executed_indexes=executed,
                current_index=current,
                effective_ceiling_mw=effective,
            )
            if requested_target is not None:
                if requested_target > target:
                    raise InvalidState("回退目标超出仍安全的阶段")
                target = requested_target
            now = self._now()
            # 保留已执行证据：回执行不删除，仅将高于目标阶段的生效回执标记为 superseded
            self.connection.execute(
                "UPDATE rehearsal_receipts SET state='superseded' "
                "WHERE plan_id=? AND stage_index>? AND state='counted'",
                (plan_id, target),
            )
            for index in range(target + 1, current + 1):
                self._stage_event(plan_id, index, "retreated", actor_id, {"reason": reason})
            new_state = "aborted" if target < 0 else "in_progress"
            if target < 0:
                self.connection.execute(
                    "UPDATE rehearsal_reservations SET state='released',released_at=? "
                    "WHERE plan_id=? AND state='active'",
                    (now, plan_id),
                )
            self.connection.execute(
                "UPDATE rehearsal_plans SET state=?,current_stage_index=?,revision=revision+1 WHERE plan_id=?",
                (new_state, target, plan_id),
            )
            self._stage_event(
                plan_id, target, "rolled_back", actor_id,
                {
                    "reason": reason,
                    "from_stage_index": current,
                    "effective_ceiling_mw": decimal_text(effective),
                },
            )
            self._audit(
                "rehearsal_plan", plan_id, "rehearsal.rolled_back", actor_id,
                {"reason": reason, "from": current, "to": target},
            )
        return {
            "plan_id": plan_id,
            "state": new_state,
            "current_stage_index": target,
            "rolled_back_from": current,
            "revision": int(plan["revision"]) + 1,
        }

    def rehearsal_plan_status(self, actor_id: str, plan_id: str) -> dict[str, Any]:
        self._require(actor_id, "rehearsal.read")
        plan = self._rehearsal_plan(plan_id)
        snapshot = self._rehearsal_snapshot(plan["snapshot_id"])
        ceilings = self._snapshot_ceilings(snapshot, json.loads(plan["derating_json"]))
        stages = json.loads(plan["stages_json"])
        current = int(plan["current_stage_index"])
        fresh = self._snapshot_fresh(snapshot)
        executed = self._counted_receipt_stages(plan_id)
        planned_ceiling = Decimal(plan["ceiling_mw"])
        receipt_rows = self.connection.execute(
            "SELECT * FROM rehearsal_receipts WHERE plan_id=? ORDER BY received_at,receipt_id", (plan_id,)
        ).fetchall()
        superseded = {int(row["stage_index"]) for row in receipt_rows if row["state"] == "superseded"}
        reservation_rows = self.connection.execute(
            "SELECT * FROM rehearsal_reservations WHERE plan_id=? ORDER BY reservation_id", (plan_id,)
        ).fetchall()
        has_active_reservation = any(row["state"] == "active" for row in reservation_rows)
        basis = {item["source_kind"]: item["basis"] for item in ceilings["constraints"]}
        pools = ceilings["pools"]
        pool_totals = {pool: self._active_pool_total(pool) for pool in pools}
        capacity_sources = []
        for row in reservation_rows:
            pool = row["pool"]
            capacity = Decimal(str(pools[pool]["capacity"]))
            capacity_sources.append({
                "source_kind": row["source_kind"],
                "reserved": row["amount"],
                "unit": row["unit"],
                "state": row["state"],
                "pool": pool,
                "pool_capacity": decimal_text(quantize_volume(capacity)),
                "pool_active_reserved": decimal_text(quantize_volume(pool_totals[pool])),
                "pool_remaining": decimal_text(quantize_volume(capacity - pool_totals[pool])),
                "basis": basis.get(row["source_kind"], ""),
            })
        active = plan["state"] in ("confirmed", "in_progress")
        stage_views = []
        for stage in stages:
            index = int(stage["index"])
            if index in executed:
                stage_state = "executed"
            elif index == current and active:
                stage_state = "entered"
            elif index in superseded:
                stage_state = "retreated"
            else:
                stage_state = "pending"
            conditions = [
                {"code": "snapshot_fresh", "met": fresh},
                {"code": "reservations_active", "met": has_active_reservation},
                {
                    "code": "headroom_sufficient",
                    "met": Decimal(stage["required_headroom_mw"]) <= planned_ceiling,
                },
            ]
            if index >= 1:
                conditions.append({"code": "previous_stage_executed", "met": (index - 1) in executed})
            stage_views.append({**stage, "state": stage_state, "entry_conditions": conditions})
        blocking: list[dict[str, Any]] = []
        if active:
            if not fresh:
                blocking.append({"code": "snapshot_stale", "detail": "底层版本已变化，旧计划不能继续推进"})
            if current not in executed:
                blocking.append({
                    "code": "receipt_missing",
                    "stage_index": current,
                    "detail": "当前阶段尚未收到现场回执",
                })
            next_index = current + 1
            if next_index < len(stages) and not stages[next_index]["reachable"]:
                for item in stages[next_index]["blocking_constraints"]:
                    blocking.append({"code": "capacity_boundary", **item, "detail": "目标功率超出该约束边界"})
        current_view = None
        if 0 <= current < len(stages):
            stage = stages[current]
            current_view = {
                "index": current,
                "code": stage["code"],
                "label": stage["label"],
                "target_mw": stage["target_mw"],
                "executed": current in executed,
            }
        if active:
            rollback_target = safe_rollback_target(
                stages=stages,
                executed_indexes=executed,
                current_index=current,
                effective_ceiling_mw=planned_ceiling,
            )
            rollback_info: dict[str, Any] = {
                "target_stage_index": rollback_target,
                "target_stage": "abort" if rollback_target < 0 else stages[rollback_target]["code"],
                "current_stage_safe": current >= 0
                and Decimal(stages[current]["required_headroom_mw"]) <= planned_ceiling,
            }
        else:
            rollback_info = {"target_stage_index": None, "target_stage": None, "current_stage_safe": None}
        event_rows = self.connection.execute(
            "SELECT stage_index,event_type,detail_json,actor_id,created_at FROM rehearsal_stage_events "
            "WHERE plan_id=? ORDER BY event_id",
            (plan_id,),
        ).fetchall()
        return {
            "plan_id": plan["plan_id"],
            "command_id": plan["command_id"],
            "state": plan["state"],
            "revision": int(plan["revision"]),
            "snapshot": {
                "snapshot_id": snapshot["snapshot_id"],
                "stale": not fresh,
                "source_revisions": self._snapshot_revisions(snapshot),
                "current_revisions": self._source_revisions(),
            },
            "ceiling_mw": plan["ceiling_mw"],
            "binding_constraints": json.loads(plan["binding_constraints_json"]),
            "current_stage": current_view,
            "stages": stage_views,
            "capacity_sources": capacity_sources,
            "blocking_constraints": blocking,
            "rollback": rollback_info,
            "receipts": [
                {
                    "receipt_id": row["receipt_id"],
                    "stage_index": int(row["stage_index"]),
                    "state": row["state"],
                    "received_at": row["received_at"],
                }
                for row in receipt_rows
            ],
            "events": [
                {
                    "stage_index": int(row["stage_index"]),
                    "event_type": row["event_type"],
                    "detail": json.loads(row["detail_json"]),
                    "actor_id": row["actor_id"],
                    "created_at": row["created_at"],
                }
                for row in event_rows
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
