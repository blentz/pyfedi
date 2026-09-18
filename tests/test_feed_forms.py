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
from tests.factories import make_community, make_instance, make_user, web_ctx

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


def test_the_create_form_crashes_when_the_url_field_is_absent(app, db_session):
    """PIN (P1): :30 calls .strip() on url.data, which is None whenever the
    input is absent or disabled -- not '' (fact 293).

    EditFeedForm.validate:82 guards exactly this with `if self.url.data is not
    None:`; AddCopyFeedForm does not, so a POST without the field is a 500 on
    /feed/new and /feed/<id>/copy.
    """
    instance, owner = _seed()

    with web_ctx(app, owner):
        form = AddCopyFeedForm(formdata=_form_data(url=None))
        # parent_feed_id is a SelectField whose choices the ROUTES assign
        # (feeds_for_form); a form built directly has none, and wtforms raises
        # 'Choices cannot be None.' from super().validate() before the code
        # under test runs.
        form.parent_feed_id.choices = [(0, 'None')]
        with pytest.raises(AttributeError, match='strip'):
            form.validate()
