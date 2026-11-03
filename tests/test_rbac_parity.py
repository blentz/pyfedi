"""RBAC parity: the table as a whole.

Spec docs/superpowers/specs/2026-11-03-rbac-design.md, build step 1. tests/rbac_parity.py explains the table and
runs a row; the rows live in one module per area (tests/test_rbac_parity_*.py), because a module runs on one
xdist worker (tests/conftest.py worker_group) and the whole table on one worker is a long serial tail.
"""
from tests.test_rbac_parity_admin import ROWS as ADMIN
from tests.test_rbac_parity_community import ROWS as COMMUNITY
from tests.test_rbac_parity_functions import ROWS as FUNCTIONS
from tests.test_rbac_parity_post import ROWS as POST
from tests.test_rbac_parity_visibility import ROWS as VISIBILITY

ROWS = ADMIN + COMMUNITY + POST + VISIBILITY + FUNCTIONS


def test_rows_are_unique():
    ids = [row.id for row in ROWS]
    assert len(ids) == len(set(ids)), sorted(i for i in ids if ids.count(i) > 1)


def test_every_row_allows_someone_and_denies_someone():
    """A row that allows nobody, or everybody, would pass however the code changed."""
    for row in ROWS:
        assert 'A' in row.expect and 'D' in row.expect, row.id
