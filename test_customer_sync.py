import os
import unittest
from unittest.mock import patch

import sync_customer_plants as sync


class CustomerPlantPlanTests(unittest.TestCase):
    def test_one_customer_can_receive_multiple_plants(self):
        customers = [{"customer_id": "C1", "name": "Alice Solar"}]
        plants = [
            {"plantUid": "P1", "plantName": "Alice Solar"},
            {"plantUid": "P2", "plantName": "ALICE-SOLAR"},
        ]
        devices = [
            {"deviceSn": "D1", "plantUid": "P1"},
            {"deviceSn": "D2", "plantUid": "P2"},
        ]

        plan = sync.build_plan(plants, devices, customers, [], [])

        self.assertEqual(plan.plant_links, [("P1", "C1"), ("P2", "C1")])
        self.assertEqual(
            [(row[0], row[1], row[2]) for row in plan.device_maps],
            [("C1", "D1", "P1"), ("C1", "D2", "P2")],
        )
        self.assertEqual(plan.multi_plant_customers, 1)

    def test_duplicate_customer_name_is_ambiguous(self):
        customers = [
            {"customer_id": "C1", "name": "Same Name"},
            {"customer_id": "C2", "name": "Same Name"},
        ]
        plants = [{"plantUid": "P1", "plantName": "Same Name"}]

        plan = sync.build_plan(plants, [], customers, [], [])

        self.assertEqual(plan.ambiguous_plants, ["P1"])
        self.assertEqual(plan.plant_links, [])

    def test_existing_valid_plant_link_is_authoritative(self):
        customers = [{"customer_id": "C1", "name": "Original Name"}]
        plants = [{"plantUid": "P1", "plantName": "Renamed Plant"}]
        devices = [{"deviceSn": "D1", "plantUid": "P1"}]
        stored_plants = [{"plant_uid": "P1", "customer_id": "C1"}]

        plan = sync.build_plan(plants, devices, customers, stored_plants, [])

        self.assertEqual(plan.existing_links, 1)
        self.assertEqual(plan.plant_links, [])
        self.assertEqual(plan.device_maps[0][:3], ("C1", "D1", "P1"))
        self.assertEqual(plan.unmatched_plants, [])

    def test_conflicting_device_mapping_blocks_new_plant_link(self):
        customers = [
            {"customer_id": "C1", "name": "Alice"},
            {"customer_id": "C2", "name": "Bob"},
        ]
        plants = [{"plantUid": "P1", "plantName": "Alice"}]
        devices = [{"deviceSn": "D1", "plantUid": "P1"}]
        mappings = [{
            "customer_id": "C2",
            "device_sn": "D1",
            "plant_uid": "P0",
            "match_method": "manual",
            "verified": True,
        }]

        plan = sync.build_plan(plants, devices, customers, [], mappings)

        self.assertEqual(plan.conflict_plants, ["P1"])
        self.assertEqual(plan.plant_links, [])
        self.assertEqual(plan.device_maps, [])

    def test_invalid_existing_customer_is_reported_not_overwritten(self):
        customers = [{"customer_id": "C1", "name": "Alice"}]
        plants = [{"plantUid": "P1", "plantName": "Alice"}]
        stored_plants = [{"plant_uid": "P1", "customer_id": "MISSING"}]

        plan = sync.build_plan(plants, [], customers, stored_plants, [])

        self.assertEqual(plan.invalid_existing_links, ["P1"])
        self.assertEqual(plan.plant_links, [])

    def test_duplicate_portal_devices_are_deduplicated(self):
        customers = [{"customer_id": "C1", "name": "Alice"}]
        plants = [{"plantUid": "P1", "plantName": "Alice"}]
        devices = [
            {"deviceSn": "D1", "plantUid": "P1"},
            {"deviceSn": "D1", "plantUid": "P1"},
        ]

        plan = sync.build_plan(plants, devices, customers, [], [])

        self.assertEqual(len(plan.device_rows), 1)
        self.assertEqual(len(plan.device_maps), 1)

    def test_apply_requires_direct_database_url(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "direct DATABASE_URL"):
                sync.apply_plan(sync.SyncPlan())

    def test_device_insert_has_cross_customer_conflict_guard(self):
        rows = [("C1", "D1", "P1", "name_exact", 0.9, False)]
        with patch.object(sync.pg, "run", return_value={"rowcount": 1}) as run:
            written = sync._insert_device_maps(rows)

        sql, params = run.call_args.args
        self.assertIn("where m.device_sn=v.device_sn", sql)
        self.assertIn("on conflict (customer_id,device_sn) do nothing", sql)
        self.assertEqual(params, list(rows[0]))
        self.assertEqual(written, 1)


if __name__ == "__main__":
    unittest.main()
