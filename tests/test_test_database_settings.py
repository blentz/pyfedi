"""The disposable test database is tuned for a suite of tiny, unanalysed tables.

JIT off: the suite's tables are fresh and never ANALYZEd, so the planner has no
statistics, and a post query eager-loading a dozen LEFT JOINs is costed at about
a million rows. That crossed jit_above_cost, jit_optimize_above_cost and
jit_inline_above_cost, and Postgres spent ~2.7 seconds JIT-compiling (92
functions) a query that then ran in milliseconds over five rows -- every API post
list and front-page test paid it, once per listing. Production tables are
analysed, so this is a test-stack setting (compose.test.yaml), not an app one.
"""
from sqlalchemy import text

from app import db


def test_the_test_database_does_not_jit_compile_queries(app, db_session):
    assert db.session.execute(text('SHOW jit')).scalar() == 'off'
