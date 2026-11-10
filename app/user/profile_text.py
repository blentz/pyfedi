"""A person's display name and bio as an app sets them: the Mastodon API's update_credentials and PeerTube's PUT
users/me make the same change the profile page makes."""
import app.api.alpha.views as alpha_views
from app import cache, db
from app.federation_urls import owner_base
from app.models import User
from app.utils import markdown_to_html, piefed_markdown_to_lemmy_markdown, user_pronouns


def set_profile_text(user: User, display_name: str | None, note: str | None) -> None:
    """Each one given replaces the old (an empty name clears it); None leaves it as it was."""
    if display_name is not None:
        user.title = display_name or None
    if note is not None:
        user.about = piefed_markdown_to_lemmy_markdown(note, owner_base(user))   # as the profile page (C3)
        user.about_html = markdown_to_html(note, link_base=owner_base(user))
    db.session.commit()
    cache.delete_memoized(user_pronouns)
    cache.delete_memoized(alpha_views.user_view)
