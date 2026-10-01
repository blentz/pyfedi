from app.models import User
from flask_login import current_user

from app.shared.upload import process_upload, process_file_delete
from app.utils import authorise_api_user


def post_upload_image(auth, image_file=None):
    # D880: a presented token must authorise by itself; only a request with no
    # token at all falls back to the browser session.
    if auth:
        user: User = authorise_api_user(auth, return_type="model")
    elif current_user.is_authenticated:
        user = current_user
    else:
        raise Exception('incorrect_login')

    # D881/D1061: no storage quota is enforced on uploads, so nothing sums the caller's stored files
    url = process_upload(image_file, user=user)
    return {'url': url}


def post_upload_community_image(auth, image_file=None):
    authorise_api_user(auth)
    url = process_upload(image_file, destination='communities')
    return {'url': url}


def post_upload_user_image(auth, image_file=None):
    authorise_api_user(auth)
    url = process_upload(image_file, destination='users')
    return {'url': url}


def post_image_delete(auth, data):
    if auth:
        user_id = authorise_api_user(auth)
    elif current_user.is_authenticated:
        user_id = current_user.id
    else:
        raise Exception('incorrect_login')
    process_file_delete(data['file'], user_id=user_id)
    return {'result': 'ok'}
