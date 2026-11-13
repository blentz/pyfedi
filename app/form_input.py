"""What a web form reads from a request. Flask-WTF fills a submitted form from a JSON body when there is no form data,
through ImmutableMultiDict, with the JSON's values as they are (fuzz-routes findings, 2026-11-13):

* a body that is JSON but not an object (a number, a list, a string, null) raised TypeError there, a 500 from every
  form of the site. Such a body is a malformed submission: a 400.
* an object's values reached the fields as JSON has them, None, numbers, lists, where every form expects the text a
  browser posts: form.urls.data.strip() raised on None (POST /user/files/upload). An object is read as a form posts it:
  each value as text, null as empty, a list as one value per item.
* a text field a submitted form leaves out: WTForms 3 leaves its data None, where 2 made it '', and PieFed's views read
  text fields as text. It is '' again; DataRequired refuses '' as it refused None.
"""
import json

from flask import abort, request
from flask_wtf.form import FlaskForm, _Auto, _is_submitted
from werkzeug.datastructures import ImmutableMultiDict
from wtforms.fields.simple import StringField

_flask_wtf_wrap_formdata = FlaskForm.Meta.wrap_formdata
_wtforms_string_process_formdata = StringField.process_formdata


def _as_posted(value) -> str:
    """One JSON value as the text a form field posts."""
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


def _wrap_formdata(self, form, formdata):
    if formdata is _Auto and _is_submitted() and not request.files and not request.form and request.is_json:
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            abort(400)
        return ImmutableMultiDict([(key, _as_posted(item)) for key, value in body.items()
                                   for item in (value if isinstance(value, list) else [value])])
    return _flask_wtf_wrap_formdata(self, form, formdata)


def _string_process_formdata(self, valuelist):
    _wtforms_string_process_formdata(self, valuelist)
    if not valuelist and self.data is None:   # submitted without this field: empty text, as WTForms 2 had it
        self.data = ''


def refuse_json_that_is_not_an_object() -> None:
    """Every FlaskForm, PieFed's and its libraries', reads a JSON body only when it is an object, and its values as the
    text a form posts; and every text field a submitted form leaves out reads as empty text."""
    FlaskForm.Meta.wrap_formdata = _wrap_formdata
    StringField.process_formdata = _string_process_formdata
