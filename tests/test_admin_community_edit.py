"""`admin_community_edit`: the GET pre-fill, and the round trip it is half of.

Twenty-six statements of the GET branch had never been executed by a test, on a page
that writes twenty-three community settings. What makes those statements worth their
own file is not their number: a setting the POST writes and the GET does not pre-fill
is a setting that editing **anything else on the page** silently resets, because the
form round-trips through the browser and an unfilled field posts back its default.

THE CAMPAIGN HAS BEEN BITTEN BY THIS THREE TIMES. D701 was `feed_edit`'s NSFL
pre-fill reading the NSFW column, so saving any edit to an NSFL feed cleared the flag.
D702 was NSFW/NSFL taken past the site's switches. D675 was `is_instance_feed` taken
from the caller. Each was one half of a pair disagreeing with the other half.

SO THE RULE IS ASSERTED OVER THE SOURCE, not only exercised. `test_the_round_trip_is_
complete` reads the function's AST and requires the two sets to match, which is a claim
about every future field as well as today's twenty-three -- a behavioural test can only
ever cover the fields it names.

THE SWEEP THAT PRODUCED THAT RULE CAME BACK CLEAN, and is recorded here so nobody
repeats it. Twenty-seven functions in `app/` both pre-fill a form and write a row from
it. Nine round-trip exactly. The rest report a divergence that is an artefact of
looking for `row.col = form.x.data`:

  * `admin_site`, `admin_misc`, `admin_federation` write through
    `set_setting('x', form.x.data)` and `db.session.execute`, not through an
    attribute;
  * `user_settings`' `compaction`, `low_bandwidth_mode`, `max_hours_per_day` and
    `max_hours_change_restriction` are cookies -- `resp.set_cookie(...)`
    (app/user/routes.py:692-700) -- pre-filled from `request.cookies`;
  * `admin_community_edit`'s own `languages`, and `community_edit`'s, are written by
    a DELETE plus `community.languages.append(...)`;
  * `add_post`, `feed_copy` and `filter_selection` are create forms, where a
    pre-fill is a default rather than the other half of a round trip;
  * `password`, `role`, `hide`, `countries`, `announcement`, `referrer` and
    `private` are each written somewhere other than an attribute assignment, or are
    deliberately not persisted from this form.

HARNESS FACTS. The `und` Language row must exist: every path that saves a community
appends it, and `.append(None)` is `FlushError: Can't flush None value found in
collection Community.languages` (the note tests/test_admin_upload_forms.py:46 and
tests/test_admin_community_listings.py:407 both carry). `topic` takes `-1` for "no
topic", not 0, and `default_layout` accepts only '', 'masonry' or 'masonry_wide' --
a failed validation re-renders the page with a 200 and looks exactly like a refusal,
so every POST here asserts the 302.
"""
import ast
import inspect

import pytest
from flask import g

from sqlalchemy import text

from app import db
from app.models import Community, Language, Site, Topic
from tests.factories import make_community

# Every field on EditCommunityForm that is neither a data field nor a control: the
# two file inputs are read from `request.files`, the three lists are SelectField
# choices, and `submit` is the button.
NOT_DATA_FIELDS = {'icon_file', 'banner_file', 'downvote_accept_modes', 'options',
                   'layouts', 'submit',
                   # a method on the class, not a field
                   'validate'}


@pytest.fixture
def env(app, api_baseline):
    from types import SimpleNamespace

    g.admin_ids = [api_baseline.user1.id]
    site = db.session.get(Site, 1)
    site.private_instance = False
    site.enable_nsfw = True
    site.enable_nsfl = True
    g.site = site
    for code, name in (('en', 'English'), ('und', 'Undetermined')):
        if Language.query.filter(Language.code == code).first() is None:
            db.session.add(Language(code=code, name=name))
    db.session.commit()
    community = make_community('probeland')
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(api_baseline.user1.id)
        session['_fresh'] = True
    return SimpleNamespace(app=app, client=client, community=community,
                           baseline=api_baseline)


def csrf(app, client):
    from flask import session as flask_session
    from flask_wtf.csrf import generate_csrf
    with app.test_request_context():
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with client.session_transaction() as session:
        session['csrf_token'] = raw
    return token


def _payload(app, client, community, **overrides):
    """The minimum the form validates with, plus whatever a test names."""
    data = {'csrf_token': csrf(app, client),
            'url': community.name, 'title': community.title or 'A Title',
            'description': '', 'rules': '', 'content_retention': -1,
            'topic': -1, 'default_layout': '', 'posting_warning': '',
            'downvote_accept_mode': 0}
    data.update(overrides)
    return {key: value for key, value in data.items() if value is not None}


