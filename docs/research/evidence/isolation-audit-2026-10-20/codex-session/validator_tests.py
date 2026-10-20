from pathlib import Path
import sys
import unittest

BASE = Path(__file__).resolve().parent
ROOT = BASE / 'checkout-git'
sys.path[:0] = [str(BASE), str(ROOT / 'scripts'), str(ROOT / 'tests')]
from audit import command

if len(sys.argv) > 1 and sys.argv[1] == 'run':
    suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'), pattern='test_validate_package.py')
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    print('AUDIT_VALIDATOR_RESULT', {'tests': result.testsRun, 'failures': len(result.failures),
                                    'errors': len(result.errors), 'skipped': len(result.skipped)}, flush=True)
    raise SystemExit(not result.wasSuccessful())

command('validator-tests-baseline', ['python3', '-B', str(BASE / 'validator_tests.py'), 'run'], cwd=ROOT, timeout=300)
