"""H3 Phase 4: User.email_hmac dual-write choke point (pure-function, no DB).

Proves the @validates hook keeps email_hmac in lockstep with email so the P5
login-lookup cutover + UNIQUE constraint have a reliable key. No Postgres needed
— just ORM instance construction.
"""

from src.db.models import User
from src.utils.crypto import blind_index


def test_email_hmac_set_on_construct():
    u = User(email="Owner@Example.com")
    assert u.email_hmac == blind_index("owner@example.com")


def test_email_hmac_updates_on_reassign():
    u = User(email="a@example.com")
    first = u.email_hmac
    u.email = "b@example.com"
    assert u.email_hmac != first
    assert u.email_hmac == blind_index("b@example.com")


def test_email_hmac_case_and_whitespace_normalized():
    assert User(email="  USER@X.COM ").email_hmac == User(email="user@x.com").email_hmac


def test_blind_index_matches_login_lookup_path():
    # The value @validates stores must equal what a login lookup computes from
    # the raw request email — otherwise no one could log in after the cutover.
    u = User(email="lookup@example.com")
    assert u.email_hmac == blind_index("lookup@example.com")