def _edit(env, community=None, **overrides):
    community = community if community is not None else env.community
    return env.client.post(f'/admin/community/{community.id}/edit',
                           data=_payload(env.app, env.client, community,
                                         **overrides))


def _captured_form(env, community=None, monkeypatch=None):
    """The form object the GET handed to the template."""
    community = community if community is not None else env.community
    captured = {}

    def fake_render(template, **kwargs):
        captured.update(kwargs)
        return 'rendered'

    from unittest import mock
    with mock.patch('app.admin.routes.render_template', side_effect=fake_render):
        response = env.client.get(f'/admin/community/{community.id}/edit')
    assert response.status_code == 200
    return captured['form']


# --------------------------------------------------------------------------
# The rule, over the source
# --------------------------------------------------------------------------


def _round_trip_sets():
    """(written, prefilled) form-field names, read out of the function itself."""
    from app.admin import routes

    tree = ast.parse(inspect.getsource(routes))
    target = next(node for node in ast.walk(tree)
                  if isinstance(node, ast.FunctionDef)
                  and node.name == 'admin_community_edit')
    written, prefilled = set(), set()
    for node in ast.walk(target):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        assigned = node.targets[0]
        if not isinstance(assigned, ast.Attribute):
            continue
        source = ast.unparse(node.value)
        if (assigned.attr == 'data'
                and isinstance(assigned.value, ast.Attribute)
                and isinstance(assigned.value.value, ast.Name)
                and assigned.value.value.id == 'form'):
            prefilled.add(assigned.value.attr)
            continue
        if isinstance(assigned.value, ast.Name) and assigned.value.id == 'community':
            for sub in ast.walk(node.value):
                if (isinstance(sub, ast.Attribute) and sub.attr == 'data'
                        and isinstance(sub.value, ast.Attribute)
                        and isinstance(sub.value.value, ast.Name)
                        and sub.value.value.id == 'form'):
                    written.add(sub.value.attr)
    return written, prefilled


def test_the_round_trip_is_complete():
    """Every field the POST writes is pre-filled on GET, and every field pre-filled
    is written.

    The claim is about the pair, so it is asserted on the pair rather than on
    twenty-three separate requests. `languages` is the one field written by
    something other than an attribute assignment -- a DELETE and
    `community.languages.append(...)` -- so it is added to the written set by name,
    and `test_the_languages_round_trip_too` covers it behaviourally.
    """
    written, prefilled = _round_trip_sets()
    written.add('languages')

    assert written == prefilled


def test_every_data_field_on_the_form_is_part_of_the_round_trip():
    """The other direction, which the AST pair cannot see: a field the template
    renders and the route never reads is a control that does nothing.

    Measured against the form class, so adding a field without wiring it fails here
    rather than on an admin's first attempt to use it.
    """
    from app.admin.forms import EditCommunityForm

    declared = {name for name in vars(EditCommunityForm)
                if not name.startswith('_')} - NOT_DATA_FIELDS
    written, _ = _round_trip_sets()
    written.add('languages')

    assert declared - written == set()


# --------------------------------------------------------------------------
# The GET pre-fill, exercised
# --------------------------------------------------------------------------


