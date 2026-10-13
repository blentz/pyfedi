"""Re-export shim. The checks moved to tests/fuzz/targets/sanitizers.py.

tests/fuzz/harnesses.py used to hold the three original property checks. With
the fuzz registry (tests/fuzz/targets/) every surface has a module of its own,
and these three became the `sanitizers` surface. This name is kept so nothing
that imported from it has to move; new code should import from
tests.fuzz.targets (or read tests.fuzz.targets.REGISTRY) instead.
"""

from tests.fuzz.targets.sanitizers import (check_allowlist_html,
                                           check_is_valid_xml_utf8,
                                           check_sanitize_svg_bytes)

__all__ = ['check_allowlist_html', 'check_is_valid_xml_utf8', 'check_sanitize_svg_bytes']
