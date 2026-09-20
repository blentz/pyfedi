from flask import g
from sqlalchemy import desc

from app.models import Community, User
from app import db


def test_api_post_list(app, api_baseline):
    from app.api.alpha.utils.post import get_post_list

    user_id = api_baseline.user1.id
    user = db.session.get(User, user_id)
    assert user is not None and hasattr(user, 'id')
    jwt = user.encode_jwt_token()
    assert jwt is not None
    auth = f'Bearer {jwt}'

    high_post_community = Community.query.filter(Community.instance_id != 1).order_by(
        desc(Community.post_count)).first()
    assert high_post_community is not None and hasattr(high_post_community, 'id')

    # post list should be more than 0
    g.admin_ids = [1]
    data = {"community_id": high_post_community.id}
    response = get_post_list(auth, data)
    assert 'posts' in response and len(response['posts']) > 0
