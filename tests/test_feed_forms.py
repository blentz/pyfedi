"""app/feed/forms.py -- AddCopyFeedForm, EditFeedForm and SearchRemoteFeed.

MEASUREMENT BASIS. Before this file existed the module carried 20 missing
statements and 14 missing arcs on the full-suite --cov=app run at 97a56e713.

The forms are exercised with formdata inside a request context rather than
through a route: apply_feed_url_rules reads current_user, so the private-url
arms need someone logged in, and the route path would drag in everything
sub-projects 52-54 already cover.
"""
import pytest
from werkzeug.datastructures import MultiDict

from app import db
from app.feed.forms import AddCopyFeedForm, EditFeedForm, SearchRemoteFeed
from app.models import Community, User
from tests.factories import make_community, make_instance, make_local_feed, make_user, web_ctx

pytestmark = pytest.mark.usefixtures('site')


def _seed():
    instance = make_instance('test.piefed.local', software='piefed')
    burn = make_user(instance, 'burnseat', local=True)
    assert burn.id == 1
    owner = make_user(instance, 'feedowner', local=True)
    return instance, owner


def _form_data(**overrides):
    data = {'title': 'A feed', 'url': 'afeed', 'description': '',
            'communities': 'somecommunity@remote.example', 'public': 'y'}
    data.update(overrides)
    return MultiDict({k: v for k, v in data.items() if v is not None})


def test_the_create_form_rejects_an_absent_url_field(app, db_session):
    """Was a PIN; INVERTED once AddCopyFeedForm gained EditFeedForm's guard.

    ORIGINAL PINNED CLAIM, now false: ":30 calls .strip() on url.data, which is
    None whenever the input is absent or disabled -- not '' (fact 293)", so a
    POST without the field was a 500 on /feed/new and /feed/<id>/copy.

    EditFeedForm.validate has guarded this since before the campaign started;
    its twin did not. The error message is asserted, not merely the False:
    a guard that returned False silently would leave the user with a form that
    refuses and says nothing.
    """
    instance, owner = _seed()

    with web_ctx(app, owner):
        form = AddCopyFeedForm(formdata=_form_data(url=None))
        # parent_feed_id is a SelectField whose choices the ROUTES assign
        # (feeds_for_form); a form built directly has none, and wtforms raises
        # 'Choices cannot be None.' from super().validate() before the code
        # under test runs.
        form.parent_feed_id.choices = [(0, 'None')]
        assert form.validate() is False
        assert 'Url is required.' in [str(e) for e in form.url.errors]


def _validate(app, user, **overrides):
    """Build AddCopyFeedForm with formdata and run its validate().

    The SelectField's choices are assigned here for the reason the first test
    records: the routes assign them, and wtforms raises 'Choices cannot be
    None.' out of super().validate() without them.
    """
    with web_ctx(app, user):
        form = AddCopyFeedForm(formdata=_form_data(**overrides))
        form.parent_feed_id.choices = [(0, 'None')]
        valid = form.validate()
        return valid, form


def test_an_empty_url_is_refused(app, db_session):
    """:30-32's other half: the field is present and blank."""
    instance, owner = _seed()
    valid, form = _validate(app, owner, url='   ')
    assert valid is False
    assert 'Url is required.' in [str(e) for e in form.url.errors]


def test_a_url_the_rules_reject_is_refused(app, db_session):
    """:34-35. apply_feed_url_rules owns the character rules; this asserts the
    form honours its answer rather than re-implementing it."""
    instance, owner = _seed()
    valid, form = _validate(app, owner, url='has-a-hyphen')
    assert valid is False
    assert form.url.errors


def test_a_url_that_collides_with_a_local_community_is_refused(app, db_session):
    """:36-40. A LOCAL community only: ap_id must be None, so a remote
    community of the same name is not a collision -- which the second half of
    this test asserts, because without it the filter is free.
    """
    instance, owner = _seed()
    make_community(name='taken', host='test.piefed.local')
    remote = make_community(name='remotetaken', host='remote.example')
    remote.ap_id = 'remotetaken@remote.example'
    db.session.commit()

    valid, form = _validate(app, owner, url='taken')
    assert valid is False
    assert 'A community with this url already exists.' in [str(e) for e in form.url.errors]

    valid, form = _validate(app, owner, url='remotetaken')
    assert valid is True


