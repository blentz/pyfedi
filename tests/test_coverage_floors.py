import contextlib
import io
import json
import os
import tempfile
import unittest

from tests.check_coverage_floors import FloorsFileError, main, read_floors, violations


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


class TestReadFloorsFailsClosed(unittest.TestCase):
    """A floors file that yields no floors must be an error, never {}.

    An empty mapping means "every floor passes", so `rm coverage_floors.ini`, a
    merge that drops the [floors] section or a typo in a CI path would produce a
    green ratchet -- the exact failure this script exists to prevent.
    """

    def setUp(self):
        self.directory = tempfile.mkdtemp()

    def tearDown(self):
        for name in os.listdir(self.directory):
            os.chmod(os.path.join(self.directory, name), 0o600)
            os.unlink(os.path.join(self.directory, name))
        os.rmdir(self.directory)

    def write(self, text, name='floors.ini', mode=0o600):
        path = os.path.join(self.directory, name)
        with open(path, 'w') as handle:
            handle.write(text)
        os.chmod(path, mode)
        return path

    def test_a_missing_file_is_an_error(self):
        """configparser.read() ignores a nonexistent path; this must not."""
        with self.assertRaises(FloorsFileError):
            read_floors(os.path.join(self.directory, 'does_not_exist.ini'))

    @unittest.skipIf(os.geteuid() == 0,
                     'root bypasses file permissions, so mode 0 is still readable')
    def test_an_unreadable_file_is_an_error(self):
        path = self.write('[floors]\napp/x.py = 100\n', mode=0o000)
        with self.assertRaises(FloorsFileError):
            read_floors(path)

    def test_a_path_that_cannot_be_opened_is_an_error(self):
        """Covers the unreadable case under root, where mode 0 is still readable."""
        with self.assertRaises(FloorsFileError):
            read_floors(self.directory)

    def test_a_file_with_no_floors_section_is_an_error(self):
        path = self.write('[something_else]\nkey = value\n')
        with self.assertRaises(FloorsFileError):
            read_floors(path)

    def test_an_empty_floors_section_is_an_error(self):
        path = self.write('[floors]\n')
        with self.assertRaises(FloorsFileError):
            read_floors(path)

    def test_unparseable_ini_is_an_error(self):
        path = self.write('this is not ini at all\n')
        with self.assertRaises(FloorsFileError):
            read_floors(path)


class TestMain(unittest.TestCase):
    """main()'s exit codes, which are what CI and a human actually read."""

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()

    def tearDown(self):
        for name in os.listdir(self.directory):
            os.unlink(os.path.join(self.directory, name))
        os.rmdir(self.directory)

    def write_report(self, percent):
        path = os.path.join(self.directory, 'coverage.json')
        with open(path, 'w') as handle:
            json.dump({'files': {'app/x.py': {'summary':
                                              {'percent_covered': percent}}}}, handle)
        return path

    def write_floors(self, text):
        path = os.path.join(self.directory, 'floors.ini')
        with open(path, 'w') as handle:
            handle.write(text)
        return path

    def run_main(self, argv):
        with contextlib.redirect_stdout(self.stdout), \
                contextlib.redirect_stderr(self.stderr):
            return main(argv)

    def test_all_floors_met_exits_zero(self):
        code = self.run_main([self.write_report(100.0),
                              self.write_floors('[floors]\napp/x.py = 100\n')])

        self.assertEqual(code, 0)
        self.assertIn('All 1 module floors met.', self.stdout.getvalue())

    def test_a_violation_exits_one_and_names_the_module(self):
        code = self.run_main([self.write_report(80.0),
                              self.write_floors('[floors]\napp/x.py = 100\n')])

        self.assertEqual(code, 1)
        self.assertIn('app/x.py: 80.00% is below its floor of 100.00%',
                      self.stderr.getvalue())
        self.assertNotIn('floors met', self.stdout.getvalue())

    def test_a_missing_floors_file_exits_two_and_does_not_claim_success(self):
        """The regression this guards: it used to print "All 0 module floors met.\""""
        code = self.run_main([self.write_report(100.0),
                              os.path.join(self.directory, 'gone.ini')])

        self.assertEqual(code, 2)
        self.assertNotIn('floors met', self.stdout.getvalue())
        self.assertIn('ERROR', self.stderr.getvalue())

    def test_an_empty_floors_section_exits_two(self):
        code = self.run_main([self.write_report(100.0),
                              self.write_floors('[floors]\n')])

        self.assertEqual(code, 2)
        self.assertNotIn('floors met', self.stdout.getvalue())

    def test_a_missing_coverage_report_exits_two(self):
        code = self.run_main([os.path.join(self.directory, 'gone.json'),
                              self.write_floors('[floors]\napp/x.py = 100\n')])

        self.assertEqual(code, 2)
        self.assertIn('ERROR', self.stderr.getvalue())

    def test_wrong_argument_count_exits_two(self):
        self.assertEqual(self.run_main(['coverage.json']), 2)

    def test_the_report_path_and_mtime_are_printed(self):
        """coverage.json is gitignored and persists, so a stale one must be visible."""
        report = self.write_report(100.0)
        os.utime(report, (1000000000, 1000000000))

        self.run_main([report, self.write_floors('[floors]\napp/x.py = 100\n')])

        output = self.stdout.getvalue()
        self.assertIn(os.path.abspath(report), output)
        self.assertIn('2001-09-', output)
