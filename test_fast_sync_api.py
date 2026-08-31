import unittest
from unittest.mock import patch

try:  # httpx is only present transitively; skip rather than break the suite
    from fastapi.testclient import TestClient
except Exception:  # noqa: BLE001
    TestClient = None

import fast_sync
import main


SUMMARY = {
    "mode": "fast",
    "target": {"kind": "customer", "query": "Ah Seng", "matched": "Ah Seng",
               "customer_id": "C1"},
    "days": 1,
    "catalog_source": "catalog",
    "plants": [{"plant_uid": "P1", "plant_name": "Ah Seng", "customer_id": "C1",
                "linked_now": False, "device_source": "catalog",
                "devices": ["D1", "D2"], "rows_written": 576}],
    "plant_count": 1,
    "device_count": 2,
    "rows_written": 576,
    "ok": 2,
    "err": 0,
    "errors": [],
    "elapsed_s": 8.0,
    "debug": {"saj_calls": 6},
    "log": ["[  0.00s] info  start", "[  8.00s] info  DONE"],
}


@unittest.skipUnless(TestClient is not None, "fastapi.testclient unavailable")
class FastSyncEndpointTests(unittest.TestCase):
    def setUp(self):
        # No context manager: startup events would try to resume a backfill.
        self.client = TestClient(main.app)
        get_client = patch.object(main, "_get_client", return_value=object())
        get_client.start()
        self.addCleanup(get_client.stop)
        with main._fast_lock:
            main._fast_runs.clear()

    def test_a_run_is_returned_and_remembered(self):
        with patch.object(main.fast_sync, "run", return_value=SUMMARY):
            r = self.client.post("/sync/fast?customer=Ah%20Seng")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["rows_written"], 576)

        recent = self.client.get("/sync/fast/log").json()
        self.assertEqual(recent["kept"], 1)
        run = recent["runs"][0]
        self.assertEqual(run["target"]["matched"], "Ah Seng")
        self.assertIn("at", run)
        # The buffer keeps the log but slims the per-plant device serials.
        self.assertEqual(run["plants"], [{"plant_uid": "P1", "plant_name": "Ah Seng",
                                          "customer_id": "C1", "devices": 2,
                                          "rows_written": 576}])
        self.assertEqual(run["log"], SUMMARY["log"])

    def test_the_buffer_is_capped_and_newest_first(self):
        with patch.object(main, "FAST_RUN_HISTORY", 2):
            for i in range(3):
                main._remember_fast_run({**SUMMARY, "rows_written": i})
        runs = self.client.get("/sync/fast/log").json()["runs"]
        self.assertEqual([r["rows_written"] for r in runs], [2, 1])

    def test_an_ambiguous_name_returns_409_with_candidates_and_log(self):
        err = fast_sync.TargetAmbiguous("Ah", ["Ah Seng", "Ah Seng Trading"])
        with patch.object(main.fast_sync, "run", side_effect=err):
            r = self.client.post("/sync/fast?customer=Ah")
        self.assertEqual(r.status_code, 409)
        detail = r.json()["detail"]
        self.assertEqual(detail["error"], "ambiguous")
        self.assertEqual(detail["candidates"], ["Ah Seng", "Ah Seng Trading"])
        self.assertTrue(any("ambiguous" in line for line in detail["log"]))

    def test_an_unknown_name_returns_404_with_the_log(self):
        with patch.object(main.fast_sync, "run",
                          side_effect=fast_sync.TargetNotFound("no match for 'X'")):
            r = self.client.post("/sync/fast?plant=X")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["detail"]["error"], "not_found")
        self.assertTrue(r.json()["detail"]["log"])

    def test_an_unexpected_failure_still_returns_its_log(self):
        with patch.object(main.fast_sync, "run", side_effect=RuntimeError("db gone")):
            r = self.client.post("/sync/fast?plant=X")
        self.assertEqual(r.status_code, 500)
        self.assertEqual(r.json()["detail"]["error"], "RuntimeError")
        self.assertTrue(r.json()["detail"]["log"])

    def test_exactly_one_target_is_required(self):
        for query in ("", "?customer=A&plant=B"):
            r = self.client.post(f"/sync/fast{query}")
            self.assertEqual(r.status_code, 400, query)

    def test_a_failed_run_is_not_remembered(self):
        with patch.object(main.fast_sync, "run",
                          side_effect=fast_sync.TargetNotFound("nope")):
            self.client.post("/sync/fast?plant=X")
        self.assertEqual(self.client.get("/sync/fast/log").json()["kept"], 0)


if __name__ == "__main__":
    unittest.main()
