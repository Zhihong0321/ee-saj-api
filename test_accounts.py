"""Unit tests for the DB-backed SAJ account store — control flow only.

`accounts._db` is stubbed with a tiny fake so the branching (new vs existing,
blank-password-keeps, single-primary demotion, seed-when-empty) is verified
without a live Postgres.
"""
import unittest
from unittest import mock

import accounts


class FakeDB:
    """Records every SQL string and returns canned rows/rowcounts by keyword."""

    def __init__(self, exists=False, primary=None, empty=True):
        self.calls = []
        self._exists = exists
        self._primary = primary
        self._empty = empty

    def __call__(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))
        low = sql.lower()
        if "select 1 from saj_account where username" in low:
            return {"rows": [{"?": 1}] if self._exists else []}
        if "select 1 from saj_account limit 1" in low:
            return {"rows": [] if self._empty else [{"?": 1}]}
        if "where is_primary and active" in low:
            return {"rows": [self._primary] if self._primary else []}
        if low.startswith("update"):
            return {"rows": [], "rowcount": 1}
        return {"rows": []}

    def sqls(self):
        return [c[0] for c in self.calls]

    def find(self, needle):
        return [c for c in self.calls if needle.lower() in c[0].lower()]


class UpsertTests(unittest.TestCase):
    def test_new_without_password_rejected(self):
        with mock.patch.object(accounts, "_db", FakeDB(exists=False)):
            with self.assertRaises(ValueError):
                accounts.upsert("operation09", password="")

    def test_new_with_password_inserts(self):
        db = FakeDB(exists=False)
        with mock.patch.object(accounts, "_db", db):
            accounts.upsert("operation09", password="secret", org_code="OAhz")
        self.assertTrue(db.find("insert into saj_account"),
                        "a new account should INSERT")

    def test_existing_blank_password_keeps_secret(self):
        db = FakeDB(exists=True)
        with mock.patch.object(accounts, "_db", db):
            accounts.upsert("operation01", password="", remarks="edit only")
        updates = db.find("update saj_account set")
        self.assertTrue(updates, "an edit should UPDATE")
        # No UPDATE may carry a password when the field was left blank.
        self.assertFalse(any("password=" in s for s, _ in updates),
                         "blank password must not overwrite the stored one")

    def test_existing_with_password_updates_secret(self):
        db = FakeDB(exists=True)
        with mock.patch.object(accounts, "_db", db):
            accounts.upsert("operation01", password="rotated")
        self.assertTrue(any("password=$2" in s for s, _ in db.find("update")),
                        "a supplied password must be written")

    def test_setting_primary_demotes_others_first(self):
        db = FakeDB(exists=True)
        with mock.patch.object(accounts, "_db", db):
            accounts.upsert("operation02", password="", is_primary=True)
        demote = db.find("is_primary=false")
        self.assertTrue(demote, "promoting one primary must demote the rest")
        self.assertIn("username<>", demote[0][0])


class PrimaryTests(unittest.TestCase):
    def test_set_primary_missing_raises(self):
        class NoRowUpdate(FakeDB):
            def __call__(self, sql, params=None):
                self.calls.append((" ".join(sql.split()), params))
                if sql.lower().startswith("update") and "is_primary=true" in sql.lower():
                    return {"rows": [], "rowcount": 0}
                return {"rows": [], "rowcount": 1}
        with mock.patch.object(accounts, "_db", NoRowUpdate()):
            with self.assertRaises(ValueError):
                accounts.set_primary("ghost")


class SeedTests(unittest.TestCase):
    def test_seed_noop_when_not_empty(self):
        db = FakeDB(empty=False)
        with mock.patch.object(accounts, "_db", db):
            self.assertEqual(accounts.seed_from_env_if_empty(), 0)

    def test_seed_copies_primary_from_env(self):
        db = FakeDB(exists=False, empty=True)
        env = {"SAJ_USER": "operation01", "SAJ_PASS": "pw", "BACKFILL_USERS": ""}
        with mock.patch.object(accounts, "_db", db), \
                mock.patch.dict(accounts.os.environ, env, clear=False):
            n = accounts.seed_from_env_if_empty()
        self.assertGreaterEqual(n, 1)
        self.assertTrue(db.find("insert into saj_account"))


if __name__ == "__main__":
    unittest.main()
