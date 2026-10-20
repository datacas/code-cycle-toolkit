"""Run existing deterministic tests in the tracked-file snapshot only."""
from pathlib import Path
import sys
import unittest

root = Path(__file__).resolve().parent / 'checkout-git'
sys.path[:0] = [str(root / 'scripts'), str(root / 'tests')]
patterns = [
    'test_environment_recovery.py', 'test_reroute.py', 'test_grants.py',
    'test_stage_permissions.py', 'test_dependency_setup.py', 'test_stage_recovery.py',
    'test_issue_review.py', 'test_user_choices.py', 'test_dispatch_failures.py',
    'test_stage_dispatch.py', 'test_cycle_outcomes.py', 'test_executors.py',
]
suite = unittest.TestSuite()
for pattern in patterns:
    suite.addTests(unittest.defaultTestLoader.discover(str(root / 'tests'), pattern=pattern))
result = unittest.TextTestRunner(verbosity=1).run(suite)
print('AUDIT_TEST_RESULT', {'tests': result.testsRun, 'failures': len(result.failures),
                            'errors': len(result.errors), 'skipped': len(result.skipped)}, flush=True)
raise SystemExit(not result.wasSuccessful())