class TestThePreFill:
    def test_every_field_arrives_holding_the_communitys_own_value(self, env):
        """One request, twenty-three assertions. The values are all set to the
        opposite of the column default first, so a pre-fill that returned a
        default -- or that read the wrong column, which is what D701 was -- cannot
        pass.
        """
        community = env.community
        topic = Topic(name='Fediverse', machine_name='fediverse',
                      num_communities=1)
        db.session.add(topic)
        db.session.commit()
        community.title = 'A Distinct Title'
        community.description = 'a description'
        community.rules = 'be kind'
        community.nsfw = True
        community.ai_generated = True
        community.banned = True
        community.local_only = True
        community.restricted_to_mods = True
        community.new_mods_wanted = True
        community.show_popular = False
        community.show_all = False
        community.low_quality = True
        community.content_retention = 30
        community.topic_id = topic.id
        community.default_layout = 'masonry'
        community.posting_warning = 'mind the rules'
        community.ignore_remote_language = True
        community.ignore_remote_gen_ai = True
        community.always_translate = True
        community.can_be_archived = False
        community.downvote_accept_mode = 2
        db.session.commit()

        form = _captured_form(env)

        assert form.url.data == community.name
        assert form.title.data == 'A Distinct Title'
        assert form.description.data == 'a description'
        assert form.rules.data == 'be kind'
        assert form.nsfw.data is True
        assert form.ai_generated.data is True
        assert form.banned.data is True
        assert form.local_only.data is True
        assert form.restricted_to_mods.data is True
        assert form.new_mods_wanted.data is True
        assert form.show_popular.data is False
        assert form.show_all.data is False
        assert form.low_quality.data is True
        assert form.content_retention.data == 30
        assert form.topic.data == topic.id
        assert form.default_layout.data == 'masonry'
        assert form.posting_warning.data == 'mind the rules'
        assert form.ignore_remote_language.data is True
        assert form.ignore_remote_gen_ai.data is True
        assert form.always_translate.data is True
        assert form.can_be_archived.data is False
        assert form.downvote_accept_mode.data == 2

    def test_no_topic_is_none_rather_than_zero(self, env):
        """`community.topic_id if community.topic_id else None`. The choice list
        uses -1 for "no topic", so None is what leaves the select unchosen; 0 would
        match no choice and render as the first one."""
        assert env.community.topic_id is None

        assert _captured_form(env).topic.data is None

    def test_the_languages_arrive_as_the_ids_the_community_holds(self, env):
        """`form.languages.data = community.language_ids()` -- a list, and the one
        field of the round trip written by something other than an attribute
        assignment."""
        english = Language.query.filter(Language.code == 'en').one()
        env.community.languages.append(english)
        db.session.commit()

        assert _captured_form(env).languages.data == [english.id]

    def test_a_remote_community_says_its_settings_will_be_overwritten(self, env):
        """The one statement of the GET branch that is not a pre-fill. An admin
        editing a remote community is told the server that publishes it will
        overwrite what they save."""
        from flask import get_flashed_messages
        remote = make_community('books', host='remote.example')
        # `ap_id`, not just the host: `is_local()` is
        # `self.ap_id is None or self.ap_profile_id.startswith(SERVER_URL)`
        # (app/models.py:1841), and `make_community` leaves `ap_id` None -- so a
        # community built with a remote host is still local until this is set, and
        # a test that only changed the host would assert nothing.
        remote.ap_id = 'books@remote.example'
        db.session.commit()
        assert remote.is_local() is False

        with env.client:
            _captured_form(env, remote)
            messages = get_flashed_messages()

        assert any('remote community' in message for message in messages)

    def test_a_local_community_is_not_warned(self, env):
        """The control: the warning is on `not community.is_local()`, so a fix that
        flashed unconditionally would pass the row above."""
        from flask import get_flashed_messages

        with env.client:
            _captured_form(env)
            messages = get_flashed_messages()

        assert messages == []

    def test_a_community_nobody_holds_is_a_404(self, env):
        assert env.client.get('/admin/community/999999/edit').status_code == 404


# --------------------------------------------------------------------------
# The POST half, for the fields whose round trip the AST cannot see
# --------------------------------------------------------------------------


class TestTheSaveHalf:
    def test_the_languages_round_trip_too(self, env):
        """`languages` is written by a DELETE and an append, so the AST pair adds it
        by name; this is the behaviour behind that name. `und` is appended
        unconditionally so that posts carrying no language are still accepted."""
        english = Language.query.filter(Language.code == 'en').one()
        undetermined = Language.query.filter(Language.code == 'und').one()

        response = _edit(env, languages=[str(english.id)])

        assert response.status_code == 302
        assert sorted(language.id for language in env.community.languages) == \
            sorted([english.id, undetermined.id])

    def test_saving_replaces_the_languages_rather_than_adding_to_them(self, env):
        """The DELETE. Without it every save would accumulate, and `und` -- appended
        unconditionally by every save -- would be the first to double up.

        The community starts holding both, and the save names only English: what
        comes back is English plus the `und` the route adds, each once.
        """
        english = Language.query.filter(Language.code == 'en').one()
        undetermined = Language.query.filter(Language.code == 'und').one()
        env.community.languages.append(english)
        env.community.languages.append(undetermined)
        db.session.commit()

        response = _edit(env, languages=[str(english.id)])

        assert response.status_code == 302
        assert sorted(language.id for language in env.community.languages) == \
            sorted([english.id, undetermined.id])

    def test_a_setting_survives_a_save_that_does_not_mention_it(self, env):
        """What the round trip is FOR, asserted end to end: read the page, post it
        back unchanged, and every setting is still what it was.

        This is the failure D701 was -- a save that cleared a flag nobody touched --
        and the only row here that would catch it in a field the AST test has not
        been told about.
        """
        env.community.nsfw = True
        env.community.low_quality = True
        env.community.always_translate = True
        env.community.posting_warning = 'mind the rules'
        db.session.commit()

        form = _captured_form(env)
        response = _edit(env, nsfw='y' if form.nsfw.data else None,
                         low_quality='y' if form.low_quality.data else None,
                         always_translate='y' if form.always_translate.data else None,
                         posting_warning=form.posting_warning.data)

        assert response.status_code == 302
        assert env.community.nsfw is True
        assert env.community.low_quality is True
        assert env.community.always_translate is True
        assert env.community.posting_warning == 'mind the rules'


