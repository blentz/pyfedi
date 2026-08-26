"""`requirements.txt` is the production install; test dependencies are not in it.

`Dockerfile:16`, `deploy.sh:9` and INSTALL.md all run
`pip install -r requirements.txt`, and the Dockerfile's `builder` stage -- the
one whose `/venv` is copied into `runtime` -- is where that happens. So anything
listed there ships to production and has to install on every architecture PieFed
runs on.

`atheris` does not. It publishes no aarch64 wheel, so on an ARM64 host pip falls
back to building atheris 3.0.0 from source, which needs clang with libFuzzer. On
a stock Debian/Ubuntu VPS that build fails and takes the entire install down with
it. The final whole-branch review measured this directly:

    pip download atheris --only-binary=:all: --platform manylinux_2_17_aarch64
      ERROR: Could not find a version that satisfies the requirement atheris
             (from versions: none)

These tests fail if a test dependency is put back into the production file, or
if the Dockerfile's `test` stage stops being what compose.test.yaml builds --
which would leave the test container unable to collect the suite.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Names that must never appear in requirements.txt. atheris is the one that
# breaks an install; the rest are here so the rule is "test dependencies live in
# requirements-test.txt", with no case-by-case judgement to get wrong later.
TEST_ONLY = ['pytest', 'pytest-cov', 'pytest-timeout', 'respx', 'moto', 'fakeredis', 'atheris']


def requirement_names(path):
    """The distribution names in a requirements file, lower-cased.

    Strips comments, extras (`Flask-Limiter[redis]`), version specifiers and
    environment markers, so `pytest-cov` and `pytest-cov==1.2` both read as
    `pytest-cov`.
    """
    names = []
    for line in (REPO_ROOT / path).read_text().splitlines():
        line = line.split('#', 1)[0].strip()
        if not line:
            continue
        name = line.split(';', 1)[0]
        for separator in ('[', '==', '>=', '<=', '~=', '!=', '>', '<', '='):
            name = name.split(separator, 1)[0]
        names.append(name.strip().lower())
    return names


def test_no_test_dependency_is_in_the_production_requirements():
    production = requirement_names('requirements.txt')
    leaked = [name for name in TEST_ONLY if name in production]
    assert not leaked, (
        f'{leaked} are test-only and requirements.txt is the production install '
        f'(Dockerfile:16, deploy.sh:9, INSTALL.md). atheris in particular has no '
        f'aarch64 wheel, so this breaks `pip install -r requirements.txt` on every '
        f'ARM64 host. Move them to requirements-test.txt.')


def test_the_test_requirements_file_carries_them_all():
    """The other half. Without this, "remove atheris" passes the test above by
    deleting the fuzzing infrastructure's only dependency."""
    test_requirements = requirement_names('requirements-test.txt')
    missing = [name for name in TEST_ONLY if name not in test_requirements]
    assert not missing, (
        f'{missing} are missing from requirements-test.txt. The test container '
        f'installs only that file on top of requirements.txt, so the suite '
        f'cannot run without them.')


def test_the_dockerfile_installs_the_test_requirements_in_a_stage_of_its_own():
    dockerfile = (REPO_ROOT / 'Dockerfile').read_text()
    assert 'FROM builder AS test' in dockerfile, (
        'The test dependencies need a stage that `runtime` does not copy from.')
    assert 'requirements-test.txt' in dockerfile, (
        'Nothing installs requirements-test.txt, so the test container has no pytest.')

    # `runtime` must come last: a build with no --target builds the final stage,
    # and that has to stay the production image.
    stages = [line for line in dockerfile.splitlines() if line.startswith('FROM ')]
    assert stages[-1].endswith('AS runtime'), (
        f'The last FROM in the Dockerfile is {stages[-1]!r}. A `docker build` '
        f'with no --target builds it, so it must be the production image.')

    # ...and it must copy from `builder`, not from `test`.
    assert 'COPY --from=builder /venv /venv' in dockerfile, (
        'The runtime image must take /venv from `builder`. Copying it from '
        '`test` would put atheris and pytest into production.')


def test_the_test_container_builds_the_test_stage():
    compose = (REPO_ROOT / 'compose.test.yaml').read_text()
    assert 'target: test' in compose, (
        'compose.test.yaml builds a stage without the test dependencies, so '
        'test-runner cannot collect the suite.')
