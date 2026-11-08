"""The apps whose client API PieFed does not serve, probed with the calls they start with (probes.py), live."""
import pytest

from interop.apps import probes
from interop.apps.conftest import ALICE
from interop.apps.matrix import expectation, inapplicable, load_matrix, record

MATRIX = load_matrix()
PROBED = sorted((name, app['probe']) for name, app in MATRIX.items() if app.get('probe'))


@pytest.mark.parametrize('name, family', PROBED, ids=[name for name, _ in PROBED])
def test_probe(http, name, family):
    skipped = inapplicable(MATRIX[name])
    for step, (outcome, detail) in probes.run(family, http, *ALICE).items():
        if step in skipped:
            outcome, detail = 'inapplicable', expectation(MATRIX[name], step)['reason']
        record(name, step, outcome, detail, version=f'probe:{family}')
