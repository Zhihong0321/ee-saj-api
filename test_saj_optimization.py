import datetime as dt
import unittest
from unittest.mock import patch

import fetcher
from saj_api import SajClient


class SajClientOptimizationTests(unittest.TestCase):
    def test_raw_day_pages_by_total_not_broken_has_next_page(self):
        client = object.__new__(SajClient)
        calls = []
        pages = {
            1: {"list": [{"datetime": "2026-01-01 00:10:00"},
                         {"datetime": "2026-01-01 00:05:00"}],
                "total": 3, "hasNextPage": False},
            2: {"list": [{"datetime": "2026-01-01 00:00:00"}],
                "total": 3, "hasNextPage": False},
        }

        def raw_page(device_sn, day, page_no, page_size, device_type):
            calls.append((device_sn, day, page_no, page_size, device_type))
            return pages[page_no]

        client.raw_data_page = raw_page
        rows = client.raw_data_day("SN1", "2026-01-01", page_size=2)

        self.assertEqual([row["datetime"] for row in rows], [
            "2026-01-01 00:00:00",
            "2026-01-01 00:05:00",
            "2026-01-01 00:10:00",
        ])
        self.assertEqual([call[2] for call in calls], [1, 2])

    def test_generation_chart_day_normalizes_required_series(self):
        client = object.__new__(SajClient)
        captured = {}

        def call(path, payload):
            captured.update(path=path, payload=payload)
            return {
                "xAxis": {"coordinateList": ["07:00", "07:05"]},
                "yAxis": [
                    {"legendKey": "AC_OUTPUT_POWER", "dataList": ["10", "20"]},
                    {"legendKey": "ENERGY_CURVE", "dataList": ["0.1", "0.2"]},
                    {"legendKey": "Pow1", "dataList": ["6", "12"]},
                    {"legendKey": "Pow2", "dataList": ["4", "8"]},
                ],
            }

        client.call = call
        rows = client.generation_chart_day("SN1", "2026-01-01")

        self.assertEqual(captured["path"],
                         "/api/v2/monitor/plant/chart/getCommonChartData")
        self.assertEqual(captured["payload"]["commonChartType"], 1)
        self.assertEqual(rows[0], {
            "datetime": "2026-01-01 07:00",
            "pac": "10",
            "PVP": 10.0,
            "todayPVEnergy": "0.1",
        })
        self.assertEqual(rows[-1]["PVP"], 20.0)

    def test_inverter_enumeration_uses_bulk_pages(self):
        client = object.__new__(SajClient)
        requested_pages = []

        def call(path, payload):
            self.assertEqual(path,
                             "/api/v2/monitor/inverter/userInverterPage")
            requested_pages.append(payload["pageNo"])
            if payload["pageNo"] == 1:
                return {"list": [
                    {"deviceSn": "A", "plantUid": "P1", "plantName": "One"},
                    {"deviceSn": "B", "plantUid": "P2", "plantName": "Two"},
                ], "total": 3, "hasNextPage": False}
            return {"list": [
                {"deviceSn": "C", "plantUid": "P3", "plantName": "Three"},
            ], "total": 3, "hasNextPage": False}

        client.call = call
        devices = list(client.iter_all_devices(page_size=2))

        self.assertEqual(requested_pages, [1, 2])
        self.assertEqual(devices, [
            ("P1", "One", "A"),
            ("P2", "Two", "B"),
            ("P3", "Three", "C"),
        ])

    def test_completed_day_falls_back_to_raw(self):
        class Client:
            def generation_chart_day(self, device_sn, day):
                raise RuntimeError("chart unavailable")

            def raw_data_day(self, device_sn, day):
                return [{"datetime": f"{day} 07:00:00", "pac": 1}]

        with patch.object(fetcher, "HISTORY_SOURCE", "chart"):
            rows = fetcher._completed_day_rows(Client(), "SN1", "2026-01-01")
        self.assertEqual(rows[0]["pac"], 1)

    def test_fetch_device_keeps_today_raw_and_uses_chart_for_history(self):
        class Client:
            def __init__(self):
                self.raw_days = []
                self.chart_days = []

            def raw_data_day(self, device_sn, day):
                self.raw_days.append(day)
                return [{"datetime": f"{day} 07:00:00", "pac": 1}]

            def generation_chart_day(self, device_sn, day):
                self.chart_days.append(day)
                return [{"datetime": f"{day} 07:00", "pac": 1,
                         "todayPVEnergy": 1}]

        client = Client()
        with (patch.object(fetcher, "HISTORY_SOURCE", "chart"),
              patch.object(fetcher, "_upsert_readings", return_value=1),
              patch.object(fetcher, "ensure_device_info"),
              patch.object(fetcher, "latest", return_value=None)):
            result = fetcher.fetch_device(client, "SN1", days=3)

        today = dt.date.today().isoformat()
        self.assertEqual(client.raw_days, [today])
        self.assertEqual(len(client.chart_days), 2)
        self.assertEqual(result["rows_written"], 3)

    def test_chart_upsert_preserves_diagnostic_columns(self):
        row = {"datetime": "2026-01-01 07:00", "pac": 10,
               "PVP": 11, "todayPVEnergy": 0.1}
        with patch.object(fetcher.pg, "run", return_value={"rowcount": 1}) as run:
            fetcher._upsert_readings("SN1", [row])
        sql = run.call_args.args[0]
        self.assertIn(
            "total_kwh=coalesce(excluded.total_kwh,saj_reading.total_kwh)", sql)
        self.assertIn(
            "device_temp=coalesce(excluded.device_temp,saj_reading.device_temp)", sql)


if __name__ == "__main__":
    unittest.main()
