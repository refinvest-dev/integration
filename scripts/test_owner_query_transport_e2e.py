"""Transport fault provenance guards, without service startup or third-party packages."""
import io
import json
import unittest

from owner_query_body_fault import MAX_BODY, BodyFault, BodyFaults, overflow_payload
from owner_query_fleet_relay import FleetRelay


class BodyFaultSafetyTest(unittest.TestCase):
    def test_transport_fault_cannot_replace_authority_responses(self):
        with self.assertRaises(ValueError):
            FleetRelay("authority", None, None, None, body_fault=BodyFaults())

    def test_fault_is_explicit_and_consumed_once_without_changing_next_request(self):
        faults = BodyFaults()
        self.assertIsNone(faults.take())
        fault = faults.arm("partial")
        with self.assertRaises(ValueError):
            faults.arm("truncate")
        self.assertIs(fault, faults.take())
        self.assertIsNone(faults.take())
        faults.close()
        self.assertTrue(fault.release.is_set())
        with self.assertRaises(ValueError):
            BodyFault("unbounded")

    def test_stream_fault_is_valid_json_over_the_body_cap_without_content_length(self):
        original = b'{"id":"synthetic"}'
        payload = overflow_payload(original)
        self.assertEqual(MAX_BODY + 1, len(payload))
        self.assertEqual(json.loads(original), json.loads(payload))
        headers = BodyFault("stream_overflow").headers(original)
        self.assertNotIn("Content-Length", headers)
        self.assertEqual("chunked", headers["Transfer-Encoding"])

    def test_truncation_keeps_original_length_but_only_sends_an_actual_prefix(self):
        original = b'{"id":"synthetic"}'
        handler = type("Handler", (), {"wfile": io.BytesIO()})()
        fault = BodyFault("truncate")
        self.assertEqual(str(len(original)), fault.headers(original)["Content-Length"])
        fault.write(handler, original)
        prefix = handler.wfile.getvalue()
        self.assertTrue(prefix and original.startswith(prefix) and len(prefix) < len(original))
        self.assertTrue(fault.finished.is_set())
        self.assertFalse(fault.peer_closed.is_set())


if __name__ == "__main__":
    unittest.main()
