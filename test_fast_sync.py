import unittest
from unittest.mock import patch

import fast_sync


class FakeClient:
    """Stands in for SajClient; only the request counter is exercised here."""

    def __init__(self, calls=0):
        self.calls = calls
        self.call_counts = {}


CUSTOMERS = [
    {"customer_id": "C1", "name": "Ah Seng"},
    {"customer_id": "C2", "name": "Ah Seng Trading"},
    {"customer_id": "C3", "name": "Lim Solar"},
]
PLANTS = [
    {"plant_uid": "P1", "plant_name": "Ah Seng", "customer_id": "C1"},
    {"plant_uid": "P2", "plant_name": "Taman Molek", "customer_id": None},
    {"plant_uid": "P3", "plant_name": "taman  molek!", "customer_id": None},
]


class SelectByNameTests(unittest.TestCase):
    def test_exact_normalized_match_ignores_case_and_punctuation(self):
        rows = fast_sync.select_by_name(PLANTS, "plant_name", "TAMAN-MOLEK")
        self.assertEqual([r["plant_uid"] for r in rows], ["P2", "P3"])

    def test_exact_match_wins_over_a_longer_substring_sibling(self):
        # "Ah Seng" must not be dragged into "Ah Seng Trading".
        rows = fast_sync.select_by_name(CUSTOMERS, "name", "Ah Seng")
        self.assertEqual([r["customer_id"] for r in rows], ["C1"])

    def test_substring_fallback_finds_the_single_longer_name(self):
        rows = fast_sync.select_by_name(CUSTOMERS, "name", "Trading")
        self.assertEqual([r["customer_id"] for r in rows], ["C2"])

    def test_substring_spanning_distinct_names_is_ambiguous(self):
        with self.assertRaises(fast_sync.TargetAmbiguous) as ctx:
            fast_sync.select_by_name(CUSTOMERS, "name", "Ah")
        self.assertEqual(ctx.exception.candidates, ["Ah Seng", "Ah Seng Trading"])

    def test_unknown_name_is_not_found(self):
        with self.assertRaises(fast_sync.TargetNotFound):
            fast_sync.select_by_name(CUSTOMERS, "name", "Nobody")

    def test_blank_name_is_not_found(self):
        with self.assertRaises(fast_sync.TargetNotFound):
            fast_sync.select_by_name(CUSTOMERS, "name", "   ")


class CustomerForNameTests(unittest.TestCase):
    def test_single_exact_customer(self):
        self.assertEqual(fast_sync.customer_for_name("ah-seng", CUSTOMERS), "C1")

    def test_duplicate_names_refuse_to_guess(self):
        dupes = CUSTOMERS + [{"customer_id": "C9", "name": "Ah Seng"}]
        self.assertIsNone(fast_sync.customer_for_name("Ah Seng", dupes))

    def test_no_match_is_none(self):
        self.assertIsNone(fast_sync.customer_for_name("Nobody", CUSTOMERS))


class FindPlantsTests(unittest.TestCase):
    def test_catalog_hit_never_touches_the_portal(self):
        client = object()  # any portal call would blow up on this
        plants, source = fast_sync.find_plants("Taman Molek", PLANTS, client)
        self.assertEqual(source, "catalog")
        self.assertEqual([p["plant_uid"] for p in plants], ["P2", "P3"])

    def test_unknown_name_falls_back_to_the_portal_and_is_catalogued(self):
        portal = [{"plant_uid": "P9", "plant_name": "Brand New",
                   "customer_id": None, "_portal": {"plantName": "Brand New"}}]
        with patch.object(fast_sync, "_portal_plants", return_value=portal), \
                patch.object(fast_sync, "_store_portal_plants") as store:
            plants, source = fast_sync.find_plants("Brand New", PLANTS, None)
        self.assertEqual(source, "portal")
        self.assertEqual([p["plant_uid"] for p in plants], ["P9"])
        store.assert_called_once()

    def test_ambiguous_catalog_hit_is_not_retried_against_the_portal(self):
        rows = [{"plant_uid": "A", "plant_name": "Sun One", "customer_id": None},
                {"plant_uid": "B", "plant_name": "Sun Two", "customer_id": None}]
        with patch.object(fast_sync, "_portal_plants") as portal:
            with self.assertRaises(fast_sync.TargetAmbiguous):
                fast_sync.find_plants("Sun", rows, None)
        portal.assert_not_called()

    def test_refresh_carries_over_the_existing_customer_link(self):
        portal = [{"plant_uid": "P1", "plant_name": "Ah Seng",
                   "customer_id": None, "_portal": {"plantName": "Ah Seng"}}]
        with patch.object(fast_sync, "_portal_plants", return_value=portal), \
                patch.object(fast_sync, "_store_portal_plants"):
            plants, source = fast_sync.find_plants("Ah Seng", PLANTS, None, refresh=True)
        self.assertEqual(source, "portal")
        self.assertEqual(plants[0]["customer_id"], "C1")


