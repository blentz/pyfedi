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
        'app.activitypub.util.remote_flair_color',
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
    # Markdown rendering and HTML text helpers (T3): every body a peer or a user writes, from markdown to the
    # rendered page -- the preprocessors, the code/link stashes, the actor-link jinja filters, the microblog title
    # and link readers, the |safe posting warning and first paragraph, and the enhanced-images extra. The
    # module-level regexes they run (LINK_PATTERN, PERSON/COMMUNITY/FEED_PATTERN, visibility.MENTION_PATTERN) are
    # not callables, so they are fuzzed through these functions and the redos_* targets rather than listed.
    'markdown': [
        'app.utils.markdown_to_html',
        'app.utils.escape_non_html_angle_brackets',
        'app.utils.handle_bold_em',
        'app.utils.escape_img',
        'app.utils.handle_lemmy_autocomplete',
        'app.utils.handle_lemmy_spoilers',
        'app.utils.handle_naked_spoilers',
        'app.utils.handle_spoiler_spacing',
        'app.utils.handle_video_embeds',
        'app.utils.make_quotes_straight',
        'app.utils.handle_reddit_spoilers',
        'app.utils.handle_blockquotes',
        'app.utils.links_with_parens',
        'app.utils.fix_www_links',
        'app.utils.handle_better_lists',
        'app.utils.piefed_markdown_to_lemmy_markdown',
        'app.utils.stash_code_html',
        'app.utils.stash_code_md',
        'app.utils.stash_link_html',
        'app.utils.pop_code',
        'app.utils.pop_link',
        'app.utils.community_link_to_href',
        'app.utils.feed_link_to_href',
        'app.utils.person_link_to_href',
        'app.utils.html_to_text',
        'app.utils.markdown_to_text',
        'app.utils.shorten_string',
        'app.utils.shorten_url',
        'app.utils.remove_images',
        'app.utils.mastodon_extra_field_link',
        'app.utils.microblog_content_to_title',
        'app.utils.microblog_content_to_link',
        'app.utils.strip_quote_fallback',
        'app.utils.first_paragraph',
        'app.utils.sanitise_posting_warning',
        'app.markdown_extras.EnhancedImages.run',
        'app.markdown_extras.apply_enhanced_image_attributes',
    ],
    # Regex-driven readers of short peer- or user-supplied strings (T3's ReDoS tier): host names in discovery
    # directories, e-mail addresses and fediverse handles, and the URLs a podcast credit is matched by.
    'text_patterns': [
        'app.discovery.sources.is_hostname',
        'app.utils.validate_email',
        'app.instance.util.is_fedi_handle',
        'app.discovery.credits._normal_url',
        'app.discovery.credits._urls_in',
        'app.discovery.credits._document_urls',
    ],
    # URLs, redirects and request-derived hosts (T4): whether a peer's link may be stored and which host it names,
    # whether a redirect stays on this site, whether a fetch would reach a private address, the domain a User-Agent
    # claims, the webfinger resource, an RSS item's link and a peer's nodeinfo links.
    'urls': [
        'app.utils.url_scheme',
        'app.utils.has_unsafe_url_scheme',
        'app.utils.url_is_storable',
        'app.utils._host_reads_alike',
        'app.utils.url_host',
        'app.utils.url_is_parseable',
        'app.utils.inbox_domain',
        'app.utils.domain_from_email',
        'app.utils.is_bot',
        'app.utils.guess_mime_type',
        'app.utils.mimetype_from_url',
        'app.utils.is_video_url',
        'app.utils.is_video_hosting_site',
        'app.utils.is_safe_redirect_target',
        'app.utils.safe_redirect_target',
        'app.storefront.browser_host',
        'app.storefront._css_urls',
        'app.storefront._srcset_urls',
        'app.storefront.rewrite_links',
        'app.storefront._fetchable',
        'app.storefront._proxied_style',
        'app.storefront.domain_of_url',
        'app.onion_media.remote_for',
        'app.utils.is_invalid_get_request_uri',
        'app.pinned_http.is_refused_address',
        'app.utils.requestor_domain',
        'app.activitypub.routes.process_webfinger_request',
        'app.cli.strip_tracking',
        'app.utils.remote_instance_software',
        'app.utils._nodeinfo_href_on_host',
        'app.utils._nodeinfo2_software',
        'app.interop.workarounds._host',
        'app.discovery.instance_answers._host',
    ],
    # Branding (per-domain branding, project 4): an admin's CSS, tagline, sidebar and About HTML and logo URLs, scanned
    # for off-site loads and scrubbed of them on the onion, and custom CSS kept inside its style element.
    'branding': [
        'app.branding.offsite_references',
        'app.branding.onion_safe',
        'app.branding._style_kept_open',
    ],
    # Discovery (T4): third-party directory rows and podcast feeds, turned into entries, links and credits.
    'discovery': [
        'app.discovery.filters.request_host',
        'app.discovery.filters.strict_ascii_host',
        'app.discovery.filters.ascii_host',
        'app.discovery.filters.clean_https_url',
        'app.discovery.filters.clean_name',
        'app.discovery.filters.looks_nsfw',
        'app.discovery.sources.is_username',
        'app.discovery.sources.uri_is_on',
        'app.discovery.sources.actor_url_on',
        'app.discovery.sources.as_count',
        'app.discovery.mastodon.top_mastodon_servers',
        'app.discovery.mastodon.account_to_entry',
        'app.discovery.pixelfed.pixelfed_hosts',
        'app.discovery.pixelfed.profile_to_entry',
        'app.discovery.peertube.channel_to_entry',
        'app.discovery.castopod.podcast_to_entry',
        'app.discovery.external_search.clean_video_url',
        'app.discovery.external_search.video_from',
        'app.discovery.credits.parse_feed',
        'app.discovery.credits._declares_a_dtd',
        'app.discovery.credits.episode_credits',
        'app.discovery.credits.parse_feed_credits',
    ],
    # NNTP (T5): every line a news client sends the bridge, from before any login -- the command dispatch, the
    # AUTHINFO exchange, POST's article lines, NEWNEWS/NEWGROUPS dates, OVER/HDR/LISTGROUP ranges and the message-ids
    # of ARTICLE/HEAD/BODY -- and the group names the bridge derives from community names.
    'nntp': [
        'app.nntp.nntpserver.parse_datetime',
        'app.nntp.nntpserver.parse_range',
        'app.nntp.nntpserver.numbers_in_group',
        'app.nntp.nntpserver.NNTPConnectionHandler.handle',
        'app.nntp.nntpserver.NNTPConnectionHandler._getline',
        'app.nntp.nntpserver.NNTPConnectionHandler._getlines',
        'app.nntp.nntpserver.NNTPConnectionHandler.auth',
        'app.nntp.nntpserver.NNTPConnectionHandler.newnews',
        'app.nntp.nntpserver.NNTPConnectionHandler.newgroups',
        'app.nntp.nntpserver.NNTPConnectionHandler.listgroup',
        'app.nntp.nntpserver.NNTPConnectionHandler.list',
        'app.nntp.nntpserver.NNTPConnectionHandler.hdr',
        'app.nntp.nntpserver.NNTPConnectionHandler.overview',
        'app.nntp.nntpserver.NNTPConnectionHandler.stat',
        'app.nntp.nntpserver.NNTPConnectionHandler.article',
        'app.nntp.nntpserver.NNTPConnectionHandler.head',
        'app.nntp.server.PieFedNNTPConnectionHandler.select_group',
        'app.nntp.server._parse_message_id',
        'app.nntp.server._sanitize_group_name',
    ],
    # The alpha API (T6): every request schema a route parses its JSON body, query string or upload with, the
    # hand-written validators in those schemas, the error handler that answers a malformed request, and the helpers a
    # client-chosen colour or sort order reaches.
    'api': [
        'app.api.alpha.schema.DefaultSchema.load',
        'app.api.alpha.schema.validate_datetime_string',
        'app.api.alpha.schema.validate_color_code',
        'app.api.alpha.schema.validate_non_empty_string',
        'app.api.alpha.shared_error_handler',
        'app.utils.expand_hex_color',
        'app.utils.safe_order_by',
    ],
    # Uploaded import files (T6): the settings import JSON, the Mastodon follows CSV and the admin's ban list, up to
    # the first lookup or write.
    'imports': [
        'app.admin.util.ban_list_entries',
        'app.user.routes.settings_import_contents',
        'app.user.routes.import_entries',
        'app.user.routes.import_note_text',
        'app.instance.util.mastodon_csv_handles',
    ],
    # Image decode (T7): every upload and peer thumbnail Pillow decodes, from the bytes to the resized result, and
    # the C2PA manifest reader every post image goes through. The decode steps are inline in the first five, so
    # tests/fuzz/targets/images.py runs the same steps on BytesIO.
    'images': [
        'app.shared.upload.process_upload',
        'app.shared.post.edit_post',
        'app.community.util.save_icon_file',
        'app.community.util.save_banner_file',
        'app.utils.url_to_thumbnail_file',
        'app.utils.to_srgb',
        'app.utils.scale_gif',
        'app.utils.get_new_frames',
        'app.utils.save_new_gif',
        'app.utils.inspect_image_c2pa',
        'app.utils.save_image_without_metadata',
        'app.utils.refuse_over_remote_thumbnail_cap',
        'app.activitypub.util.make_image_sizes_async',
    ],
    # Video channels (T8): the ffprobe JSON an uploaded file's media record is made from, and the PeerTube Video a
    # local video_channel post is sent and fetched as.
    'video_channels': [
        'app.shared.tasks.media.validate_probe',
        'app.activitypub.serializers.peertube.peertube_video',
    ],

    # Podcasts (T9): the audio Document a local podcast episode's Page carries, and the fields its feed item adds.
    'podcasts': [
        'app.activitypub.serializers.podcast.episode_attachment',
        'app.rss_extras.episode_item_fields',
    ],
}
