"""Main-mode guards, without a JVM, DB, network, or third-party Python dependencies."""
from pathlib import Path
import tempfile
import unittest
import zipfile

from owner_query_authority_observer import checked_target
from owner_query_e2e import AcceptanceFailure
from owner_query_main_e2e import inspect_jar


class MainAcceptanceSafetyTest(unittest.TestCase):
    def test_observer_only_forwards_to_a_fixed_loopback_authority_path(self):
        self.assertEqual("https://127.0.0.1:1234/internal/trading-review/v1/delegations/check",
                         checked_target("https://127.0.0.1:1234"))
        for value in ("http://127.0.0.1:1", "https://example.invalid:443", "https://127.0.0.1:1/other",
                      "https://user@127.0.0.1:1", "https://127.0.0.1:1?x=1", "https://127.0.0.1:1#x"):
            with self.assertRaises(ValueError):
                checked_target(value)

    def test_main_mode_rejects_fixture_jar_before_starting_java(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.jar"
            manifest = ("Main-Class: org.springframework.boot.loader.launch.JarLauncher\r\n"
                        "Start-Class: com.refinvest.RefinvestApplicationKt\r\n")
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("META-INF/MANIFEST.MF", manifest)
            inspect_jar(path)
            with zipfile.ZipFile(path, "a") as archive:
                archive.writestr("BOOT-INF/classes/com/refinvestfixture/Initializer.class", "synthetic")
            with self.assertRaises(AcceptanceFailure):
                inspect_jar(path)


if __name__ == "__main__":
    unittest.main()
