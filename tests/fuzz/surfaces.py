"""The committed manifest of PieFed's external input entry points.

SURFACES maps a surface key to the dotted names of the production functions it
covers: every parser of remote or user-supplied bytes is meant to appear under
exactly one surface, and every surface is meant to be fuzzed by at least one
target in tests.fuzz.targets.REGISTRY. tests/test_fuzz_registry.py holds both
halves to that: each name here must import and resolve to a callable, and each
surface must have a target. A name that is renamed or deleted in app/ therefore
fails the gate instead of leaving the manifest quietly describing code that is
gone.

A dotted name is a module path followed by an attribute path, so a method is
written as module.Class.method.

A function is listed by what it parses, not by which target happens to call it:
app.utils.sanitize_svg is NOT listed, because its only input handling is
sanitize_svg_bytes (see tests/fuzz/targets/sanitizers.py's module docstring).
"""

SURFACES: dict[str, list[str]] = {
    # Remote HTML and SVG, and the XML character check in front of feed output.
    'sanitizers': [
        'app.utils.allowlist_html',
        'app.utils.is_valid_xml_utf8',
        'app.utils.sanitize_svg_bytes',
    ],
}
