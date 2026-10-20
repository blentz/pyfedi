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
        'fetch_remote_actor_data': 1,
        'find_actor_by_url': 2,
    },
    'app/activitypub/routes.py': {
        'activities_json': 1,
        'announce_activity_to_followers': 1,
        'api_v1_instance': 1,
        'comment_ap': 1,
        'community_featured': 1,
        'community_followers': 1,
        'community_following': 1,
        'community_moderators_route': 1,
        'community_outbox': 1,
        'community_profile': 1,
        'feed_followers': 1,
        'feed_moderators_route': 1,
        'feed_profile': 5,
        'host_meta': 1,
        'host_meta_json': 1,
        'nodeinfo': 2,
        'post_ap': 2,
        'private_message_ap': 1,
        'process_inbox_request': 7,
        'process_remove': 3,
        'process_webfinger_request': 8,
        'quote_boost_auth': 2,
        'replay_inbox_request': 1,
        'rerun_parked_request': 1,
        'shared_inbox': 1,
        'user_profile': 6,
    },
    'app/activitypub/serializers/microblog.py': {
        '_hashtag_href': 1,
        'microblog_note': 1,
    },
    'app/activitypub/serializers/peertube.py': {
        'peertube_video': 1,
    },
    'app/activitypub/signature.py': {
        'HttpSignature.signed_request': 1,
        '_check_signed_host': 1,
        '_fetch_actor_key': 1,
        '_target_uri': 1,
    },
    'app/activitypub/util.py': {
        '_origin_refusal_of_delete': 1,
        'comment_count_collections': 1,
        'comment_model_to_json': 1,
        'community_actor_data': 4,
        'create_activity_of': 1,
        'create_post_reply': 2,
        'fetch_object_capped': 1,
        'lemmy_site_data': 4,
        'post_count_collections': 1,
        'post_to_activity': 1,
        'post_to_page': 2,
        'proactively_delete_content': 2,
        'process_announce_of_uri': 1,
        'process_quote_boost': 2,
        'process_report': 2,
        'refresh_community_profile_task': 1,
        'refresh_feed_profile_task': 1,
        'refresh_user_profile_task': 1,
        'served_by_us': 1,
        'update_post_from_activity': 3,
        'update_post_reply_from_activity': 3,
    },
    'app/admin/routes.py': {
        'admin_community_edit': 2,
        'admin_community_move': 6,
        'admin_federation_ban_lists': 1,
        'admin_topics_export': 1,
    },
    'app/admin/util.py': {
        'move_community_images_to_here': 4,
        'unsubscribe_from_community': 2,
        'unsubscribe_from_everything_then_delete_task': 1,
    },
    'app/api/alpha/utils/community.py': {
        'get_community': 1,
    },
    'app/api/alpha/utils/misc.py': {
        'get_resolve_object': 5,
    },
    'app/api/alpha/utils/post.py': {
        'get_post_list': 2,
        'get_post_list2': 2,
    },
    'app/api/alpha/utils/reply.py': {
        'get_reply_list': 1,
    },
    'app/api/alpha/utils/user.py': {
        'get_user': 1,
    },
    'app/api/alpha/views.py': {
        '_neutral_post': 1,
        'private_message_view': 1,
        'reply_removal_ack_view': 1,
        'site_view': 1,
    },
    'app/auth/onboarding.py': {
        'send_community_follow': 1,
    },
    'app/auth/util.py': {
        'find_user_lookups': 1,
    },
    'app/chat/util.py': {
        'send_message': 2,
        'update_message': 1,
    },
    'app/cli.py': {
        'register.config_check': 2,
        'register.init_db': 4,
        'register.lemmy_import': 1,
        'register.move_more_post_images_to_s3': 1,
        'register.process_email_bounces': 4,
        'register.rss_feeds': 4,
        'register.send_batched_activities': 1,
    },
    'app/community/routes.py': {
        'community_invite': 1,
        'community_move': 1,   # the community handle shown on the move page names the federation domain
        'do_subscribe': 1,
        'join_then_add': 1,
        'unsubscribe': 3,
    },
    'app/community/server_view_list.py': {
        'server_view_select': 1,
    },
    'app/community/util.py': {
        'delete_post_from_community_task': 2,
        'delete_post_reply_from_community_task': 2,
        'search_for_community': 1,
    },
    'app/dev/routes.py': {
        'tools': 4,
    },
    'app/discovery/credits.py': {
        '_known_user': 1,
        '_signed_headers': 1,
        'verified_credit_user': 1,
    },
    'app/discovery/instance_actor.py': {
        'follow_activity': 1,
        'send_instance_undo': 1,
    },
    'app/discovery/instance_answers.py': {
        'instance_actor_url': 1,
    },
    'app/feed/routes.py': {
        'feed_copy': 2,
        'feed_unsubscribe': 3,
    },
    'app/feed/util.py': {
        'feed_communities_for_edit': 1,
        'search_for_feed': 1,
    },
    'app/main/routes.py': {
        'activitypub_application': 6,
        'instance_actor': 9,
        'instance_actor_outbox': 1,
        'list_feeds': 1,
        'modlog': 2,
        'test_hashing': 1,
    },
    'app/mentions.py': {
        'mentioned_users_found_by': 1,
    },
    'app/models.py': {
        'Community.flair_for_ap': 1,
        'Community.lemmy_link': 1,
        'Community.local_url': 1,
        'Community.profile_id': 1,
        'Community.public_url': 1,
        'Feed.lemmy_link': 1,
        'Feed.local_url': 1,
        'Feed.profile_id': 1,
        'Feed.public_url': 1,
        'File.delete_from_disk': 2,
        'File.local_path_for_url': 2,
        'Post.generate_ap_id': 4,
        'Post.generate_slug': 1,
        'Post.new': 2,
        'Post.profile_id': 1,
        'Post.tags_for_activitypub': 2,
        'PostReply.profile_id': 1,
        'User.encode_jwt_token': 1,
        'User.instance_domain': 1,
        'User.lemmy_link': 1,
        'User.mention_tag': 1,
        'User.profile_id': 1,
        'User.public_url': 1,
        'is_local_url': 1,
        'media_local_path': 1,
    },
    'app/nntp/server.py': {
        'PieFedNNTPServer.__init__': 1,
    },
    'app/onion_media_fill.py': {
        '_fetch': 1,      # the User-Agent of a media fetch: the same instance identity get_request sends
    },
    'app/post/routes.py': {
        'post_cross_post': 1,
        'post_edit': 1,
        'post_move': 1,
        'post_reply_restore': 3,
        'post_share_mastodon': 1,
        'show_post': 1,
    },
    'app/quotes.py': {
        'resolve_quotes': 1,
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
        '_feed_remove_community': 3,
        'announce_feed_add_remove_to_subscribers': 2,
        'announce_feed_delete_to_subscribers': 1,
        'edit_feed': 2,
        'form_communities_to_ids': 1,
        'join_feed': 1,
        'make_feed': 2,
    },
    'app/shared/media.py': {
        'store_uploaded_media': 1,
    },
    'app/shared/post.py': {
        'edit_post': 1,
        'report_post': 1,
    },
    'app/shared/reply.py': {
        'report_reply': 1,
    },
    'app/shared/tasks/adds.py': {
        'add_object': 2,
    },
    'app/shared/tasks/blocks.py': {
        'ban_person': 4,
    },
    'app/shared/tasks/deletes.py': {
        '_video_create': 1,
        'delete_message': 2,
        'delete_object': 3,
    },
    'app/shared/tasks/flags.py': {
        'report_chat': 1,
        'report_object': 1,
    },
    'app/shared/tasks/follows.py': {
        'follow_user': 1,
        'join_community': 1,
        'leave_community': 2,
        'leave_feed': 2,
        'unfollow_user': 3,
    },
    'app/shared/tasks/groups.py': {
        'edit_community': 6,
    },
    'app/shared/tasks/likes.py': {
        'send_vote': 3,
        'vote_for_poll': 2,
    },
    'app/shared/tasks/locks.py': {
        'lock_object': 3,
    },
    'app/shared/tasks/maintenance.py': {
        'add_remote_community_from_post': 1,
    },
    'app/shared/tasks/media.py': {
        '_thumbnail': 1,
    },
    'app/shared/tasks/notes.py': {
        'send_answer': 3,
        'send_reply': 5,
    },
    'app/shared/tasks/pages.py': {
        'move_object': 2,
        'send_microblog_note': 2,
        'send_peertube_video': 2,
        'send_post': 7,
    },
    'app/shared/tasks/removes.py': {
        'remove_object': 2,
    },
    'app/shared/upload.py': {
        'process_upload': 1,
    },
    'app/shared/user.py': {
        'bot_challenge_user': 1,
    },
    'app/storefront.py': {
        '_authority': 1,   # SERVER_NAME verbatim is the canonical host's authority; no other host may borrow its port
        '_local_hosts': 1,   # SERVER_NAME's host is one of ours, as in is_local_host: it names no URL
        '_synthesised_default': 1,
        'canonical_url': 1,
        'canonicalise_links': 1,   # as canonicalise_text: the embedded URLs of SERVER_NAME's host stay as written
        'canonicalise_text': 1,   # SERVER_NAME's host is left as written in text: only the other hosts move
        'check_default_domain': 2,
        'domain_authority': 1,   # an owned row's ap_domain on the default domain is SERVER_NAME verbatim
        'domain_base_url': 1,   # the default domain's identity origin is SERVER_URL itself
        'is_local_host': 1,
        'is_served_name': 1,   # SERVER_NAME as configured is a name this site answers on: it names no URL
    },
    'app/user/routes.py': {
        'ban_profile': 1,
        'block_profile': 1,
        'delete_profile': 1,
        'export_user_settings': 4,
        'fediverse_redirect': 4,
        'import_settings_task': 1,
        'report_profile': 1,
        'send_deletion_requests': 1,
        'show_people': 1,
        'show_profile_rss': 1,
        'unban_profile': 1,
        'unblock_profile': 1,
        'user_block_instance': 1,
        'user_feeds': 1,
        'user_follow_request_accept': 2,
        'user_follow_request_reject': 2,
        'user_settings_block_instance': 1,
        'user_upvotes': 1,
    },
    'app/user/subscription.py': {
        'choose_plan': 3,
    },
    'app/user/utils.py': {
        'search_for_user': 1,
        'unsubscribe_from_community': 2,
    },
    'app/utils.py': {
        'get_request': 2,
        'get_request_capped': 1,
    },
    'app/visibility.py': {
        '_mentions': 1,
    },
}
