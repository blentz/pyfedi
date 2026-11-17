"""A code's limits hold under concurrent use (paid membership G7, D8): two sessions on real connections, one holding a
use of a code that is not yet committed while the other tries the same code. The second must wait for the first and
then be refused; without the row lock it would read no use and go ahead, and the code would be used twice."""
import threading
from types import SimpleNamespace

import pytest

from app import db
from app.models import Offer, PromoCode, PromoCodeUse, Site, User
from app.payments.discounts import reserve
from app.payments.gifts import start_gift
from tests.factories import make_user

WAIT = 2   # seconds the second session is given to (wrongly) go ahead while the first holds the code


@pytest.fixture
def env(app, api_baseline):
    db.session.get(Site, 1).private_instance = False
    knight = Offer(provider_kind='btcpay', kind='subscription', title='Furcas', offer_group='tiers',
                   amount_minor=666, currency='usd', interval='month')
    db.session.add(knight)
    first = make_user(api_baseline.instance_local, 'first', local=True)
    second = make_user(api_baseline.instance_local, 'second', local=True)
    db.session.commit()
    return SimpleNamespace(app=app, knight=knight, first=first, second=second)


def holding_a_use(app, code_id: int, user_id: int, locked: threading.Event, release: threading.Event):
    """Another session's use in progress: the code's row locked and a use written, not yet committed."""
    def run():
        with app.app_context():
            try:
                PromoCode.query.filter_by(id=code_id).with_for_update().one()
                db.session.add(PromoCodeUse(promo_code_id=code_id, user_id=user_id))
                db.session.flush()
                locked.set()
                release.wait(10)
                db.session.commit()
            finally:
                db.session.remove()
    return threading.Thread(target=run)


def racing(app, attempt):
    """Run `attempt` in its own session; what it returned (as a bool), or None while it has not finished."""
    outcome = {}

    def run():
        with app.app_context():
            try:
                outcome['done'] = attempt() is not None
            finally:
                db.session.rollback()
                db.session.remove()
    thread = threading.Thread(target=run)
    thread.start()
    return thread, outcome


def race(env, code_id, attempt):
    locked, release = threading.Event(), threading.Event()
    holder = holding_a_use(env.app, code_id, env.first.id, locked, release)
    holder.start()
    assert locked.wait(10)
    thread, outcome = racing(env.app, attempt)
    thread.join(WAIT)
    went_ahead_while_held = not thread.is_alive()
    release.set()
    holder.join(10)
    thread.join(10)
    return went_ahead_while_held, outcome.get('done')


class TestAGiftCodeWithOneUseLeft:
    def test_the_second_member_waits_and_is_refused(self, env):
        code = PromoCode(code='one-gift', kind='gift', offer_id=env.knight.id, periods=1, max_uses=1)
        db.session.add(code)
        db.session.commit()
        code_id, second_id = code.id, env.second.id

        def attempt():
            from_session = db.session.get(PromoCode, code_id)
            return start_gift(db.session.get(User, second_id), from_session)

        went_ahead, done = race(env, code_id, attempt)
        assert not went_ahead and done is False
        db.session.expire_all()
        assert PromoCodeUse.query.filter_by(promo_code_id=code_id).count() == 1


class TestADiscountCodeWithOneUseLeft:
    def test_the_second_checkout_waits_and_is_refused(self, env):
        code = PromoCode(code='one-discount', kind='percent', percent=20, max_uses=1)
        db.session.add(code)
        db.session.commit()
        code_id, second_id, offer_id = code.id, env.second.id, env.knight.id

        def attempt():
            return reserve(db.session.get(User, second_id), db.session.get(PromoCode, code_id),
                           db.session.get(Offer, offer_id))

        went_ahead, done = race(env, code_id, attempt)
        assert not went_ahead and done is False
        db.session.expire_all()
        assert PromoCodeUse.query.filter_by(promo_code_id=code_id).count() == 1
