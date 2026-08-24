from flask import g


def test_api_get_site(app, api_baseline):
    from app.api.alpha.utils.site import get_site
    from app.models import User
    g.site = api_baseline.site

    anon_response = get_site(None)
    assert anon_response is not None and 'version' in anon_response
    assert 'my_user' not in anon_response

    user = User.query.get(api_baseline.user1.id)
    assert user is not None and hasattr(user, 'id')
    jwt = user.encode_jwt_token()
    assert jwt is not None
    auth = f'Bearer {jwt}'

    logged_in_response = get_site(auth)
    assert logged_in_response is not None and 'version' in logged_in_response
    assert 'my_user' in logged_in_response
    assert logged_in_response['my_user']['local_user_view'][
               'show_read_posts'] == False if user.hide_read_posts == True else True