class RunTests(unittest.TestCase):
    """`run` end to end with the DB, the portal and the fetcher stubbed out."""

    def setUp(self):
        self.fetched = []
        self.client = FakeClient()
        patches = [
            patch.object(fast_sync, "load_customers", return_value=CUSTOMERS),
            patch.object(fast_sync, "load_plants", return_value=PLANTS),
            patch.object(fast_sync, "insert_device_maps", return_value=1),
            patch.object(fast_sync, "link_plants", return_value=1),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _fetch(self, client, sn, days=1):
        self.fetched.append((sn, days))
        return {"rows_written": 288, "source": "live", "latest": None}

    def _run(self, sns_by_plant, **kwargs):
        with patch.object(fast_sync, "_device_sns",
                          side_effect=lambda c, uid: (sns_by_plant.get(uid, []), "catalog")), \
                patch.object(fast_sync.fetcher, "fetch_device", side_effect=self._fetch):
            kwargs.setdefault("log", fast_sync.RunLog(echo=False))
            return fast_sync.run(self.client, interval=0, jitter=0, **kwargs)

    def test_customer_syncs_only_their_own_plants(self):
        out = self._run({"P1": ["D1", "D2"], "P2": ["D3"]}, customer="Ah Seng")
        self.assertEqual(out["target"]["customer_id"], "C1")
        self.assertEqual(out["plant_count"], 1)
        self.assertEqual(out["device_count"], 2)
        self.assertEqual(out["rows_written"], 576)
        self.assertEqual([sn for sn, _ in self.fetched], ["D1", "D2"])

    def test_plant_name_syncs_every_plant_sharing_that_name(self):
        out = self._run({"P2": ["D3"], "P3": ["D4"]}, plant="Taman Molek", days=7)
        self.assertEqual(out["device_count"], 2)
        self.assertEqual(self.fetched, [("D3", 7), ("D4", 7)])

    def test_unlinked_plant_is_linked_to_the_customer_of_the_same_name(self):
        plants = [{"plant_uid": "P5", "plant_name": "Lim Solar", "customer_id": None}]
        with patch.object(fast_sync, "load_plants", return_value=plants):
            out = self._run({"P5": ["D5"]}, plant="Lim Solar")
        self.assertTrue(out["plants"][0]["linked_now"])
        self.assertEqual(out["plants"][0]["customer_id"], "C3")

    def test_no_link_leaves_the_customer_edge_alone(self):
        plants = [{"plant_uid": "P5", "plant_name": "Lim Solar", "customer_id": None}]
        with patch.object(fast_sync, "load_plants", return_value=plants):
            out = self._run({"P5": ["D5"]}, plant="Lim Solar", link=False)
        self.assertFalse(out["plants"][0]["linked_now"])
        self.assertIsNone(out["plants"][0]["customer_id"])
        fast_sync.link_plants.assert_not_called()

    def test_customer_with_no_link_falls_back_to_the_plant_of_their_name(self):
        plants = [{"plant_uid": "P1", "plant_name": "Ah Seng", "customer_id": None}]
        with patch.object(fast_sync, "load_plants", return_value=plants):
            out = self._run({"P1": ["D1"]}, customer="Ah Seng")
        self.assertEqual(out["plant_count"], 1)
        self.assertEqual(out["plants"][0]["customer_id"], "C1")
        self.assertTrue(out["plants"][0]["linked_now"])

    def test_a_loose_plant_match_syncs_but_refuses_to_write_a_link(self):
        # "Ah Seng" reaches the plant "Ah Seng Solar", but only an exact name
        # match is allowed to create a customer edge.
        plants = [{"plant_uid": "P7", "plant_name": "Ah Seng Solar",
                   "customer_id": None}]
        with patch.object(fast_sync, "load_plants", return_value=plants):
            out = self._run({"P7": ["D7"]}, customer="Ah Seng")
        self.assertEqual(out["device_count"], 1)          # readings still synced
        self.assertFalse(out["plants"][0]["linked_now"])  # but no link written
        self.assertIsNone(out["plants"][0]["customer_id"])
        fast_sync.link_plants.assert_not_called()

    def test_refresh_keeps_a_linked_plant_that_was_renamed(self):
        # The plant was linked as "Ah Seng" and has since been renamed in the
        # portal. Refreshing must follow the uid, not re-search by the old name.
        portal = [{"plant_uid": "P1", "plant_name": "Ah Seng (new roof)",
                   "customer_id": None, "_portal": {"plantName": "Ah Seng (new roof)"}}]
        with patch.object(fast_sync, "_portal_plants", return_value=portal),                 patch.object(fast_sync, "_store_portal_plants"):
            out = self._run({"P1": ["D1"]}, customer="Ah Seng", refresh_catalog=True)
        self.assertEqual(out["catalog_source"], "portal")
        self.assertEqual(out["plants"][0]["plant_name"], "Ah Seng (new roof)")
        self.assertEqual(out["plants"][0]["customer_id"], "C1")

    def test_customer_with_no_plant_at_all_is_reported_not_found(self):
        with patch.object(fast_sync, "load_plants", return_value=[]), \
                patch.object(fast_sync, "_portal_plants", return_value=[]):
            with self.assertRaises(fast_sync.TargetNotFound) as ctx:
                fast_sync.run(self.client, customer="Lim Solar", interval=0,
                              jitter=0, log=fast_sync.RunLog(echo=False))
        self.assertIn("no linked plant", str(ctx.exception))

    def test_one_failing_inverter_does_not_abort_the_others(self):
        def flaky(client, sn, days=1):
            if sn == "D1":
                raise RuntimeError("boom")
            return {"rows_written": 10, "source": "live", "latest": None}

        with patch.object(fast_sync, "_device_sns",
                          side_effect=lambda c, uid: (["D1", "D2"], "catalog")), \
                patch.object(fast_sync.fetcher, "fetch_device", side_effect=flaky):
            out = fast_sync.run(self.client, customer="Ah Seng", interval=0,
                                jitter=0, log=fast_sync.RunLog(echo=False))
        self.assertEqual((out["ok"], out["err"]), (1, 1))
        self.assertEqual(out["rows_written"], 10)
        self.assertEqual(out["errors"][0]["device_sn"], "D1")
        self.assertEqual(out["errors"][0]["error"], "RuntimeError: boom")

    def test_summary_carries_the_log_timings_and_saj_cost(self):
        self.client.calls = 17  # a long-lived client already has a history
        out = self._run({"P1": ["D1", "D2"]}, customer="Ah Seng")
        self.assertEqual(out["debug"]["saj_calls"], 0)  # delta, not the total
        self.assertEqual(out["debug"]["saj_calls_before_readings"], 0)
        self.assertIn("readings", out["debug"]["timings_s"])
        self.assertIn("resolve", out["debug"]["timings_s"])
        self.assertTrue(any("DONE" in line for line in out["log"]))
        self.assertTrue(all(line.startswith("[") for line in out["log"]))

    def test_saj_calls_reports_the_delta_for_this_run_only(self):
        def counting_fetch(client, sn, days=1):
            client.calls += 3  # a device costs several portal requests
            return {"rows_written": 1, "source": "live", "latest": None}

        with patch.object(fast_sync, "_device_sns",
                          side_effect=lambda c, uid: (["D1", "D2"], "catalog")), \
                patch.object(fast_sync.fetcher, "fetch_device",
                             side_effect=counting_fetch):
            out = fast_sync.run(self.client, customer="Ah Seng", interval=0,
                                jitter=0, log=fast_sync.RunLog(echo=False))
        self.assertEqual(out["debug"]["saj_calls"], 6)
        self.assertEqual(out["debug"]["saj_calls_before_readings"], 0)

    def test_quiet_log_keeps_info_and_drops_debug(self):
        out = self._run({"P1": ["D1"]}, customer="Ah Seng",
                        log=fast_sync.RunLog(debug=False, echo=False))
        self.assertTrue(any("info" in line for line in out["log"]))
        self.assertFalse(any("debug" in line for line in out["log"]))

    def test_a_callers_log_survives_a_failed_resolution(self):
        log = fast_sync.RunLog(echo=False)
        with self.assertRaises(fast_sync.TargetAmbiguous):
            fast_sync.run(self.client, customer="Ah", log=log)
        # The narration up to the failure is what makes a prod 409 diagnosable.
        self.assertTrue(any("start customer=" in line for line in log.lines))
        self.assertTrue(any("mirror:" in line for line in log.lines))

    def test_a_customer_id_skips_name_matching_entirely(self):
        # Three customers named "Chen" is real in prod; the id is the only way in.
        dupes = CUSTOMERS + [{"customer_id": "C9", "name": "Ah Seng"}]
        with patch.object(fast_sync, "load_customers", return_value=dupes):
            out = self._run({"P1": ["D1"]}, customer_id="C1")
        self.assertEqual(out["target"]["customer_id"], "C1")
        self.assertEqual(out["target"]["by"], "id")
        self.assertEqual(out["device_count"], 1)

    def test_an_unknown_customer_id_is_not_found(self):
        with self.assertRaises(fast_sync.TargetNotFound):
            fast_sync.run(self.client, customer_id="nope",
                          log=fast_sync.RunLog(echo=False))

    def test_ambiguous_customers_carry_ids_to_choose_from(self):
        dupes = [{"customer_id": "C1", "name": "Chen"},
                 {"customer_id": "C2", "name": "Chen"}]
        with patch.object(fast_sync, "load_customers", return_value=dupes):
            with self.assertRaises(fast_sync.TargetAmbiguous) as ctx:
                fast_sync.run(self.client, customer="Chen",
                              log=fast_sync.RunLog(echo=False))
        self.assertEqual([c["customer_id"] for c in ctx.exception.choices],
                         ["C1", "C2"])

    def test_both_targets_is_a_usage_error(self):
        for kwargs in ({}, {"customer": "A", "plant": "B"},
                       {"customer": "A", "customer_id": "C1"}):
            with self.assertRaises(ValueError):
                fast_sync.run(self.client, **kwargs)


if __name__ == "__main__":
    unittest.main()
