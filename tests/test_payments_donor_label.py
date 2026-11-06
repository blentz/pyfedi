"""The Donor label reads what core keeps, for every provider (M9, plan P1)."""
import subprocess
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import g
from sqlalchemy import event

from app import db
from app.constants import DONOR_WHILE_SUBSCRIBED
from app.models import Site, utcnow
from tests.factories import make_community, make_community_member, make_post, make_user

REPO = Path(__file__).parent.parent


@pytest.fixture
def env(app, api_baseline):
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    site = db.session.get(Site, 1)
    site.private_instance = False
    db.session.commit()
    author = make_user(api_baseline.instance_local, 'author', local=True)
    community = make_community('labelled')
    make_community_member(author, community)
    post = make_post(community, author, 'https://local.example/post/labels', title='About labels')
    return SimpleNamespace(client=app.test_client(), author=author, post=post, site=site, community=community)


@pytest.mark.parametrize('until, shown', [(None, False), ('past', False), ('future', True), ('sentinel', True)],
                         ids=['never', 'lapsed', 'one_off', 'subscribed'])
def test_is_donor(env, until, shown):
    env.author.donor_until = {None: None, 'past': utcnow() - timedelta(days=1), 'future': utcnow() + timedelta(days=1),
                              'sentinel': DONOR_WHILE_SUBSCRIBED}[until]
    assert env.author.is_donor() is shown


@pytest.mark.parametrize('theme', ['piefed', 'dillo'])
@pytest.mark.parametrize('donor', [True, False], ids=['donor', 'not_donor'])
def test_a_post_shows_it_in_each_theme(env, theme, donor):
    env.site.default_theme = theme
    env.author.donor_until = DONOR_WHILE_SUBSCRIBED if donor else None
    db.session.commit()
    body = env.client.get(f'/post/{env.post.id}').get_data(as_text=True)
    assert ('author_link donor' in body) is donor



def test_rendering_a_list_asks_the_database_nothing_more_for_a_donor(env):
    def queries():
        seen = []
        def record(conn, cursor, statement, *_):
            seen.append(statement)
        event.listen(db.engine, 'before_cursor_execute', record)
        try:
            env.client.get(f'/c/{env.community.name}')
        finally:
            event.remove(db.engine, 'before_cursor_execute', record)
        return len(seen)
    queries()   # anything cached once
    db.session.commit()   # both counts follow a commit, which makes the session reload what it holds
    before = queries()
    env.author.donor_until = DONOR_WHILE_SUBSCRIBED
    db.session.commit()
    assert queries() <= before


def test_no_template_reads_the_stripe_column_any_more():
    found = subprocess.run(['git', 'grep', '-l', 'stripe_subscription_id', '--', 'app/templates'], cwd=REPO,
                           capture_output=True, text=True).stdout
    assert found == ''
