"""app/instance/routes.py -- the instance list, its people and posts pages, the
bulk follow importer, and the block pair.

MEASUREMENT BASIS. The module stood at 16.573% on the full-suite --cov=app run
at 5f4f8294b, carrying 189 missing statements and 108 missing arcs.

One defect is pinned here:

  P1  instance_unblock assigned the OPTIONAL HX-Current-Url header straight
      into HX-Redirect, so with the header absent werkzeug stringified None and
      htmx navigated to a page called /None -- after the unblock had been
      written. D756's family in a third module, and the only one of the three
      that fails silently.

Facts 292-294 and 314 apply: render_template is patched for anything that
renders, POST routes need a real CSRF token, the site fixture supplies g.site,
and a cookie needs the app's SERVER_NAME as its domain.
"""
import io

import pytest
from unittest.mock import patch

from flask import session
from flask_wtf.csrf import generate_csrf

from app import db
from app.constants import POST_STATUS_REVIEWING
from app.models import (AllowedInstances, BannedInstances, Instance, InstanceBlock, Post,
                        Site, User, utcnow)
from tests.factories import make_community, make_instance, make_post, make_user

pytestmark = pytest.mark.usefixtures('site')


def login(client, user):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def csrf(app, client):
    with app.test_request_context():
        token = generate_csrf()
        raw = session['csrf_token']
    with client.session_transaction() as sess:
        sess['csrf_token'] = raw
    return token


def _seed():
    """The local instance at id 1, the burnt id-1 user, and two locals.

    Instance 1 is the local one everywhere in this codebase, which several of
    these routes rely on -- `/instance/local/people` reads `Instance.query.get(1)`
    and the `federated` filter excludes it by id.
    """
    instance = make_instance('test.piefed.local', software='piefed')
    burn = make_user(instance, 'burnseat', local=True)
    assert burn.id == 1
    alice = make_user(instance, 'alice', local=True)
    bob = make_user(instance, 'bob', local=True)
    site = Site.query.get(1)
    site.private_instance = False
    db.session.commit()
    return instance, alice, bob


def _submitter(user):
    """instance_add_people sits behind validation_required and
    approval_required, so its user needs verified and a private_key.
    """
    user.verified = True
    user.private_key = 'a-private-key'
    db.session.commit()
    return user


def _make_admin(user):
    """Both notions of admin (fact 308)."""
    from app.constants import ROLE_ADMIN
    from app.models import Role, user_role
    role = Role.query.get(ROLE_ADMIN)
    if role is None:
        role = Role(id=ROLE_ADMIN, name='Admin', weight=0)
        db.session.add(role)
        db.session.commit()
    db.session.execute(user_role.insert().values(user_id=user.id, role_id=role.id))
    db.session.commit()
    return role


# --------------------------------------------------------------------------
# P1: the unblock's redirect header
# --------------------------------------------------------------------------


def test_unblocking_over_htmx_without_a_current_url_names_a_real_page(app, db_session):
    """Before the repair:

        PROBE f3 status: 200 HX-Redirect: 'None'

    -- werkzeug stringifies the None the absent header gives, and htmx then
    navigates to a relative url called `None`. The block was already undone by
    then, so the reader lands on a 404 having succeeded.
    """
    instance, alice, bob = _seed()
    peer = make_instance('remote.example', software='lemmy')
    db.session.add(InstanceBlock(user_id=alice.id, instance_id=peer.id))
    db.session.commit()
    client = app.test_client()
    login(client, alice)
    token = csrf(app, client)

    response = client.post(f'/instance/{peer.id}/unblock', data={'csrf_token': token},
                           headers={'HX-Request': 'true'})

    assert response.status_code == 200
    assert response.headers['HX-Redirect'] == f'/instance/{peer.domain}'
    assert InstanceBlock.query.count() == 0


def test_unblocking_over_htmx_returns_the_reader_where_they_were(app, db_session):
    """The true arm of the same guard: a current url that IS supplied comes
    back unchanged, which is what keeps the fallback from swallowing it.
    """
    instance, alice, bob = _seed()
    peer = make_instance('remote.example', software='lemmy')
    client = app.test_client()
    login(client, alice)
    token = csrf(app, client)

    response = client.post(f'/instance/{peer.id}/unblock', data={'csrf_token': token},
                           headers={'HX-Request': 'true',
                                    'HX-Current-Url': 'https://test.piefed.local/u/bob'})

    assert response.headers['HX-Redirect'] == 'https://test.piefed.local/u/bob'
