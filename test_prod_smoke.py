import datetime as dt
import unittest

from prod_smoke import MYT, redact, validate_data_response, validate_health


class ProductionSmokeTests(unittest.TestCase):
    def test_health_requires_exact_production_revision_and_config(self):
        checks = validate_health({
            "ok": True,
            "service": "ee-saj-api",
            "revision": "abc123",
            "db_backend": "database_url",
            "protected": True,
        }, "abc123")

        self.assertTrue(all(item["ok"] for item in checks), checks)

    def test_health_rejects_old_revision(self):
        checks = validate_health({
            "ok": True,
            "service": "ee-saj-api",
            "revision": "old",
            "db_backend": "database_url",
            "protected": True,
        }, "new")
        state = {item["name"]: item["ok"] for item in checks}

        self.assertFalse(state["deployed_revision"])

    def test_fetch_response_proves_completed_day_was_persisted(self):
        yesterday = (dt.datetime.now(MYT).date() - dt.timedelta(days=1)).isoformat()
        payload = {
            "device_sn": "SN1", "days": 2, "source": "live",
            "rows_written": 200,
            "latest": {"ts": f"{yesterday}T15:00:00+08:00"},
            "series": [
                {"ts": f"{yesterday}T07:00:00+08:00", "ac_power_w": 10},
                {"ts": f"{yesterday}T07:05:00+08:00", "ac_power_w": 20},
            ],
            "daily": [{"day": yesterday, "kwh": 12.3}],
        }
        checks = validate_data_response(payload, "SN1", 2, yesterday)

        self.assertTrue(all(item["ok"] for item in checks), checks)

    def test_report_text_redacts_trigger_token(self):
        output = redact("request failed token-value", ["token-value"])

        self.assertNotIn("token-value", output)
        self.assertIn("[REDACTED]", output)


if __name__ == "__main__":
    unittest.main()
