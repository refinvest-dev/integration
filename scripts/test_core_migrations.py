import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from core_migrations import core_migrations


class CoreMigrationsTest(unittest.TestCase):
    def write(self, root, feature, name):
        path = root / feature / 'adapter/persistence/src/main/resources/db/migration' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('-- synthetic migration', encoding='utf-8')

    def powershell(self, root):
        shell = shutil.which('powershell.exe') or shutil.which('pwsh')
        self.assertIsNotNone(shell, 'PowerShell is required to verify both discovery paths')
        driver = root / 'discovery.ps1'
        driver.write_text('''param($Helper,$CoreRoot)
$ErrorActionPreference='Stop'
. $Helper
ConvertTo-Json -InputObject @(Get-CoreIntegrationMigrations -CorePath $CoreRoot | ForEach-Object { $_.Name })
''', encoding='utf-8')
        return subprocess.run([shell, '-NoProfile', '-File', str(driver),
            str(Path(__file__).with_name('CoreMigrations.ps1').resolve()), str(root)], capture_output=True, text=True)

    def test_all_feature_migrations_have_one_numeric_order(self):
        with tempfile.TemporaryDirectory(prefix='core migrations ') as directory:
            root = Path(directory)
            for feature, version in [('tradingreview', 2), ('backtest', 11), ('auth', 19), ('subscription', 18), ('strategy', 20)]:
                self.write(root, feature, f'V{version}__synthetic.sql')
            expected = [f'V{v}__synthetic.sql' for v in [2, 11, 18, 19, 20]]
            self.assertEqual(expected, [p.name for p in core_migrations(root)])
            result = self.powershell(root)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(expected, json.loads(result.stdout))

    def test_invalid_duplicate_and_empty_sets_fail_before_sql(self):
        for case in ['duplicate', 'invalid', 'empty']:
            with self.subTest(case=case), tempfile.TemporaryDirectory(prefix='core migrations ') as directory:
                root = Path(directory)
                if case == 'duplicate':
                    self.write(root, 'auth', 'V19__first.sql')
                    self.write(root, 'strategy', 'V19__second.sql')
                elif case == 'invalid':
                    self.write(root, 'auth', 'Vfoo__invalid.sql')
                with self.assertRaises(ValueError):
                    core_migrations(root)
                self.assertNotEqual(0, self.powershell(root).returncode)


if __name__ == '__main__':
    unittest.main()
