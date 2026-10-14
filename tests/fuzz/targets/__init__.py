"""Every fuzz target, collected into one registry.

One module per input surface (sanitizers, signatures, AP JSON, ...). Each
exports plain property checks and a TARGETS list of Target records; this
package gathers them into REGISTRY, keyed by target name. run_campaign, the
corpus replay (tests/test_utils_fuzz_corpus.py), the smoke campaign and the
registry gate (tests/test_fuzz_registry.py) all read REGISTRY and nothing else,
so a target exists in all four places or in none.

Adding a surface means adding its module to SURFACE_MODULES below. The registry
gate fails if a module in this package is left out, because an unlisted module
is a set of targets nothing ever runs.

Nothing here imports atheris, for the same reason the checks do not: the corpus
replay runs in the ordinary suite, under coverage, without the fuzzer.
"""

from tests.fuzz.targets import ap_json, ap_models, discovery, markdown, sanitizers, signatures, urls
from tests.fuzz.targets._target import Target

SURFACE_MODULES = [sanitizers, signatures, ap_json, ap_models, markdown, urls, discovery]


def collect(modules) -> dict[str, Target]:
    """name -> Target over every module's TARGETS, refusing a duplicate name.

    A duplicate is an import-time error rather than last-one-wins: two targets
    sharing a name share a corpus directory, so one would silently replay and
    fuzz against the other's check.
    """
    registry = {}
    for module in modules:
        for target in module.TARGETS:
            if target.name in registry:
                raise ValueError(
                    f'fuzz target {target.name!r} is registered twice '
                    f'(again in {module.__name__})')
            registry[target.name] = target
    return registry


REGISTRY = collect(SURFACE_MODULES)

__all__ = ['REGISTRY', 'SURFACE_MODULES', 'Target', 'collect']
