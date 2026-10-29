"""The deliberate federation and identity reads of SERVER_URL / SERVER_NAME in app/ (onion storefront spec §3.4).

repo-relative file -> enclosing function (qualified: `Class.method`, `outer.inner`; '<module>' at module level) ->
how many direct reads of `config['SERVER_URL']` / `config['SERVER_NAME']` it makes on purpose. Each is an
ActivityPub id, an actor or collection URL, a signature keyId, WebFinger or NodeInfo, a handle's domain, a value
stored in the database as an identifier, or the canonical host in a "this is ours" test. None varies with the
request's Host.

A presentation use (a page, a redirect, a media URL, RSS, an email sent during a request) is not listed: it calls
`app.storefront.storefront_url()` instead. tests/test_storefront_url_inventory.py fails on any read that is not
listed here. The reason for each entry is in .superpowers/sdd/2026-10-17-onion-storefront/task-4a-report.md.

To add one: be sure it is identity, not presentation, then raise its count here.
"""

CANONICAL_USES: dict[str, dict[str, int]] = {
    'app/__init__.py': {
        'create_app': 2,
    },
    'app/activitypub/actor.py': {
        'find_actor_by_url': 2,
    },
    'app/activitypub/routes.py': {
        'rerun_parked_request': 1,
        'user_profile': 1,
    },
    'app/activitypub/signature.py': {
        '_check_signed_host': 1,
        '_target_uri': 1,
    },
    'app/activitypub/util.py': {
        'create_post_reply': 1,
        'process_report': 2,
        'update_post_from_activity': 2,
        'update_post_reply_from_activity': 2,
    },
    'app/admin/routes.py': {
        'admin_federation_ban_lists': 1,
        'admin_topics_export': 1,
    },
    'app/admin/util.py': {
        'move_community_images_to_here': 2,
    },
    'app/api/alpha/views.py': {
        '_neutral_post': 1,
        'private_message_view': 1,
        'reply_removal_ack_view': 1,
    },
    'app/cli.py': {
        'register.config_check': 2,
        'register.init_db': 2,   # the local Instance row, and the default SiteDomain row init-db seeds
        'register.lemmy_import': 1,
        'register.move_more_post_images_to_s3': 1,
        'register.process_email_bounces': 4,
        'register.rss_feeds': 1,
    },
    'app/community/routes.py': {
        'community_invite': 1,
        'community_move': 1,   # the community handle shown on the move page names the federation domain
    },
    'app/community/server_view_list.py': {
        'server_view_select': 1,
    },
    'app/discovery/instance_answers.py': {
        'instance_actor_url': 1,
    },
    'app/federation_urls.py': {
        'hosted_domain_names': 1,   # the hosts a fan-out never sends to
    },
    'app/feed/util.py': {
    },
    'app/local_names.py': {
        'default_domain_user_url': 1,   # a bare name on the default domain is found by the actor URL SERVER_URL builds
    },
    'app/local_only.py': {
        'outbound_refusal': 1,   # the canonical host is ours in the "names our post or comment" test (plan B task 7)
        'notification_stamps': 1,   # the canonical host is ours: its links' paths are read (follow-up F6, stamp)
    },
    'app/main/routes.py': {
        'modlog_shown_name': 1,   # a handle on SERVER_NAME is shown back as its bare name on the default storefront
        'test_hashing': 1,
    },
    'app/models.py': {
        'Community.local_url': 1,
        'Feed.local_url': 1,
        'File.delete_from_disk': 3,   # the CDN purge URLs of file_path, thumbnail_path and the converted JPEG
        'File.local_path_for_url': 2,
        'Post.generate_slug': 1,
        'Post.new': 1,
        'User.encode_jwt_token': 1,
        'is_local_url': 1,
        'media_local_path': 1,
    },
    'app/nntp/server.py': {
        'PieFedNNTPServer.__init__': 1,
    },
    'app/post/routes.py': {
        'post_cross_post': 1,
        'post_edit': 1,
        'post_move': 1,
        'post_share_mastodon': 1,
    },
    'app/relays/expiry.py': {
        '_local_user_ids': 1,
    },
    'app/relays/subscribe.py': {
        '_new_follow_id': 1,
        'remove_relay': 1,
        'send_follow_accept': 1,
    },
    'app/search/routes.py': {
        'run_search': 1,
    },
    'app/shared/community.py': {
        'add_mod_to_community': 1,
        'invite_with_chat': 9,
        'invite_with_email': 1,
    },
    'app/shared/feed.py': {
    },
    'app/shared/post.py': {
        'report_post': 1,
    },
    'app/shared/reply.py': {
        'report_reply': 1,
    },
    'app/shared/tasks/maintenance.py': {
        'add_remote_community_from_post': 1,
    },
    'app/shared/tasks/notes.py': {
        'send_reply': 2,
    },
    'app/shared/tasks/pages.py': {
        'send_post': 2,
    },
    'app/shared/user.py': {
        'bot_challenge_user': 1,
    },
    'app/storefront.py': {
        '_authority': 1,   # SERVER_NAME verbatim is the canonical host's authority; no other host may borrow its port
        '_local_hosts': 1,   # SERVER_NAME's host is one of ours, as in is_local_host: it names no URL
        '_local_only_hosts': 1,   # SERVER_NAME's host federates: it is never one a stored link is moved from
        '_synthesised_default': 1,
        'canonical_url': 1,
        'canonicalise_links': 1,   # the canonical host's own links on the canonical origin; embedded ones stay as written
        'check_default_domain': 2,
        'domain_authority': 1,   # an owned row's ap_domain on the default domain is SERVER_NAME verbatim
        'domain_base_url': 1,   # the default domain's identity origin is SERVER_URL itself
        'is_local_host': 1,
        'is_served_name': 1,   # SERVER_NAME as configured is a name this site answers on: it names no URL
    },
    'app/user/routes.py': {
        'export_user_settings': 4,
        'fediverse_redirect': 4,
        'show_people': 1,
    },
    'app/user/subscription.py': {
        'choose_plan': 3,
    },
    'app/visibility.py': {
        '_mentions': 1,
    },
}
