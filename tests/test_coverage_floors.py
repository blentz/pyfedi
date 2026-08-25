import os
import tempfile
import unittest

from tests.check_coverage_floors import read_floors, violations


class TestViolations(unittest.TestCase):

    def test_module_below_its_floor_is_a_violation(self):
        data = {'files': {'app/x.py': {'summary': {'percent_covered': 82.0}}}}
        self.assertEqual(violations(data, {'app/x.py': 90.0}),
                         [('app/x.py', 82.0, 90.0)])

    def test_module_at_its_floor_is_not_a_violation(self):
        data = {'files': {'app/x.py': {'summary': {'percent_covered': 90.0}}}}
        self.assertEqual(violations(data, {'app/x.py': 90.0}), [])

    def test_module_above_its_floor_is_not_a_violation(self):
        data = {'files': {'app/x.py': {'summary': {'percent_covered': 97.5}}}}
        self.assertEqual(violations(data, {'app/x.py': 90.0}), [])

    def test_module_with_no_floor_is_ignored(self):
        """Unfinished modules must not block anyone."""
        data = {'files': {'app/x.py': {'summary': {'percent_covered': 95.0}},
                          'app/y.py': {'summary': {'percent_covered': 3.0}}}}
        self.assertEqual(violations(data, {'app/x.py': 90.0}), [])

    def test_floored_module_absent_from_the_report_is_a_violation(self):
        """A module that vanishes from coverage output is a regression, not a pass.

        Renaming or failing to import a floored module would otherwise silently
        satisfy the ratchet.
        """
        self.assertEqual(violations({'files': {}}, {'app/x.py': 90.0}),
                         [('app/x.py', 0.0, 90.0)])

    def test_several_violations_are_all_reported(self):
        data = {'files': {'app/x.py': {'summary': {'percent_covered': 10.0}},
                          'app/y.py': {'summary': {'percent_covered': 20.0}}}}
        result = violations(data, {'app/x.py': 50.0, 'app/y.py': 50.0})
        self.assertEqual(len(result), 2)


class TestReadFloors(unittest.TestCase):

    def test_reads_module_floors(self):
        handle, path = tempfile.mkstemp(suffix='.ini')
        with os.fdopen(handle, 'w') as f:
            f.write('[floors]\napp/request_hooks.py = 100\napp/x.py = 42.5\n')
        try:
            self.assertEqual(read_floors(path),
                             {'app/request_hooks.py': 100.0, 'app/x.py': 42.5})
        finally:
            os.unlink(path)