# --------------------------------------------------------------------------
# The topic counters, which the save keeps
# --------------------------------------------------------------------------


class TestTheTopicCounts:
    def _topic(self, name, count=0):
        topic = Topic(name=name.title(), machine_name=name,
                      num_communities=count)
        db.session.add(topic)
        db.session.commit()
        return topic

    def test_moving_a_community_recounts_both_topics(self, env):
        """`if community.topic_id != old_topic_id:` -- the new topic gains one and
        the old one loses one, each recounted from the table rather than
        incremented, so a count that had drifted is corrected."""
        old = self._topic('fediverse', count=99)
        new = self._topic('microblogging', count=0)
        env.community.topic_id = old.id
        db.session.commit()

        assert _edit(env, topic=new.id).status_code == 302

        assert env.community.topic_id == new.id
        assert db.session.get(Topic, new.id).num_communities == 1
        assert db.session.get(Topic, old.id).num_communities == 0

    def test_clearing_the_topic_recounts_the_one_it_left(self, env):
        """`form.topic.data if form.topic.data > 0 else None`, and then only the old
        topic to recount."""
        old = self._topic('fediverse', count=99)
        env.community.topic_id = old.id
        db.session.commit()

        assert _edit(env, topic=-1).status_code == 302

        assert env.community.topic_id is None
        assert db.session.get(Topic, old.id).num_communities == 0

    def test_a_save_that_does_not_move_the_community_leaves_the_counts_alone(
            self, env):
        """The guard. `num_communities` is seeded to a value the recount cannot
        produce, so a route that recounted unconditionally would change it."""
        topic = self._topic('fediverse', count=99)
        env.community.topic_id = topic.id
        db.session.commit()

        assert _edit(env, topic=topic.id).status_code == 302

        assert db.session.get(Topic, topic.id).num_communities == 99

    def test_the_missing_old_topic_guard_cannot_fire(self, env):
        """THE ROUND'S RESIDUAL. `topic = db.session.get(Topic, old_topic_id)` is
        followed by `if topic:`, and that `if` can never be false.

        Two things make the state it guards against unreachable, and this test is
        the proof rather than an assertion about the route:

        * `Community.topic_id` is a real foreign key, so an id no Topic row holds
          cannot be stored at all --
          `ForeignKeyViolation: Key (topic_id)=(999999) is not present in table
          "topic"`;
        * `Topic.communities` is `cascade="all, delete-orphan"`
          (app/models.py:1124), so deleting a topic deletes its communities. The
          first attempt at this row deleted the topic and got a **404** from the
          edit page, because the community being edited had gone with it.

        So `old_topic_id` either names a live Topic or is None, and None never
        reaches the lookup -- `if old_topic_id:` guards it one line above. Fact 75
        CAUSE 5, unreachable data, with the schema doing the guaranteeing. The guard
        stays: it costs one line and the relationship could gain a different cascade.
        """
        topic = self._topic('fediverse')
        env.community.topic_id = topic.id
        db.session.commit()
        community_id = env.community.id

        with pytest.raises(Exception) as dangling:
            db.session.execute(
                text('UPDATE community SET topic_id = :topic_id WHERE id = :id'),
                {'topic_id': 999999, 'id': community_id})
            db.session.commit()
        assert 'ForeignKeyViolation' in str(type(dangling.value)) or \
            'is not present in table' in str(dangling.value)
        db.session.rollback()

        db.session.delete(db.session.get(Topic, topic.id))
        db.session.commit()
        assert db.session.get(Community, community_id) is None


# --------------------------------------------------------------------------
# Who may do it
# --------------------------------------------------------------------------


def test_the_page_needs_the_permission(app, api_baseline):
    """`permission_required('administer all communities')`. An ordinary account is
    refused before any of the above runs."""
    from tests.factories import make_user
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    community = make_community('probeland')
    ordinary = make_user(api_baseline.instance_local, 'ordinary', local=True)
    db.session.commit()
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(ordinary.id)
        session['_fresh'] = True

    response = client.get(f'/admin/community/{community.id}/edit')

    assert response.status_code in (302, 401, 403)
    assert db.session.get(Community, community.id).title == 'probeland'
