import csv
import io

from flask import current_app

from app import celery
from app.constants import SRC_API
from app.models import User
# The module, not the names: app.shared.user reaches this file through a blueprint
# package before they are defined (import cycle: app.shared.user)
import app.shared.user as shared_user
from app.user.utils import search_for_user
from app.utils import validate_email, get_task_session, patch_db_session


def is_fedi_handle(handle):
    return validate_email(handle) or validate_email(handle[1:])


def mastodon_csv_handles(csv_bytes: bytes) -> list[str]:
    """The handles in the first column of an uploaded Mastodon follows export, in file order.

    Rows whose first field is not a handle -- the header row, blank lines, anything else -- are skipped. Raises
    csv.Error for a file that decodes but will not parse as CSV; instance_add_people answers that with a message.
    """
    # D805: UTF-8, then UTF-8 with a byte-order mark, then Latin-1, which decodes any bytes at all. UTF-8 with a mark
    # is UTF-8 that starts with U+FEFF, so it needs no decode of its own: whatever fails as UTF-8 fails as utf-8-sig
    # too, and the mark utf-8 keeps as text is stripped below.
    try:
        csv_text = csv_bytes.decode('utf-8')
    except UnicodeDecodeError:
        csv_text = csv_bytes.decode('latin-1')
    csv_text = csv_text.lstrip('\ufeff')
    return [csv_row[0] for csv_row in csv.reader(io.StringIO(csv_text))
            if csv_row and is_fedi_handle(csv_row[0])]


@celery.task
def bulk_follow(user_id, to_follow):
    with current_app.app_context():
        session = get_task_session()
        try:
            with patch_db_session(session):
                following_user = session.get(User, user_id)
                auth_token = f"Bearer {following_user.encode_jwt_token()}"
                for tf in to_follow:
                    user = search_for_user(tf, retry=True)   # a Celery task: it may wait (D738)
                    if user and following_user.is_following(user) == 'no':
                        shared_user.follow_user(user.id, src=SRC_API, auth=auth_token)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
