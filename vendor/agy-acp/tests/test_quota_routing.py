"""Routing rules for the agy-acp bridge pool.

The bug these pin: Google's "Individual quota reached … Resets in 3h0m17s" text
arrived as an ordinary assistant answer, so nothing marked the drained account
and every later request picked it again before falling back. The account that
had quota was never used first.
"""
import importlib
import json
import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

STATE = tempfile.mkdtemp(prefix="agy-state-")
APPDATA = os.path.join(STATE, "appdata")
WORK = os.path.join(STATE, "work")
PROFILES = os.path.join(STATE, "profiles")
for d in (APPDATA, WORK, PROFILES):
    os.makedirs(d, exist_ok=True)

# list_profiles() only reports profiles that carry a credential file.
for name in ("kureha", "karere", "katsukare", "1323"):
    p = os.path.join(APPDATA, "agy-" + name)
    os.makedirs(p, exist_ok=True)
    open(os.path.join(p, "antigravity-oauth-token"), "w").close()

os.environ.update({
    "AGY_STATE_FILE": os.path.join(STATE, "profile-state.json"),
    "AGY_APPDATA_ROOT": APPDATA,
    "AGY_PROFILES_DIR": PROFILES,
    "AGY_CWD": WORK,
    "AGY_PREWARM": "0",
    "AGY_BIN": "/bin/true",
    "AGY_ADMIN_TOKEN": "test-token",
})

bridge = importlib.import_module("bridge")

QUOTA_TEXT = ("\u26a0 Individual quota reached. Please upgrade your subscription "
              "to increase your limits. Resets in 3h0m17s.")


class QuotaParsing(unittest.TestCase):
    def test_reset_window_is_read_from_the_banner(self):
        self.assertEqual(bridge.quota_reset_seconds(QUOTA_TEXT), 3 * 3600 + 17)

    def test_banner_without_window_falls_back_to_default(self):
        self.assertEqual(bridge.quota_reset_seconds("quota reached"),
                         bridge.DEFAULT_QUOTA_COOLDOWN)

    def test_banner_is_recognised(self):
        self.assertTrue(bridge.is_quota_banner(QUOTA_TEXT))
        self.assertTrue(bridge.is_quota_banner("RESOURCE_EXHAUSTED"))

    def test_normal_answer_is_not_a_banner(self):
        self.assertFalse(bridge.is_quota_banner("OK"))
        self.assertFalse(bridge.is_quota_banner("Here is how limits work in the API."))


class PoolOrdering(unittest.TestCase):
    def setUp(self):
        self.pool = bridge.ProfilePool()
        self.pool.tested = {}
        self.pool.set_primary("kureha")
        for name in ("kureha", "karere", "katsukare", "1323"):
            self.pool.mark(name, "ok", "OK")

    def test_healthy_primary_keeps_the_traffic(self):
        self.assertEqual(self.pool.order(model="gemini-4-hydrogen-high")[0], "kureha")

    def test_drained_account_moves_to_the_tail(self):
        self.pool.quota("kureha", 3 * 3600, QUOTA_TEXT)
        order = self.pool.order(model="gemini-4-hydrogen-high")
        self.assertNotEqual(order[0], "kureha")
        self.assertEqual(order[-1], "kureha")          # still last resort, never dropped
        self.assertEqual(len(order), 4)

    def test_next_request_does_not_repick_the_drained_account(self):
        self.pool.quota("kureha", 3 * 3600, QUOTA_TEXT)
        first = self.pool.order(model="gemini-4-hydrogen-high")[0]
        second = self.pool.order(model="gemini-4-hydrogen-high")[0]
        self.assertEqual(first, second)
        self.assertNotEqual(first, "kureha")

    def test_quota_expiry_restores_the_primary(self):
        self.pool.quota("kureha", 3 * 3600, QUOTA_TEXT)
        self.pool.tested["kureha"]["quota_cooldown_until"] = time.time() - 1
        self.assertEqual(self.pool.order(model="gemini-4-hydrogen-high")[0], "kureha")

    def test_entitlement_home_leads_when_primary_cannot_serve_the_model(self):
        # neon/carbon only exist on karere; the primary learns a bad-model mark
        model = "gemini-4-neon-high"
        self.pool.learn_bad_model("kureha", model, "unknown model")
        self.pool.learn_model("karere", model)
        self.assertEqual(self.pool.order(model=model)[0], "karere")

    def test_pin_beats_every_rule(self):
        self.pool.mark("karere", "ok", "OK")
        self.assertEqual(self.pool.order(prefer="1323", model="gemini-4-hydrogen-high")[0],
                         "1323")


class StatePreservation(unittest.TestCase):
    def test_mark_keeps_learned_models(self):
        pool = bridge.ProfilePool()
        pool.tested = {}
        pool.learn_model("karere", "gemini-4-carbon-high")
        pool.mark("karere", "ok", "OK")
        self.assertIn("gemini-4-carbon-high", pool.tested["karere"]["models"])

    def test_quota_survives_a_reload(self):
        pool = bridge.ProfilePool()
        pool.tested = {}
        pool.quota("karere", 600, QUOTA_TEXT)
        reloaded = bridge.ProfilePool()
        self.assertGreater(reloaded.tested["karere"]["quota_cooldown_until"], time.time())


class DrainDetection(unittest.TestCase):
    def test_quota_banner_aborts_the_turn_instead_of_being_returned(self):
        def pieces():
            yield QUOTA_TEXT
            yield "unreachable"

        with self.assertRaises(RuntimeError) as ctx:
            list(bridge._drain(pieces(), "karere"))
        self.assertIn("quota banner", str(ctx.exception).lower())

    def test_plain_answer_still_streams_through(self):
        def pieces():
            yield "pong"

        self.assertEqual(list(bridge._drain(pieces(), "kureha")), ["pong"])


if __name__ == "__main__":
    unittest.main()
