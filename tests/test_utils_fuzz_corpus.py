"""Replays every committed fuzz input deterministically.

The campaign itself is nondeterministic and runs on demand (see tests/README.md).
This file is what keeps a finding fixed: an input that once broke a property is
committed here and checked on every run, forever.

Nothing here imports atheris. The property checks in tests/fuzz/targets/ are
plain functions, so the corpus replays inside the ordinary suite -- fast,
deterministic, and without the fuzzer's tracing fighting coverage.py's.

Driven by tests.fuzz.targets.REGISTRY: every registered target's corpus is
replayed, and a target registered without a corpus fails tests/test_fuzz_registry.py
rather than silently replaying nothing here. Two Target fields change how a case
runs:

* needs_app -- the case runs inside the suite's own `app` fixture (an app built
  from TestConfig with its context pushed). That fixture skips when
  TEST_DATABASE_URL is unset, so those cases need ./run_tests.sh; the
  sanitizer targets do not, and still replay from a bare checkout.
* budget_ms -- the case also fails if one input takes longer than that, in CPU
  time of the thread running it (time.thread_time). This is the ReDoS gate: a
  slow input is the finding, so it has to fail the deterministic suite, not only
  a campaign. CPU time, not wall-clock: a backtracking regex or a quadratic
  parser is CPU-bound, while wall-clock also counts the time the scheduler gives
  the CPU to other work. Measured 2026-10-14 in the test container, the slowest
  seed (markdown_to_html/pump_image_links, budget 2000 ms) took 0.60 s alone,
  0.62 s under --cov (coverage's default sys.monitoring core; 0.96 s with
  COVERAGE_CORE=ctrace), and 1.2-1.3 s wall-clock but 0.82-0.84 s CPU beside 64
  busy processes on 32 hardware threads. CPU time still grows when the machine
  slows (2047 ms in a full parallel suite run), so an input over its budget is
  held to the budget times how much slower than nominal the machine runs a
  fixed reference workload at that moment (machine_slowdown); see run_case.
  With markdown_to_html's rewrites switched off, nine of its twelve rewritten
  pump seeds take 2.5-11 s of CPU, so the budget still catches their return.

The production change that would make these tests fail: any edit to a function
listed in tests/fuzz/surfaces.py that breaks the property its target asserts --
for the sanitizer surface, letting script markup, an event-handler attribute, a
javascript:/vbscript: URL in an anchor's href, or an entity declaration through
app.utils.allowlist_html or app.utils.sanitize_svg_bytes, or making
app.utils.is_valid_xml_utf8's character table disagree with XML 1.0 -- or that
makes a budgeted target's input exceed its budget.
"""

import pathlib
import re
import time

import pytest

from tests.fuzz.targets import REGISTRY, Target

CORPUS_ROOT = pathlib.Path(__file__).parent / 'fuzz' / 'corpus'

# Inputs that break a property because the DEFECT IS REAL AND UNFIXED. A security
# fix in deployed software is the project owner's decision, so these are reported
# and pinned, not patched.
#
# strict=True on purpose: when a defect is fixed its cases XPASS and fail the
# run, which is the signal to delete the entry rather than let a stale xfail hide
# a later regression.
#
# EMPTY, deliberately. It held six entries covering two defects, both of which
# the project owner authorised and which are now fixed in app/utils.py:
#
#   * the javascript:-URL bypass (javascript_url_leading_space,
#     javascript_url_leading_tab_entity, javascript_url_newline_in_scheme).
#     allowlist_html decided the scheme with furl(href).scheme, and furl does not
#     apply the WHATWG normalisation a browser applies before reading a scheme.
#     It now calls app.utils.has_unsafe_url_scheme, which does.
#   * IndexError out of escape_non_html_brackets on '</>' (empty_closing_tag,
#     empty_closing_tag_with_space, empty_closing_tag_indexerror_as_found).
#
# Their corpus files are still here and still replayed on every run -- they are
# now regression pins that PASS, which is exactly what a fixed finding should
# become. The strict xfails XPASSed the moment the fix landed, which is what
# prompted deleting these entries rather than relaxing them.
KNOWN_UNFIXED = {}


