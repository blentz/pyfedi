"""A notification's or report's `targets` keeps each account's or community's id beside its name (follow-up F1).

The name (`User.stored_link()`: bare on the first domain, `name@domain` on another) is for display. A link goes by the
id, so it reaches the same account on whichever domain the viewer is on. The id key is the name key with its trailing
`_user_name` (else `_name`) replaced by `_id`: `suspect_user_user_name` -> `suspect_user_id`, `author_user_name` ->
`author_id`, `community_name` -> `community_id`."""
import ast
import pathlib

import pytest

from app.local_names import target_id_key

APP = pathlib.Path(__file__).resolve().parent.parent / 'app'


@pytest.mark.parametrize('name_key, id_key', [
    ('suspect_user_user_name', 'suspect_user_id'),
    ('reporter_user_name', 'reporter_id'),
    ('author_user_name', 'author_id'),
    ('community_name', 'community_id'),
    ('suspect_community_name', 'suspect_community_id'),
])
def test_the_id_key_is_the_name_key_with_its_name_suffix_replaced(name_key, id_key):
    assert target_id_key(name_key) == id_key


def _calls_stored_link(node) -> bool:
    return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == 'stored_link'
               for n in ast.walk(node))


def _stored_names_without_an_id():
    missing = []
    for path in sorted(APP.rglob('*.py')):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Dict):
                continue
            keys = {k.value for k in node.keys if isinstance(k, ast.Constant)}
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and _calls_stored_link(value) \
                        and target_id_key(key.value) not in keys:
                    missing.append(f'{path.relative_to(APP.parent)}:{key.lineno} {key.value}')
    return missing


def test_every_targets_dict_that_stores_a_name_stores_the_id_beside_it():
    assert _stored_names_without_an_id() == []
