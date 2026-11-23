"""Run the complete isolated Linux suite; skipping Gunicorn is a failed release gate."""
import json
import sys
import unittest
from pathlib import Path


RUNTIME_TEST = ('test_server_runtime.ServerRuntimeTests.'
                'test_single_worker_start_stop_restart_preserves_private_data')


def require_runtime(passed):
    if RUNTIME_TEST not in passed:
        raise RuntimeError('Linux Gunicorn lifecycle test must execute and pass')


class Result(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.passed = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.passed.append(test.id())


def main():
    result = unittest.TextTestRunner(verbosity=1, resultclass=Result).run(
        unittest.defaultTestLoader.discover('.'))
    summary = {'discovered': result.testsRun, 'passed': len(result.passed),
               'failures': len(result.failures), 'errors': len(result.errors),
               'skipped': [(test.id(), reason) for test, reason in result.skipped],
               'gunicorn_passed': RUNTIME_TEST in result.passed}
    # This directory is a separate CI artifact mount, never the application's DATA_DIR.
    Path('/results/summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps(summary, indent=2))
    require_runtime(result.passed)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    sys.exit(main())
