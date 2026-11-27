"""Fleet guards: stdlib-only, no service or listener is started."""
import unittest

from owner_query_fleet_relay import ServingTargets, TokenSlot, loopback_origin
from owner_query_fleet_scenarios import renewed_probe_claims


class FleetSafetyTest(unittest.TestCase):
    def test_retired_key_probe_renews_the_entire_time_profile_without_changing_identity(self):
        source = {"iat": 100, "nbf": 100, "exp": 160, "jti": "previous", "sub": "synthetic", "scope": "fixed"}
        probe = renewed_probe_claims(source, 500, "fresh")
        self.assertEqual(500, probe["iat"])
        self.assertEqual(probe["iat"], probe["nbf"])
        self.assertEqual(60, probe["exp"] - probe["iat"])
        self.assertEqual("fresh", probe["jti"])
        self.assertEqual((source["sub"], source["scope"]), (probe["sub"], probe["scope"]))
        self.assertEqual((100, 100, 160), (source["iat"], source["nbf"], source["exp"]))

    def test_forwarding_targets_cannot_escape_loopback_or_add_paths(self):
        self.assertEqual("https://127.0.0.1:1234", loopback_origin("https://127.0.0.1:1234"))
        for origin in ("http://127.0.0.1:1", "https://localhost:1", "https://example.invalid:443",
                       "https://127.0.0.1:1/", "https://a@127.0.0.1:1", "https://127.0.0.1:1?x=1"):
            with self.assertRaises(ValueError):
                loopback_origin(origin)

    def test_unconfirmed_node_is_never_selected_as_serving_even_when_registered(self):
        targets = ServingTargets()
        targets.register("go-a", "https://127.0.0.1:1")
        targets.register("go-b", "https://127.0.0.1:2")
        targets.confirm("go-a")
        targets.select("go-a")
        with self.assertRaises(ValueError):
            targets.select("go-b")
        targets.select("go-b", diagnostic=True)
        self.assertEqual(("go-b", "https://127.0.0.1:2", True), targets.target())
        targets.exclude("go-b")
        self.assertEqual("go-a", targets.target()[0])
        targets.exclude("go-a")
        with self.assertRaises(ValueError):
            targets.target()

    def test_confirmation_applies_to_known_node_and_duplicate_identity_is_rejected(self):
        targets = ServingTargets()
        with self.assertRaises(ValueError):
            targets.confirm("go-a")
        targets.register("core-a", "https://127.0.0.1:1")
        with self.assertRaises(ValueError):
            targets.register("core-a", "https://127.0.0.1:2")
        with self.assertRaises(ValueError):
            targets.register("arbitrary", "https://127.0.0.1:3")

    def test_delegation_slot_is_bounded_redacted_and_clearable(self):
        slot = TokenSlot()
        slot.put("synthetic-private-delegation")
        self.assertNotIn(slot.peek(), repr(slot))
        slot.put("x" * 4097)
        self.assertIsNone(slot.peek())
        slot.put("synthetic-private-delegation")
        slot.clear()
        self.assertIsNone(slot.peek())


if __name__ == "__main__":
    unittest.main()
