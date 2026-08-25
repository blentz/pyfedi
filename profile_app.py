#!flask/bin/python
import os

from flask import json
from flask_wtf.csrf import generate_csrf
from werkzeug.middleware.profiler import ProfilerMiddleware
from app import create_app, cli
from app.utils import shorten_number, community_membership, digits, user_access, ap_datetime, \
    can_create_post, can_upvote, can_downvote, current_theme, shorten_string, shorten_url, feed_membership, role_access, \
    in_sorted_list, first_paragraph, html_to_text, community_link_to_href, person_link_to_href, remove_images, \
    feed_link_to_href, show_explore, human_filesize

app = create_app()


with app.app_context():
    app.jinja_env.globals['len'] = len
    app.jinja_env.globals['digits'] = digits
    app.jinja_env.globals['str'] = str
    app.jinja_env.globals['shorten_number'] = shorten_number
    app.jinja_env.globals['community_membership'] = community_membership
    app.jinja_env.globals['feed_membership'] = feed_membership
    app.jinja_env.globals['json_loads'] = json.loads
    app.jinja_env.globals['user_access'] = user_access
    app.jinja_env.globals['role_access'] = role_access
    app.jinja_env.globals['ap_datetime'] = ap_datetime
    app.jinja_env.globals['can_upvote'] = can_upvote
    app.jinja_env.globals['can_downvote'] = can_downvote
    app.jinja_env.globals['show_explore'] = show_explore
    app.jinja_env.globals['in_sorted_list'] = in_sorted_list
    app.jinja_env.globals['theme'] = current_theme
    app.jinja_env.globals['file_exists'] = os.path.exists
    app.jinja_env.globals['first_paragraph'] = first_paragraph
    app.jinja_env.globals['html_to_text'] = html_to_text
    app.jinja_env.globals['csrf_token'] = generate_csrf
    app.jinja_env.filters['community_links'] = community_link_to_href
    app.jinja_env.filters['feed_links'] = feed_link_to_href
    app.jinja_env.filters['person_links'] = person_link_to_href
    app.jinja_env.filters['shorten'] = shorten_string
    app.jinja_env.filters['shorten_url'] = shorten_url
    app.jinja_env.filters['remove_images'] = remove_images
    app.jinja_env.filters["human_filesize"] = human_filesize
    app.config['PROFILE'] = True
    app.wsgi_app = ProfilerMiddleware(app.wsgi_app, restrictions=[500])
    app.run(debug = True, host='127.0.0.1')
