"""app/chat/forms.py.

MEASUREMENT BASIS. Before this file existed the module stood at 55.556 on the
full-suite --cov=app run, its whole residual being
`ReportConversationForm.reasons_to_string` -- 6 missing statements and 6 missing
arcs (forms.py:33-39).

The function is a nested loop over the submitted ids and the form's own choice
list, so a single-reason test leaves the inner loop's non-matching pass
untested.
"""
import pytest

from app.chat.forms import ReportConversationForm

pytestmark = pytest.mark.usefixtures('site')


def _form(app):
    with app.test_request_context():
        return ReportConversationForm()


def test_one_reason_becomes_its_label(app):
    form = _form(app)

    assert form.reasons_to_string(['7']) == 'Spam'


def test_several_reasons_follow_the_order_they_were_given(app):
    """The submitted ids are the OUTER loop, so the result follows their order
    and not the form's. Given in the form's own order this assertion would pass
    either way, which is why they are given backwards.
    """
    form = _form(app)

    assert form.reasons_to_string(['14', '2']) == 'Other, Harassment'


def test_an_id_that_matches_no_choice_contributes_nothing(app):
    """The inner loop's non-matching pass: without a row like this, the
    comparison at forms.py:36 could be anything at all.
    """
    form = _form(app)

    assert form.reasons_to_string(['7', '999']) == 'Spam'
    assert form.reasons_to_string(['999']) == ''


def test_no_reasons_at_all_is_an_empty_string(app):
    form = _form(app)

    assert form.reasons_to_string([]) == ''


def test_the_joined_string_is_truncated_to_255_characters(app):
    """The column is String(256) and the function slices at 255 (forms.py:39),
    so the whole choice list -- which joins to more than that -- is what proves
    the slice is there.
    """
    form = _form(app)
    every_id = [choice[0] for choice in ReportConversationForm.reason_choices]

    result = form.reasons_to_string(every_id)

    assert len(result) == 255
    assert result.startswith('Spam, Harassment')
