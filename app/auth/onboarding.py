from flask import redirect, url_for, flash, current_app, abort, g, request
from flask_babel import _
from flask_login import current_user, login_required

from app import db, cache
from app.activitypub.signature import send_post_request
from app.auth import bp
from app.auth.forms import ChooseTopicsForm, FilterSetupForm
from app.auth.util import get_country
from app.constants import SUBSCRIPTION_NONMEMBER
from app.models import User, Topic, Community, CommunityJoinRequest, CommunityMember, Filter, InstanceChooser, Language
from app.utils import render_template, joined_communities, community_membership, get_setting, num_topics, ip_address


@bp.route('/instance_chooser')
def onboarding_instance_chooser():
    if get_setting('enable_instance_chooser', False):
        instances = InstanceChooser.query.all()
        language_ids = set()
        for instance in instances:
            language_ids.add(instance.language_id)
        languages = Language.query.filter(Language.id.in_(language_ids)).all()
        return render_template('auth/instance_chooser.html', title=_('Which server do you want to join?'),
                               instances=instances, languages=languages, closed=g.site.registration_mode == 'Closed')
    else:
        return redirect(url_for('auth.register'))


@bp.route('/filter_selection', methods=['GET', 'POST'])
@login_required
def filter_selection():
    if get_setting('filter_selection', True):
        form = FilterSetupForm()
        if form.validate_on_submit():
            if form.trump_musk_level.data >= 0:
                # D1152. The test was `if existing_filters is not None`, so the
                # filter was created only for somebody who ALREADY had one --
                # never for the new account this screen exists to set up, and
                # twice over for anyone who came back to it. Measured:
                #
                #     PROBE ay1 status: 302 | filters now: []
                #     PROBE ay2 filters now: 2 | titles: ['Trump & Musk', 'Trump & Musk']
                existing_filters = Filter.query.filter(Filter.user_id == current_user.id, Filter.title == 'Trump & Musk').first()
                if existing_filters is None:
                    content_filter = Filter(title='Trump & Musk', filter_home=True, filter_posts=True, filter_replies=False, hide_type=form.trump_musk_level.data, keywords='trump\nmusk', expire_after=None, user_id=current_user.id)
                    db.session.add(content_filter)
            current_user.ignore_bots = form.ignore_bots.data
            current_user.hide_nsfw = form.hide_nsfw.data
            current_user.hide_nsfl = form.hide_nsfl.data
            current_user.hide_gen_ai = form.hide_gen_ai.data
            db.session.commit()
            return redirect(url_for('auth.choose_topics'))
        else:
            form.hide_nsfw.data = 0 if current_app.config['CONTENT_WARNING'] or g.site.enable_nsfw else 1
            form.ignore_bots.data = 1
            return render_template('auth/filter_selection.html', form=form)
    else:
        return redirect(url_for('auth.choose_topics'))


@bp.route('/choose_topics', methods=['GET', 'POST'])
@login_required
def choose_topics():
    if get_setting('choose_topics', True) and num_topics() > 0:
        form = ChooseTopicsForm()
        topic_tree, selections = topics_for_form()

        if request.method == 'POST':
            # D1155. `mark_onboarding_as_finished()` was the first line of this
            # route, so merely LOOKING at the page finished onboarding --
            # somebody who opened it and went elsewhere was never brought back
            # to it. Measured: `PROBE ay5 finished_onboarding after a GET:
            # True`. It is finished when they act, here and on the arm below
            # where there is nothing to choose from.
            mark_onboarding_as_finished()
            # Handle form submission - get selected topics from request
            # D1153. `int(topic_id_str)` on `request.form.getlist`, which is
            # whatever was posted: `ValueError: invalid literal for int() with
            # base 10: 'nonsense'`. Anything that is not an id is dropped.
            chosen_topic_ids = [int(chosen) for chosen in request.form.getlist('chosen_topics')
                                if chosen.isdigit()]
            joined = 0
            for topic_id in chosen_topic_ids:
                joined += join_topic(topic_id)
            if joined:
                flash(_('You have joined some communities relating to those interests. Find more on the Explore menu or browse the home page.'))
                cache.delete_memoized(joined_communities, current_user.id)
                return redirect(url_for('main.index'))
            else:
                # D1156. This said "You have joined some communities" for a
                # topic with no communities in it, or with only communities
                # this account may not join. Measured: `PROBE ay6 said: You
                # have joined some communities relating to those interests.`
                flash(_('You did not choose any topics. Would you like to choose individual communities instead?'))
                return redirect(url_for('main.list_communities'))
        else:
            # Set default selections based on user's country
            form.chosen_topics.data = selections
            return render_template('auth/choose_topics.html', form=form, topic_tree=topic_tree)
    else:
        mark_onboarding_as_finished()
        flash(_('Please join some communities you\'re interested in and then go to the home page by clicking on the logo above.'))
        return redirect(url_for('main.list_communities'))


