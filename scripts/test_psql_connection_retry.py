"""Verify process retry classification; never run a database command."""
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

PS = shutil.which('powershell.exe') or shutil.which('pwsh')


@unittest.skipUnless(PS, 'PowerShell required')
class PsqlConnectionRetryTest(unittest.TestCase):
    def test_only_initial_socket_failure_is_retried_and_exit_status_is_preserved(self):
        helper = pathlib.Path(__file__).with_name('PsqlConnectionRetry.ps1').resolve()
        cases = [('connect_then_success', 0, 3), ('connect_exhausted', 2, 3),
                 ('sql_failure', 1, 1), ('mid_connection_loss', 2, 1)]
        for mode, expected_code, expected_calls in cases:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(prefix='psql-retry-') as folder:
                root = pathlib.Path(folder)
                state = root / 'calls.txt'
                stub = root / 'psql_stub.py'
                stub.write_text('''import pathlib,sys
p=pathlib.Path(sys.argv[1]); mode=sys.argv[2]
assert sys.argv[3:]==['-h','127.0.0.1','-p','5432','-v','ON_ERROR_STOP=1','-c','SELECT current_database();']
n=int(p.read_text())+1 if p.exists() else 1; p.write_text(str(n))
if mode.startswith('connect') and (mode=='connect_exhausted' or n<3):
 sys.stderr.write('psql: error: connection to server at "127.0.0.1" failed: Bad address (0x0000271E/10014)\\n'); sys.exit(2)
if mode=='sql_failure': sys.stderr.write('psql: error: syntax error in SQL\\n'); sys.exit(1)
if mode=='mid_connection_loss': sys.stderr.write('server closed the connection unexpectedly\\n'); sys.exit(2)
print('refinvest_integration')
''', encoding='utf-8')
                driver = root / 'driver.ps1'
                driver.write_text('''param($Helper,$Executable,$Stub,$State,$Mode)
$ErrorActionPreference='Stop'
. $Helper
Invoke-IntegrationPsql $Executable $Stub $State $Mode -h 127.0.0.1 -p 5432 -v ON_ERROR_STOP=1 -c 'SELECT current_database();'
exit $LASTEXITCODE
''', encoding='utf-8')
                result = subprocess.run([PS, '-NoProfile', '-File', str(driver), str(helper),
                    sys.executable, str(stub), str(state), mode], capture_output=True, text=True)
                self.assertEqual(expected_code, result.returncode, result.stderr)
                self.assertEqual(expected_calls, int(state.read_text()))
