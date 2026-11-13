"""What a web form reads from a request. Flask-WTF fills a submitted form from a JSON body when there is no form data,
through ImmutableMultiDict, and a body that is JSON but not an object (a number, a list, a string, null) raised
TypeError there: a 500 from every form of the site (fuzz-routes finding, 2026-11-13). Such a body is a malformed
submission, a 400; a JSON object still fills the form as Flask-WTF fills it."""
from flask import abort, request
from flask_wtf.form import FlaskForm, _Auto, _is_submitted

_flask_wtf_wrap_formdata = FlaskForm.Meta.wrap_formdata


def _wrap_formdata(self, form, formdata):
    if formdata is _Auto and _is_submitted() and not request.files and not request.form and request.is_json:
        if not isinstance(request.get_json(silent=True), dict):
            abort(400)
    return _flask_wtf_wrap_formdata(self, form, formdata)


def refuse_json_that_is_not_an_object() -> None:
    """Every FlaskForm, PieFed's and its libraries', reads a JSON body only when it is an object."""
    FlaskForm.Meta.wrap_formdata = _wrap_formdata