def mark_onboarding_as_finished():
    current_user.finished_onboarding = True
    db.session.commit()


def join_topic(topic_id):
    """The number of communities this account was actually put into."""
    joined = 0
    # D1154. `Community.private` is invite-only real access control
    # (app/models.py:594), and every other surface treats it that way -- this
    # one put the account straight into a CommunityMember row with no join
    # request and nobody's approval, for every private community that happened
    # to carry the topic. Measured: `PROBE ay4 status: 302 | member of the
    # private community: True`.
    communities = Community.query.filter_by(topic_id=topic_id, banned=False,
                                            private=False).all()
    for community in communities:
        if not community.user_is_banned(current_user) and community_membership(current_user, community) == SUBSCRIPTION_NONMEMBER:
            if not community.is_local():
                join_request = CommunityJoinRequest(user_id=current_user.id, community_id=community.id)
                db.session.add(join_request)
                db.session.commit()
                send_community_follow(community.id, join_request.uuid, current_user.id)

            existing_member = CommunityMember.query.filter(CommunityMember.community_id == community.id, CommunityMember.user_id == current_user.id).first()
            if not existing_member:
                member = CommunityMember(user_id=current_user.id, community_id=community.id)
                db.session.add(member)
                db.session.commit()
                joined += 1
            cache.delete_memoized(community_membership, current_user, community)

    return joined


def topics_for_form():
    """Build a hierarchical topic tree with max 3 levels for the form.
    
    Returns:
        list: Nested topic structure with depth info
        list: Default selected topic IDs based on user's country
    """
    topics = Topic.query.filter_by(parent_id=None).order_by(Topic.name).all()
    user_country = get_country(ip_address())
    
    def build_topic_tree(topic, depth=0):
        """Recursively build topic tree, limiting to 3 levels max."""
        if depth > 2:  # Max depth is 2 (0=root, 1=child, 2=grandchild)
            return None
            
        node = {
            'id': topic.id,
            'name': topic.name,
            'depth': depth,
            'children': [],
            'selected': user_country in topic.countries if user_country and topic.countries else False
        }
        
        # Fetch children and build their trees
        sub_topics = Topic.query.filter_by(parent_id=topic.id).order_by(Topic.name).all()
        for sub_topic in sub_topics:
            child = build_topic_tree(sub_topic, depth + 1)
            if child is not None:  # Only add if within depth limit
                node['children'].append(child)
        
        return node
    
    # Build tree from root topics
    topic_tree = []
    selections = []
    
    # D1158. This loop used to test `if node is not None`, which cannot
    # happen: `build_topic_tree` answers None only past depth 2, and these are
    # the roots, at depth 0. The test inside the recursion, where the depth
    # cap actually bites, is the one that matters.
    for topic in topics:
        node = build_topic_tree(topic)
        topic_tree.append(node)
        if node['selected']:
            selections.append(node['id'])
    
    return topic_tree, selections


def send_community_follow(community_id: int, join_request_id: int, user_id: int):
    with current_app.app_context():
        user = db.session.get(User, user_id)
        community = db.session.get(Community, community_id)
        if not community.instance.gone_forever:
            follow = {
                "actor": user.public_url(),
                "to": [community.public_url()],
                "object": community.public_url(),
                "type": "Follow",
                "id": f"{current_app.config['SERVER_URL']}/activities/follow/{join_request_id}"
            }
            send_post_request(community.ap_inbox_url, follow, user.private_key, user.public_url() + '#main-key')