def corpus_cases():
    """A list, not a generator: pytest 10 will stop accepting the latter."""
    cases = []
    for name, target in REGISTRY.items():
        directory = CORPUS_ROOT / name
        for path in sorted(directory.glob('*')):
            if path.is_file():
                case_id = f'{name}/{path.name}'
                marks = []
                if case_id in KNOWN_UNFIXED:
                    marks.append(pytest.mark.xfail(strict=True, reason=KNOWN_UNFIXED[case_id]))
                cases.append(pytest.param(target, path, id=case_id, marks=marks))
    return cases


def reference_work() -> int:
    """A fixed piece of CPU-bound Python -- formatting, sorting, a regex scan --
    whose time says how fast this machine is running Python right now."""
    words = [str(i * 7919 % 100003) for i in range(80_000)]
    return len(re.findall(r'[0-9]+7', ' '.join(sorted(words))))


# reference_work's CPU time in the test container on an idle machine (AMD Ryzen
# 9 7945HX, 2026-10-14), the machine every budget_ms was measured on.
REFERENCE_WORK_MS = 44


def machine_slowdown(clock=time.thread_time) -> float:
    """How many times slower than nominal this machine runs reference_work now,
    and never below 1: a faster machine does not tighten any budget."""
    started = clock()
    reference_work()
    return max(1.0, (clock() - started) * 1000 / REFERENCE_WORK_MS)


def run_case(target, data: bytes, clock=time.thread_time, slowdown=machine_slowdown) -> None:
    """Push one input through its target's check, then hold it to the budget.

    The check runs first and its AssertionError propagates untouched, so a
    property break is reported as itself and not as a slow input. The budget is
    the thread's CPU time around the check alone: reading the file is outside it.

    An input over its budget is held to the budget times machine_slowdown(),
    measured then and only then. CPU time still grows when the machine slows:
    beside other busy processes the cores clock down and share their units
    (SMT), and in a full parallel suite run pump_image_links took 2047 ms of CPU
    against its 0.60 s alone. reference_work slows by the same factor (its ratio
    to the pump stayed 6.3-6.8, idle and beside 64 busy processes), so the
    scaled budget allows for the machine, not for a slower function: a
    regression is as many times slower than reference_work on any machine.

    `clock` and `slowdown` are parameters only so the budget assertion can be
    tested without a genuinely slow check or machine.
    """
    started = clock()
    target.check(data)
    elapsed_ms = (clock() - started) * 1000
    if target.budget_ms is not None and elapsed_ms > target.budget_ms:
        factor = slowdown()
        assert elapsed_ms <= target.budget_ms * factor, (
            f'{target.name}: one input took {elapsed_ms:.0f} ms, over its '
            f'{target.budget_ms} ms budget ({target.budget_ms * factor:.0f} ms on '
            f'this machine now, {factor:.2f} times slower than nominal)')


@pytest.mark.parametrize('target,path', corpus_cases())
def test_corpus_input_still_satisfies_its_property(target, path, request):
    if target.needs_app:
        # The session `app` fixture pushes its app context for the whole
        # session, so requesting it is all a needs_app case has to do. It is
        # requested only here, so the sanitizer cases do not need a database.
        request.getfixturevalue('app')
    run_case(target, path.read_bytes())


@pytest.mark.parametrize('case_id', sorted(KNOWN_UNFIXED))
def test_every_known_unfixed_case_is_present(case_id):
    """A strict xfail only reports a fix if the file it names still exists.

    Delete the corpus file and the parametrize id disappears with it, taking the
    xfail and its report of the defect along -- silently. This asserts the
    pairing so the reminder cannot be removed by accident.

    While KNOWN_UNFIXED is empty this collects as a single test skipped with
    "got empty parameter set", which is the honest report: there is nothing
    pinned as unfixed, and the guard is here for the next finding."""
    name, _, filename = case_id.partition('/')
    assert (CORPUS_ROOT / name / filename).is_file(), (
        f'{case_id} is named in KNOWN_UNFIXED but no longer exists')


