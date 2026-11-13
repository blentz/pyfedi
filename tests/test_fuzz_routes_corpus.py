"""Replays every committed route seed (spec 2026-11-13-fuzz-every-route-design §5, gate 2): the world is seeded once
per family, and each input runs against it restored from its snapshot, held to the same oracle and budget as the
atheris campaign (its target's own check). A family's every finding is listed together.

KNOWN_UNFIXED pins a finding queued for its tier as strictly failing, with why: one that starts to pass fails here."""
from pathlib import Path

import pytest

from tests.fuzz import routes
from tests.fuzz.routes import FAMILIES
from tests.fuzz.targets import REGISTRY

CORPUS_ROOT = Path(__file__).parent / 'fuzz' / 'corpus'
KNOWN_UNFIXED: dict[str, str] = {}   # 'routes_<family>/<file>' -> the finding, until its fix


@pytest.mark.parametrize('family', sorted(FAMILIES))
def test_every_route_seed_satisfies_the_oracle(db_session, family):
    routes.prepare(force=True)
    failures, fixed = [], []
    for path in sorted((CORPUS_ROOT / f'routes_{family}').glob('*')):
        case = f'routes_{family}/{path.name}'
        try:
            REGISTRY[f'routes_{family}'].check(path.read_bytes())
        except AssertionError as finding:
            if case not in KNOWN_UNFIXED:
                failures.append(f'{case}: {finding}')
        else:
            if case in KNOWN_UNFIXED:
                fixed.append(case)
    assert not fixed, f'pinned as unfixed but passing now: remove them from KNOWN_UNFIXED: {fixed}'
    assert not failures, f'{len(failures)} findings:\n' + '\n'.join(failures)
