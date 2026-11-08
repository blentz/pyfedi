"""app.user.profile_images: an upload that saved nothing changes nothing (plan W1.1, the web profile page's and
update_credentials' shared avatar and banner replacement)."""
from unittest.mock import patch

from app import db
from app.models import File
from app.user.profile_images import replace_avatar, replace_cover
from tests.factories import make_user


def test_an_upload_that_saved_nothing_keeps_the_old_images(app, api_baseline):
    me = make_user(None, 'keepimages', local=True)
    kept_cover, kept_avatar = File(file_path='app/static/media/users/kept.png'), File(file_path='app/static/a.png')
    me.cover, me.avatar = kept_cover, kept_avatar
    db.session.commit()
    with patch('app.user.profile_images.save_banner_file', return_value=None), \
            patch('app.user.profile_images.save_icon_file', return_value=None), \
            patch('app.user.profile_images.cache.delete_memoized') as forgotten:
        assert replace_cover(me, object()) is False
        assert replace_avatar(me, object()) is False
    forgotten.assert_not_called()
    assert me.cover_id == kept_cover.id and me.avatar_id == kept_avatar.id
