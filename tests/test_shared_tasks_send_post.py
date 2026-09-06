"""`send_post` -- the Celery-path builder and deliverer of an ActivityPub Page.

`app/shared/tasks/pages.py:88-368`. This is the second of two Page builders in
the codebase; the other is `post_to_page` (app/activitypub/util.py:132-219),
reached from the outbox collection view at app/activitypub/routes.py:2033. They
are near-twins and their disagreements are findings D298, D299 and D300.

ENTRY is a direct call. `send_post(post_id, edit=False, session=None)` has no
usable default for `session` -- :89 is `session.query(Post).get(post_id)` -- so
every test here passes `db.session` explicitly.

FOUR EARLY RETURNS stand between entry and the builder at :175, and a test that
wants to reach the builder must clear all four:

  :149-150  `if not community.instance.online(): return`
  :153-154  `if community.local_only or community.private: return`
  :156-158  a CommunityBan row for (user, community)
  :159-161  a remote community whose instance the user blocked, or that is banned

`Instance.online()` is `not (self.dormant or self.gone_forever)`, and both
columns default False (app/models.py:98, :100), so a factory instance is online
without help.

NOTE ON :153, because sub-project 18 relied on the opposite. Setting
`community.local_only = True` does NOT merely skip delivery -- it returns at
:154 before the builder runs at all. That is also why the false arms of :267
and :330 (`if not community.local_only:`) are UNREACHABLE: `community` is bound
once at :91 and never reassigned, so by :267 the flag is always falsy. Those two
arms are registered as unreachable rather than chased.

STOPPING BEFORE THE NETWORK. :336-338 is
`followers = ...; if not followers: return`. A post whose author has no inward
UserFollower rows ends the function there. Combined with a local community that
has no following_instances(), the entire builder runs with zero outbound
requests, and no `http_mock` is needed. Tests that DO reach delivery must give
the sender real keys (`make_user(..., with_keys=True)`), because signing calls
`.encode()` on the private key.

MENTIONS ARE SILENTLY SKIPPED. `search_for_user` (app/user/utils.py:85) is
called at :106 and :112, each inside a bare `except: pass` (:107-108, :113-114).
So a test asserting that a mention produced no notification cannot distinguish
"correctly skipped" from "crashed and swallowed" -- pin the reason, not the
absence.
"""

import pytest
from types import SimpleNamespace

from app import db
from app.constants import (
    NOTIF_MENTION, POST_TYPE_ARTICLE, POST_TYPE_EVENT, POST_TYPE_IMAGE,
    POST_TYPE_LINK, POST_TYPE_POLL, POST_TYPE_VIDEO,
)
from app.models import (
    CommunityBan, Event, File, Notification, Poll, PollChoice, User,
    UserFollower,
)
from app.shared.tasks.pages import send_post
from tests.factories import (
    make_community, make_community_member, make_instance, make_post, make_user,
)


def _seed(body=None, post_type=POST_TYPE_ARTICLE, url=None, local_community=True):
    """The local instance, a local author, a community, and a post.

    ORDER IS LOAD-BEARING. `make_community` hardcodes `instance_id=1`
    (tests/factories.py) and tests/conftest.py:143 truncates with
    RESTART IDENTITY, so whichever Instance is inserted first gets id 1. The
    local instance is created first here so the community's FK points at it. A
    peer built before this call would capture id 1 and silently make the
    community's instance the peer -- see `_peer` below.

    `local_community=False` gives the community an `ap_id`, which is what
    `Community.is_local()` tests, so :159's guard opens.
    """
    instance = make_instance('test.piefed.local', software='piefed')
    user = make_user(instance, 'author', local=True)
    community = make_community('c1')
    post = make_post(community, user, ap_id='https://test.piefed.local/post/1')
    post.type = post_type
    post.body = body
    post.url = url
    if not local_community:
        community.ap_id = 'c1@peer.example'
    db.session.commit()
    return SimpleNamespace(instance=instance, user=user, community=community,
                           post=post)


def _peer(domain='peer.example', software='lemmy'):
    """A remote Instance. ALWAYS call this AFTER `_seed()` -- see `_seed`'s
    docstring for why the order matters."""
    return make_instance(domain, software=software)


def _send(post, edit=False):
    """`send_post` takes an explicit session; there is no usable default."""
    return send_post(post.id, edit=edit, session=db.session)
