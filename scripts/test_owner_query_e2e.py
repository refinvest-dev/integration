"""Safety gates for the optional owner-query service harness; no services are started."""
import tempfile
from pathlib import Path
import unittest

from owner_query_e2e import AcceptanceFailure, environment


class OwnerQueryFixtureSafetyTest(unittest.TestCase):
    def file(self, database="refinvest_integration", host="127.0.0.1", port="5433"):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "fixture.env"
        path.write_text(f"POSTGRES_DB={database}\nINTEGRATION_POSTGRES_HOST={host}\nINTEGRATION_POSTGRES_PORT={port}\nPOSTGRES_USER=fixture\nPOSTGRES_PASSWORD=fixture-only\n", encoding="utf-8")
        return path

    def test_non_integration_database_and_remote_host_are_rejected_before_process_or_sql(self):
        for path in [self.file(database="refinvest"), self.file(host="example.invalid"), self.file(port="5433,5434")]:
            with self.assertRaises(AcceptanceFailure):
                environment(path)

    def test_loopback_fixture_target_is_fixed_without_mutating_environment_file(self):
        path = self.file(host="localhost")
        original = path.read_bytes()
        values = environment(path)
        self.assertEqual("127.0.0.1", values["INTEGRATION_POSTGRES_HOST"])
        self.assertEqual("refinvest_integration", values["POSTGRES_DB"])
        self.assertEqual(original, path.read_bytes())


if __name__ == "__main__":
    unittest.main()
