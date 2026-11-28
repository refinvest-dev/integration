"""List probe provenance, size isolation, and numeric boundary guards; no services."""
import json
import unittest

from owner_query_account_list_fault import LIST_LIMIT, ListFaults, mutated_page
from owner_query_body_fault import MAX_BODY, BodyFault, overflow_payload
from owner_query_fleet_relay import FleetRelay, read_path
from owner_query_account_list_e2e import AccountListServiceAcceptance
from owner_query_e2e import TRACE_PARENT


class AccountListFixtureSafetyTest(unittest.TestCase):
    def test_direct_go_probe_participates_in_the_same_trace_as_public_probes(self):
        captured = {}

        class Session:
            def get(self, url, **options):
                captured.update(options)
                return type("Response",(),{"status_code":200})()

        run = object.__new__(AccountListServiceAcceptance)
        run.session, run.go_origin, run.service_credential = Session(), "https://127.0.0.1:1234", "synthetic"
        run.direct("/internal/trading-review/v1/accounts","synthetic-token",200)
        self.assertEqual(TRACE_PARENT,captured["headers"]["traceparent"])

    def page(self):
        return json.dumps({"items":[{"id":"9007199254740993","venue":"BINANCE"},
                                    {"id":"9007199254740992","venue":"BINANCE"}],
                           "total":2,"page":0,"size":2}).encode()

    def test_list_path_is_opt_in_and_only_accepts_canonical_reconstructed_query(self):
        path = "/internal/trading-review/v1/accounts"
        self.assertFalse(read_path(path+"?page=0&size=20"))
        for query in ("?page=0&size=1","?page=2147483647&size=100"):
            self.assertTrue(read_path(path+query,True))
        for query in ("", "?ownerId=1", "?page=00&size=1", "?page=2147483648&size=1", "?page=0&size=101",
                      "?page=0&size=1&size=1", "?page=0&size=1#alias"):
            self.assertFalse(read_path(path+query,True))
        self.assertTrue(read_path(path+"/123"))

    def test_list_option_never_changes_authority_hop(self):
        with self.assertRaises(ValueError):
            FleetRelay("authority",None,None,None,account_list=True)

    def test_list_fault_uses_its_own_cap_without_changing_metadata_default(self):
        payload = self.page()
        self.assertEqual(MAX_BODY+1,len(overflow_payload(payload)))
        self.assertEqual(LIST_LIMIT+1,len(overflow_payload(payload,LIST_LIMIT)))
        self.assertEqual(str(MAX_BODY+1),BodyFault("declared_oversize").headers(payload)["Content-Length"])
        fault = ListFaults().arm("declared_oversize")
        self.assertEqual(str(LIST_LIMIT+1),fault.headers(payload)["Content-Length"])
        with self.assertRaises(ValueError):
            ListFaults().arm("unbounded")

    def test_longmax_probe_is_exact_and_overflow_is_not_rounded(self):
        self.assertEqual(9223372036854775807,json.loads(mutated_page(self.page(),"longmax"))["total"])
        self.assertEqual(9223372036854775808,json.loads(mutated_page(self.page(),"overflow_total"))["total"])
        duplicate = mutated_page(self.page(),"duplicate_field")
        self.assertEqual(2,duplicate.count(b'"total":'))
        self.assertTrue(mutated_page(self.page(),"invalid_utf8").startswith(b"\xff"))

    def test_mutation_is_one_shot_and_the_original_payload_is_unchanged(self):
        payload = self.page()
        faults = ListFaults()
        fault = faults.mutate("missing_field")
        self.assertIs(fault,faults.take())
        self.assertIsNone(faults.take())
        self.assertIn("venue",json.loads(payload)["items"][0])
        self.assertNotIn("venue",json.loads(mutated_page(payload,"missing_field"))["items"][0])
        with self.assertRaises(ValueError):
            faults.mutate("arbitrary_payload")
        faults.close()
        self.assertTrue(fault.release.is_set())


if __name__ == "__main__":
    unittest.main()
