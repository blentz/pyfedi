"""The route fuzzing gate (spec 2026-11-13-fuzz-every-route-design §5): every rule in the app's url_map belongs to
exactly one family, every family has an atheris target, and every rule has a committed seed that names it, so a route
added later is fuzzed or this fails.

A missing seed is written from tests.fuzz.routes.seeds_for and the test fails, as a missing golden page does: review
the new files under tests/fuzz/corpus/routes_<family>/ and commit them."""
from collections import Counter
from pathlib import Path

import pytest

from tests.fuzz import routes
from tests.fuzz.routes import FAMILIES, Draw, blueprint_of, family_of, rule_key, rules_of
from tests.fuzz.targets import REGISTRY

CORPUS_ROOT = Path(__file__).parent / 'fuzz' / 'corpus'


def test_every_rule_belongs_to_exactly_one_family(app):
    unmapped = Counter(blueprint_of(rule.endpoint) for rule in app.url_map.iter_rules()
                       if family_of(rule.endpoint) is None)
    assert not unmapped, f'blueprints in no family (rules each): {dict(sorted(unmapped.items()))}'


def test_no_blueprint_is_in_two_families():
    seen = Counter(blueprint for blueprints in FAMILIES.values() for blueprint in blueprints)
    assert [name for name, count in seen.items() if count > 1] == []


@pytest.mark.parametrize('family', sorted(FAMILIES))
def test_every_family_has_an_atheris_target_replayed_by_its_own_test(family):
    target = REGISTRY[f'routes_{family}']
    assert target.needs_app and target.own_replay and target.surface == 'routes'


@pytest.mark.parametrize('family', sorted(FAMILIES))
def test_every_rule_has_a_seed_that_names_it(app, family):
    directory = CORPUS_ROOT / f'routes_{family}'
    named = {Draw(path.read_bytes()).text(1024) for path in directory.glob('rule-*')} if directory.is_dir() else set()
    missing = {name: data for name, data in routes.seeds_for(app, family).items()
               if Draw(data).text(1024) not in named}
    if missing:
        directory.mkdir(parents=True, exist_ok=True)
        for name, data in missing.items():
            (directory / name).write_bytes(data)
        pytest.fail(f'wrote {len(missing)} seeds into {directory}: review and commit them')


@pytest.mark.parametrize('family', sorted(FAMILIES))
def test_a_seed_naming_a_rule_reaches_that_rule(app, family):
    by_key = {rule_key(rule): rule for rule in rules_of(app, family)}
    for path in sorted((CORPUS_ROOT / f'routes_{family}').glob('rule-*')):
        key = Draw(path.read_bytes()).text(1024)
        if key in by_key:   # a rule removed since leaves a seed that lands on some other rule: still a request
            assert routes.decode(app, family, path.read_bytes(), routes.World(
                ids={'user': [1]}, names={'user': ['x']})).rule is by_key[key]


def test_a_key_that_names_no_rule_still_lands_on_one_of_the_family(app):
    request = routes.decode(app, 'auth', routes.seed('no such rule'), routes.World(ids={'user': [1]},
                                                                                   names={'user': ['x']}))
    assert family_of(request.rule.endpoint) == 'auth'