@pytest.mark.parametrize('deleted, expected', [
    (False, 'This name is in use already.'),
    (True, 'This name was used in the past and cannot be reused.'),
])
def test_a_url_that_collides_with_a_user_is_refused(app, db_session, deleted, expected):
    """:41-48. Two messages, and the deleted one is the stricter claim: the
    name cannot be reused at all.

    The comparison is case-insensitive on both sides, so the url here differs
    in case from the user name -- without that, the func.lower() pair is free.
    """
    instance, owner = _seed()
    clash = make_user(instance, 'TakenName', local=True)
    clash.deleted = deleted
    db.session.commit()

    valid, form = _validate(app, owner, url='takenname')
    assert valid is False
    assert expected in [str(e) for e in form.url.errors]


def test_a_url_that_names_an_existing_feed_once_normalised_is_refused(app, db_session):
    """D681, fixed. The form compared the url as typed against `Feed.name`,
    but `make_feed` stores it slugified, so 'afeed_' passed validation and
    then collided with 'afeed' at the unique index: an IntegrityError 500.
    The form now checks the name the feed would actually be stored under."""
    instance, owner = _seed()
    make_local_feed('afeed', public=True)

    valid, form = _validate(app, owner, url='afeed_')
    assert valid is False
    assert 'A Feed with this url already exists.' in [str(e) for e in form.url.errors]


def test_communities_must_carry_a_host(app, db_session):
    """:50-58. Every non-blank line needs an '@'; blank lines are skipped by
    the `continue` at :53, which the second row exercises with a trailing
    newline the first would fail on."""
    instance, owner = _seed()

    valid, form = _validate(app, owner, communities='justaname')
    assert valid is False
    assert form.communities.errors

    valid, form = _validate(app, owner,
                            communities='one@remote.example\n\n  \ntwo@remote.example\n')
    assert valid is True


def test_a_private_feed_url_gets_the_owner_appended_by_the_rules(app, db_session):
    """apply_feed_url_rules rewrites url.data in place for a private feed, and
    the form is where that happens -- which is why feed_copy's own append made
    the suffix appear twice (D716)."""
    instance, owner = _seed()

    valid, form = _validate(app, owner, url='privateone', public=None)
    assert valid is True
    assert form.url.data == f'privateone/{owner.user_name.lower()}'


def test_the_create_form_refuses_a_missing_title(app, db_session):
    """:28-29, super().validate()'s False arm. title carries DataRequired, so
    a form without it never reaches this class's own checks -- which is what
    the arm is for."""
    instance, owner = _seed()
    valid, form = _validate(app, owner, title=None)
    assert valid is False
    assert form.title.errors
    assert not form.url.errors


def _validate_edit(app, user, **overrides):
    """EditFeedForm's twin of _validate. Its `public` field has no default,
    unlike AddCopyFeedForm's, so the rows below pass it explicitly."""
    with web_ctx(app, user):
        form = EditFeedForm(formdata=_form_data(**overrides))
        form.parent_feed_id.choices = [(0, 'None')]
        valid = form.validate()
        return valid, form


def test_the_edit_form_accepts_an_absent_url(app, db_session):
    """:85's guard, and the reason it exists: the route disables the url input
    once a feed has subscribers (app/feed/routes.py:157-158), so the field is
    absent from the POST and url.data is None.

    This is the guard AddCopyFeedForm was missing (D733), asserted here as the
    behaviour the other form now matches.
    """
    instance, owner = _seed()
    valid, form = _validate_edit(app, owner, url=None)
    assert valid is True
    assert not form.url.errors


def test_the_edit_form_refuses_a_blank_url(app, db_session):
    """:86-88. Present and blank is a different case from absent. D740, fixed
    (owner ruling): it gets the same message as AddCopyFeedForm's, 'Url is
    required.', where the edit form used to say 'This field is required.'."""
    instance, owner = _seed()
    valid, form = _validate_edit(app, owner, url='  ')
    assert valid is False
    assert [str(e) for e in form.url.errors] == ['Url is required.']


def test_the_edit_form_applies_the_url_rules(app, db_session):
    """:90-91."""
    instance, owner = _seed()
    valid, form = _validate_edit(app, owner, url='has-a-hyphen')
    assert valid is False
    assert form.url.errors


def test_the_edit_form_refuses_a_missing_title(app, db_session):
    """:83-84, super().validate()'s False arm in this twin."""
    instance, owner = _seed()
    valid, form = _validate_edit(app, owner, title=None)
    assert valid is False
    assert form.title.errors


def test_the_edit_form_checks_every_community_line(app, db_session):
    """:93-101, the same loop AddCopyFeedForm carries, in its own copy -- blank
    lines skipped, a line without a host refused."""
    instance, owner = _seed()

    valid, form = _validate_edit(app, owner, communities='justaname')
    assert valid is False
    assert form.communities.errors

    valid, form = _validate_edit(app, owner,
                                 communities='one@remote.example\n\n  \ntwo@remote.example\n')
    assert valid is True
