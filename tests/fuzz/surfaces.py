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
    # Signatures and transport (T1): every header and body an inbound request is verified by, before its sender
    # is known, plus the capped reader under every outbound GET and the Accept header.
    'signatures': [
        'app.activitypub.signature.parse_signature_header',
        'app.activitypub.signature.signature_part',
        'app.activitypub.signature.HttpSignature.parse_signature',
        'app.activitypub.signature.HttpSignature.verify_signature',
        'app.activitypub.signature.parse_http_date',
        'app.activitypub.signature.parse_ld_date',
        'app.activitypub.signature.LDSignature.verify_signature',
        'app.activitypub.signature.LDSignature.normalized_hash',
        'app.activitypub.signature_rfc9421.parse_dictionary',
        'app.activitypub.signature_rfc9421.parse_key_id',
        'app.activitypub.signature_rfc9421.check_content_digest',
        'app.activitypub.signature_rfc9421.signature_base',
        'app.activitypub.signature_rfc9421.normalise_covered',
        'app.activitypub.signature_rfc9421.verify',
        'app.activitypub.ld_contexts.check_ld_coverage',
        'app.activitypub.ld_contexts.strip_empty_values',
        'app.activitypub.ld_contexts.ld_document_loader',
        'app.utils._read_capped_body',
        'app.utils._Inflater',
        'app.utils.accepted_media_ranges',
    ],
}
