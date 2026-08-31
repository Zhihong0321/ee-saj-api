"""Synchronize the SAJ plant/device catalog and link plants to customers.

Phase 1 deliberately uses only a conservative exact normalized-name match:

    SAJ plantName -> customer.name -> customer.customer_id

One customer may own any number of plants. Existing plant links and existing
device mappings are preserved; conflicting or ambiguous candidates are reported
for later invoice-assisted/manual resolution.

Dry-run is the default. Production writes require both ``--apply`` and a direct
``DATABASE_URL`` so a read-only HTTP proxy cannot be mistaken for write access.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import pg
from saj_api import SajClient


MATCH_METHOD = "name_exact"
MATCH_CONFIDENCE = 0.9


def norm_name(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def to_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _rows(result: dict, label: str) -> list[dict]:
    if "error" in result:
        raise RuntimeError(f"{label} failed: {result.get('detail') or result['error']}")
    return result.get("rows") or []


def _rowcount(result: dict) -> int:
    value = result.get("rowcount", result.get("rowCount", 0))
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


@dataclass
class SyncPlan:
    plant_rows: list[tuple] = field(default_factory=list)
    device_rows: list[tuple] = field(default_factory=list)
    plant_links: list[tuple[str, str]] = field(default_factory=list)
    device_maps: list[tuple[str, str, str, str, float, bool]] = field(default_factory=list)
    existing_links: int = 0
    unmatched_plants: list[str] = field(default_factory=list)
    ambiguous_plants: list[str] = field(default_factory=list)
    conflict_plants: list[str] = field(default_factory=list)
    invalid_existing_links: list[str] = field(default_factory=list)
    multi_plant_customers: int = 0

    def summary(self, applied: dict | None = None) -> dict:
        result = {
            "mode": "apply" if applied is not None else "dry-run",
            "portal_plants": len(self.plant_rows),
            "portal_devices": len(self.device_rows),
            "existing_plant_links": self.existing_links,
            "planned_plant_links": len(self.plant_links),
            "planned_device_maps": len(self.device_maps),
            "customers_with_multiple_plants": self.multi_plant_customers,
            "unmatched_plants": len(self.unmatched_plants),
            "ambiguous_plants": len(self.ambiguous_plants),
            "conflict_plants": len(self.conflict_plants),
            "invalid_existing_links": len(self.invalid_existing_links),
            "review_plant_uids": {
                "ambiguous": self.ambiguous_plants[:20],
                "conflict": self.conflict_plants[:20],
                "invalid_existing": self.invalid_existing_links[:20],
            },
        }
        if applied is not None:
            result["applied"] = applied
        return result


def fetch_catalog(client: SajClient, page_size: int = 100) -> tuple[list[dict], list[dict]]:
    plants = client.list_plants(page_size=page_size)
    devices = client.list_inverters(page_size=page_size)
    return plants, devices


def load_database_state() -> tuple[list[dict], list[dict], list[dict]]:
    customers = _rows(
        pg.run("select customer_id, name from customer where name is not null and name <> ''"),
        "load customers",
    )
    plants = _rows(
        pg.run("select plant_uid, customer_id from saj_plant"),
        "load plant links",
    )
    mappings = _rows(
        pg.run(
            "select customer_id, device_sn, plant_uid, match_method, verified "
            "from saj_customer_device_map"
        ),
        "load device mappings",
    )
    return customers, plants, mappings


def build_plan(
    portal_plants: list[dict],
    portal_devices: list[dict],
    customers: list[dict],
    stored_plants: list[dict],
    stored_mappings: list[dict],
) -> SyncPlan:
    plan = SyncPlan()
    customer_ids = {str(row["customer_id"]) for row in customers}
    customers_by_name: dict[str, list[str]] = defaultdict(list)
    for row in customers:
        key = norm_name(row.get("name"))
        if key:
            customers_by_name[key].append(str(row["customer_id"]))

    stored_customer_by_plant = {
        str(row["plant_uid"]): str(row["customer_id"])
        for row in stored_plants
        if row.get("plant_uid") and row.get("customer_id") is not None
    }
    maps_by_device: dict[str, list[dict]] = defaultdict(list)
    for row in stored_mappings:
        if row.get("device_sn"):
            maps_by_device[str(row["device_sn"])].append(row)

    devices_by_plant: dict[str, list[str]] = defaultdict(list)
    seen_devices: set[str] = set()
    for device in portal_devices:
        sn = str(device.get("deviceSn") or "").strip()
        plant_uid = str(device.get("plantUid") or "").strip()
        if not sn or sn in seen_devices:
            continue
        seen_devices.add(sn)
        if plant_uid:
            devices_by_plant[plant_uid].append(sn)
        # Only refresh membership here. Model/type/alias fields are populated by
        # their dedicated metadata path and must not be erased by sparse fleet rows.
        plan.device_rows.append((sn, plant_uid or None))

    assignments: dict[str, str] = {}
    seen_plants: set[str] = set()
    for plant in portal_plants:
        plant_uid = str(plant.get("plantUid") or "").strip()
        if not plant_uid or plant_uid in seen_plants:
            continue
        seen_plants.add(plant_uid)
        plant_name = plant.get("plantName")
        running_state = plant.get("runningState")
        plan.plant_rows.append((
            plant_uid,
            plant_name,
            plant.get("ownerName"),
            plant.get("installerName"),
            to_float(plant.get("pvPower")),
            str(running_state) if running_state is not None else None,
            plant.get("typeName"),
        ))

        existing_customer = stored_customer_by_plant.get(plant_uid)
        if existing_customer is not None:
            if existing_customer not in customer_ids:
                plan.invalid_existing_links.append(plant_uid)
                continue
            customer_id = existing_customer
            plan.existing_links += 1
        else:
            matches = sorted(set(customers_by_name.get(norm_name(plant_name), [])))
            if not matches:
                plan.unmatched_plants.append(plant_uid)
                continue
            if len(matches) != 1:
                plan.ambiguous_plants.append(plant_uid)
                continue
            customer_id = matches[0]

        device_sns = devices_by_plant.get(plant_uid, [])
        conflict = False
        for sn in device_sns:
            existing_customers = {
                str(row["customer_id"]) for row in maps_by_device.get(sn, [])
                if row.get("customer_id") is not None
            }
            if existing_customers and existing_customers != {customer_id}:
                conflict = True
                break
        if conflict:
            plan.conflict_plants.append(plant_uid)
            continue

        assignments[plant_uid] = customer_id
        if existing_customer is None:
            plan.plant_links.append((plant_uid, customer_id))
        for sn in device_sns:
            if not maps_by_device.get(sn):
                plan.device_maps.append((customer_id, sn, plant_uid,
                                         MATCH_METHOD, MATCH_CONFIDENCE, False))

    plants_per_customer: dict[str, int] = defaultdict(int)
    for customer_id in assignments.values():
        plants_per_customer[customer_id] += 1
    plan.multi_plant_customers = sum(1 for count in plants_per_customer.values() if count > 1)
    return plan


def _values_placeholders(rows: list[tuple], width: int) -> tuple[str, list[Any]]:
    placeholders = []
    params: list[Any] = []
    for row_index, row in enumerate(rows):
        start = row_index * width + 1
        placeholders.append("(" + ",".join(f"${start + i}" for i in range(width)) + ")")
        params.extend(row)
    return ",".join(placeholders), params


def insert_device_maps(rows: list[tuple], batch_size: int = 200) -> int:
    total = 0
    for offset in range(0, len(rows), batch_size):
        chunk = rows[offset:offset + batch_size]
        values, params = _values_placeholders(chunk, 6)
        result = pg.run(
            "insert into saj_customer_device_map "
            "(customer_id,device_sn,plant_uid,match_method,confidence,verified) "
            f"select v.* from (values {values}) "
            "as v(customer_id,device_sn,plant_uid,match_method,confidence,verified) "
            "where not exists (select 1 from saj_customer_device_map m "
            "                  where m.device_sn=v.device_sn) "
            "on conflict (customer_id,device_sn) do nothing",
            params,
        )
        if "error" in result:
            raise RuntimeError(f"insert device mappings failed: {result.get('detail')}")
        total += _rowcount(result)
    return total


def link_plants(rows: list[tuple], batch_size: int = 200) -> int:
    total = 0
    for offset in range(0, len(rows), batch_size):
        chunk = rows[offset:offset + batch_size]
        values, params = _values_placeholders(chunk, 2)
        result = pg.run(
            f"update saj_plant p set customer_id=v.customer_id from (values {values}) "
            "as v(plant_uid,customer_id) "
            "where p.plant_uid=v.plant_uid and p.customer_id is null "
            "and exists (select 1 from customer c where c.customer_id=v.customer_id)",
            params,
        )
        if "error" in result:
            raise RuntimeError(f"link plants failed: {result.get('detail')}")
        total += _rowcount(result)
    return total


def apply_plan(plan: SyncPlan) -> dict:
    if not os.environ.get("DATABASE_URL"):
        raise RuntimeError("--apply requires a direct DATABASE_URL; proxy mode is not accepted")
    pg.upsert(
        "saj_plant",
        ["plant_uid", "plant_name", "owner_name", "installer_name",
         "pv_power_wp", "running_state", "type_name"],
        plan.plant_rows,
        "plant_uid",
        ["plant_name", "owner_name", "installer_name", "pv_power_wp",
         "running_state", "type_name"],
    )
    pg.upsert(
        "saj_device",
        ["device_sn", "plant_uid"],
        plan.device_rows,
        "device_sn",
        ["plant_uid"],
    )
    maps_written = insert_device_maps(plan.device_maps)
    plants_linked = link_plants(plan.plant_links)
    return {
        "catalog_plants_sent": len(plan.plant_rows),
        "catalog_devices_sent": len(plan.device_rows),
        "device_maps_inserted": maps_written,
        "plants_linked": plants_linked,
    }


def run(apply: bool = False, page_size: int = 100, db_lock=None) -> dict:
    username = os.environ.get("SAJ_USER")
    password = os.environ.get("SAJ_PASS")
    client = SajClient(username=username, password=password) if username and password else SajClient()
    portal_plants, portal_devices = fetch_catalog(client, page_size=page_size)
    if db_lock is None:
        customers, stored_plants, stored_mappings = load_database_state()
        plan = build_plan(portal_plants, portal_devices, customers, stored_plants, stored_mappings)
        applied = apply_plan(plan) if apply else None
    else:
        # pg.py owns one shared connection. Backfill passes its DB lock so status
        # polling cannot use that connection while this reconciliation writes.
        with db_lock:
            customers, stored_plants, stored_mappings = load_database_state()
            plan = build_plan(portal_plants, portal_devices, customers, stored_plants, stored_mappings)
            applied = apply_plan(plan) if apply else None
    return plan.summary(applied)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="write the reviewed plan; default is dry-run")
    parser.add_argument("--page-size", type=int, default=100)
    args = parser.parse_args()
    summary = run(apply=args.apply, page_size=args.page_size)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if not summary["conflict_plants"] and not summary["invalid_existing_links"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
