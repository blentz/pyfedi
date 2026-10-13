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
    # AP JSON extractors (T2): what the inbox, the refresh tasks and the entitlement checks read out of a peer's
    # document -- types, hosts, inboxes, content, media URLs, audiences, keyId owners -- before any of it is trusted.
    'ap_json': [
        'app.activitypub.util.normalise_activity_types',
        'app.activitypub.util._single_type',
        'app.activitypub.util._type_holders',
        'app.activitypub.util.object_has_missing_fields',
        'app.activitypub.util.is_vote',
        'app.activitypub.util.ensure_domains_match',
        'app.activitypub.util.host_of',
        'app.activitypub.util.authority_of',
        'app.activitypub.util.extract_domain_and_actor',
        'app.activitypub.util.normalise_actor_string',
        'app.activitypub.util.on_owner_host',
        'app.activitypub.util.names_actor',
        'app.activitypub.util.shared_inbox_of',
        'app.activitypub.util.off_host_inbox',
        'app.activitypub.util.delivery_inbox_of',
        'app.activitypub.util.content_warning_from',
        'app.activitypub.util.primary_subtag',
        'app.activitypub.util._language_map',
        'app.activitypub.util.language_variants_from',
        'app.activitypub.util.posting_warning_from_ap',
        'app.activitypub.util.media_url_from',
        'app.activitypub.util.gallery_attachments',
        'app.activitypub.util.parse_ban_expiry',
        'app.activitypub.util.castopod_episode_url',
        'app.activitypub.util._note_urls',
        'app.activitypub.util.quote_id_from',
        'app.activitypub.util._valid_quote_id',
        'app.activitypub.util.activitypub_visibility',
        'app.activitypub.util.announce_target_uri',
        'app.activitypub.util.is_top_level',
        'app.activitypub.util._id_of',
        'app.activitypub.util._attributed_ids',
        'app.activitypub.util._same_id',
        'app.activitypub.util._is_tombstone',
        'app.activitypub.util.activity_dedupe_key',
        'app.activitypub.util.stable_create_token',
        'app.activitypub.entitlement._owner_candidates',
        'app.activitypub.entitlement.addressees_of',
        'app.activitypub.entitlement._path_matches',
        'app.activitypub.entitlement.signed_fetch_exempt',
    ],
    # The peer-document coercers in front of every column a peer's actor or object fills (T2).
    'ap_models': [
        'app.models._as_int',
        'app.models._as_float',
        'app.models._as_text',
        'app.models._as_url',
        'app.models._as_dict',
        'app.models.image_url_from',
        'app.models.more_info_url_from',
        'app.models.actor_name_from_ap',
        'app.models.parse_ap_timestamp',
        'app.models.parse_ap_published',
        'app.models.property_value_fields',
        'app.models.public_key_pem',
        'app.models.language_from_ap',
        'app.models.markdown_source',
        'app.models.is_local_url',
    ],
}