class TestRunCase:
    """The budget and needs_app paths, which no target exercised when they were
    written: the sanitizer targets are untimed and need no app. Without these, a
    run_case that never compared against budget_ms would pass every corpus case
    above and the ReDoS gate would be a gate in name only."""

    @staticmethod
    def _target(check, budget_ms=None, needs_app=False):
        return Target(name='fake', check=check, surface='sanitizers',
                      budget_ms=budget_ms, needs_app=needs_app)

    @staticmethod
    def _clock(*readings):
        """A perf_counter stand-in returning `readings`, in seconds, in order."""
        values = iter(readings)
        return lambda: next(values)

    def test_an_input_over_its_budget_fails(self):
        target = self._target(lambda data: None, budget_ms=50)
        with pytest.raises(AssertionError, match='took 51 ms, over its 50 ms budget'):
            run_case(target, b'x', clock=self._clock(10.0, 10.051), slowdown=lambda: 1.0)

    def test_an_input_over_its_budget_on_a_slowed_machine_is_held_to_the_scaled_budget(self):
        target = self._target(lambda data: None, budget_ms=2000)
        run_case(target, b'x', clock=self._clock(0.0, 2.9), slowdown=lambda: 1.5)
        with pytest.raises(AssertionError, match=r'took 3100 ms, over its 2000 ms budget \(3000 ms on this machine'):
            run_case(target, b'x', clock=self._clock(0.0, 3.1), slowdown=lambda: 1.5)

    def test_the_machine_is_measured_only_when_an_input_is_over_its_budget(self):
        def never():
            raise AssertionError('measured the machine for an input within its budget')
        run_case(self._target(lambda data: None, budget_ms=50), b'x', clock=self._clock(0.0, 0.05), slowdown=never)

    def test_the_slowdown_is_the_reference_work_against_its_nominal_time_and_never_below_one(self):
        nominal = REFERENCE_WORK_MS / 1000
        assert machine_slowdown(clock=self._clock(5.0, 5.0 + 2 * nominal)) == pytest.approx(2.0)
        assert machine_slowdown(clock=self._clock(5.0, 5.0 + nominal / 3)) == 1.0

    def test_the_reference_work_takes_about_its_nominal_time_on_an_idle_machine(self):
        """REFERENCE_WORK_MS is a measurement: re-measure it if the reference work changes. A loose check, since
        the suite may share the machine; it catches a reference that is now ten times lighter or heavier."""
        started = time.thread_time()
        reference_work()
        elapsed_ms = (time.thread_time() - started) * 1000
        assert REFERENCE_WORK_MS / 10 < elapsed_ms < REFERENCE_WORK_MS * 10

    def test_an_input_exactly_at_its_budget_passes(self):
        target = self._target(lambda data: None, budget_ms=50)
        run_case(target, b'x', clock=self._clock(0.0, 0.05))

    def test_an_untimed_target_is_never_held_to_a_budget(self):
        target = self._target(lambda data: None, budget_ms=None)
        run_case(target, b'x', clock=self._clock(0.0, 3600.0))

    def test_a_property_break_is_reported_as_itself_not_as_slowness(self):
        def breaks(data):
            raise AssertionError('property broke')
        target = self._target(breaks, budget_ms=1)
        with pytest.raises(AssertionError, match='property broke'):
            run_case(target, b'x', clock=self._clock(0.0, 99.0), slowdown=lambda: 1.0)

    def test_time_the_check_spends_waiting_is_not_held_against_its_budget(self):
        """The budget is the check's CPU time: wall-clock time also counted every moment the scheduler gave the
        CPU to other work, so the pump seeds failed under coverage beside other test files and campaigns."""
        run_case(self._target(lambda data: time.sleep(0.2), budget_ms=50), b'x')

    def test_cpu_time_the_check_spends_is_held_against_its_budget(self):
        def spin(data):
            started = time.thread_time()
            while time.thread_time() - started < 0.1:
                pass
        with pytest.raises(AssertionError, match='over its 50 ms budget'):
            run_case(self._target(spin, budget_ms=50), b'x', slowdown=lambda: 1.0)

    def test_the_check_receives_the_bytes_unchanged(self):
        seen = []
        run_case(self._target(seen.append), b'\x00\xff')
        assert seen == [b'\x00\xff']

    def test_a_needs_app_case_runs_inside_an_app_context(self, request, tmp_path):
        from flask import has_app_context

        contexts = []
        target = self._target(lambda data: contexts.append(has_app_context()), needs_app=True)
        path = tmp_path / 'seed'
        path.write_bytes(b'x')
        test_corpus_input_still_satisfies_its_property(target, path, request)
        assert contexts == [True]
