import datetime as dt
import unittest
from unittest.mock import MagicMock, patch

import backfill
import sync_customer_plants as customer_sync
from backfill_page import PAGE


SYNC_RESULT = {
    "plants": 9,
    "devices": 12,
    "plant_links": 2,
    "device_maps": 3,
    "unmatched": 1,
    "ambiguous": 0,
    "conflicts": 1,
}


class BackfillCatalogSyncTests(unittest.TestCase):
    def test_customer_sync_holds_supplied_lock_only_for_database_phase(self):
        events = []

        class RecordingLock:
            def __enter__(self):
                events.append("lock")

            def __exit__(self, *_args):
                events.append("unlock")

        plan = customer_sync.SyncPlan()
        with (
            patch.object(customer_sync, "SajClient"),
            patch.object(customer_sync, "fetch_catalog",
                         side_effect=lambda *_args, **_kwargs: events.append("fetch") or ([], [])),
            patch.object(customer_sync, "load_database_state",
                         side_effect=lambda: events.append("load") or ([], [], [])),
            patch.object(customer_sync, "build_plan",
                         side_effect=lambda *_args: events.append("build") or plan),
            patch.object(customer_sync, "apply_plan",
                         side_effect=lambda *_args: events.append("apply") or {}),
        ):
            customer_sync.run(apply=True, db_lock=RecordingLock())

        self.assertEqual(events, ["fetch", "lock", "load", "build", "apply", "unlock"])

    def test_sync_catalog_applies_with_backfill_db_lock(self):
        summary = {
            "portal_plants": 9,
            "portal_devices": 12,
            "unmatched_plants": 1,
            "ambiguous_plants": 2,
            "conflict_plants": 3,
            "invalid_existing_links": 4,
            "applied": {"plants_linked": 5, "device_maps_inserted": 6},
        }
        with patch.object(backfill.sync_customer_plants, "run", return_value=summary) as run:
            result = backfill._sync_catalog()

        run.assert_called_once_with(apply=True, db_lock=backfill._db_lock)
        self.assertEqual(result["plant_links"], 5)
        self.assertEqual(result["device_maps"], 6)
        self.assertEqual(result["conflicts"], 7)

    def test_start_syncs_before_seeding_history_workers(self):
        events = []
        fake_thread = MagicMock()
        fake_thread.start.side_effect = lambda: events.append("worker")
        with (
            patch.object(backfill, "PASSWORD", "secret"),
            patch.object(backfill, "USERS", ["operation02"]),
            patch.object(backfill, "ensure_schema"),
            patch.object(backfill, "job", return_value=None),
            patch.object(backfill, "policy_floor", return_value=dt.date(2026, 4, 1)),
            patch.object(backfill, "_window_end", return_value=dt.date(2026, 8, 13)),
            patch.object(backfill, "_db", return_value={"rowcount": 1}),
            patch.object(backfill, "_sync_catalog",
                         side_effect=lambda: events.append("sync") or SYNC_RESULT),
            patch.object(backfill, "_seed_devices",
                         side_effect=lambda: events.append("seed") or 2),
            patch.object(backfill, "status", return_value={"state": "running"}),
            patch.object(backfill.threading, "Thread", return_value=fake_thread),
            patch.object(backfill, "_threads", []),
        ):
            result = backfill._start()

        self.assertEqual(events, ["sync", "seed", "worker"])
        self.assertEqual(result["status"], "started")
        fake_thread.start.assert_called_once_with()

    def test_sync_failure_marks_job_failed_and_does_not_seed(self):
        with (
            patch.object(backfill, "PASSWORD", "secret"),
            patch.object(backfill, "ensure_schema"),
            patch.object(backfill, "job", return_value=None),
            patch.object(backfill, "policy_floor", return_value=dt.date(2026, 4, 1)),
            patch.object(backfill, "_window_end", return_value=dt.date(2026, 8, 13)),
            patch.object(backfill, "_db", return_value={"rowcount": 1}),
            patch.object(backfill, "_sync_catalog", side_effect=RuntimeError("SAJ unavailable")),
            patch.object(backfill, "_set_state") as set_state,
            patch.object(backfill, "_seed_devices") as seed,
            patch.object(backfill.threading, "Thread") as thread,
        ):
            with self.assertRaisesRegex(RuntimeError, "catalog/customer sync failed"):
                backfill._start()

        seed.assert_not_called()
        thread.assert_not_called()
        self.assertEqual(set_state.call_args.args[0], "failed")
        self.assertEqual(set_state.call_args.kwargs["sync_state"], "failed")

    def test_backfill_page_exposes_sync_progress(self):
        for element_id in ("syncstate", "synccatalog", "synclinks", "syncreview", "syncerror"):
            self.assertIn(f'id="{element_id}"', PAGE)


if __name__ == "__main__":
    unittest.main()
