import pytest
from app import db


def test_api_get_community(app, api_baseline):
    from app.api.alpha.utils.community import get_community
    from app.models import CommunityMember, User

    community = api_baseline.community1

    # Calling with data={} triggers the "no id or name supplied" validation path.
    # (The original version of this test called get_community(None, None); data=None
    # makes `'id' not in data` raise TypeError before the function's own validation
    # runs, and asserted a message ('missing parameters for community') that
    # get_community has never produced -- it raises 'id or name required'. Both are
    # fixed here to exercise, and assert on, the real validation path.)
    with pytest.raises(Exception) as ex:
        get_community(None, {})
    assert str(ex.value) == 'id or name required'

    data = {"id": community.id}
    anon_response = get_community(None, data)
    assert anon_response is not None and anon_response['community_view']['community']['name'] == community.name

    data = {"name": community.ap_id}
    anon_response = get_community(None, data)
    assert anon_response is not None and anon_response['community_view']['community']['id'] == community.id

    user_id = api_baseline.user1.id
    user = db.session.get(User, user_id)
    assert user is not None and hasattr(user, 'id')
    jwt = user.encode_jwt_token()
    assert jwt is not None
    auth = f'Bearer {jwt}'

    cm = CommunityMember.query.filter_by(user_id=user_id).first()
    assert cm is not None
    data = {"id": cm.community_id}
    logged_in_response = get_community(auth, data)
    assert logged_in_response is not None and logged_in_response['community_view']['subscribed'] == "Subscribed"

    cm = CommunityMember.query.filter(CommunityMember.user_id != user_id).first()
    assert cm is not None
    data = {"id": cm.community_id}
    logged_in_response = get_community(auth, data)
    assert logged_in_response is not None and logged_in_response['community_view']['subscribed'] == "NotSubscribed"
