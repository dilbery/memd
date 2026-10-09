"""Kasm session tokens as a second identity source.

A Kasm workspace has already signed the user in through authentik, and making
them sign in AGAIN in a browser inside the container is a poor experience. Kasm
gives every session container a `KASM_API_JWT` signed by Kasm's own RSA key, so
memd can accept that as proof of identity.

WHAT MAKES THIS SAFE, and what does not:

  * The token is SIGNED BY KASM. The user has a shell in the container and can
    read it, but cannot forge one for somebody else, which is the whole reason
    a header saying "I am Bob" was rejected as a design.
  * memd holds NO Kasm credentials. The signature is checked against Kasm's
    published public key, and the `user_id` to email mapping is a file the
    onboarding job maintains on the identity-provider host, where the privileged credentials already
    live. memd only reads it.
  * `kasm_id` must be an ACTIVE session. This is the mitigation for the thing
    that is genuinely unpleasant about these tokens: they stay valid for years.
    Binding them to a live session means a copied token stops working when the
    workspace closes, instead of years later.

Deliberately NOT accepted: an unsigned header naming a user. The container is
under the user's control, so that would let any user read any other user's
memory, which is the property the whole of Plan 2 exists to provide.
"""
import json
import time

import pytest

pytest.importorskip("jwt")
import jwt  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from cryptography.hazmat.primitives.serialization import (  # noqa: E402
    Encoding, PublicFormat,
)

from memd.identity import clear_identity, current_identity  # noqa: E402
from memd.kasm import KasmError, kasm_identity, kasm_enabled, reset_kasm_cache  # noqa: E402

ALICE = "alice@example.invalid"
BOB = "bob@example.invalid"
ALICE_ID = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
BOB_ID = "11111111-2222-3333-4444-555555555555"
SESSION = "cccccccc-3333-4333-8333-cccccccccccc"


@pytest.fixture(scope="module")
def keys():
    good = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    rogue = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return {"good": good, "rogue": rogue}


@pytest.fixture(autouse=True)
def _clean():
    clear_identity()
    reset_kasm_cache()
    yield
    clear_identity()
    reset_kasm_cache()


@pytest.fixture
def kasm(tmp_path, monkeypatch, keys):
    pub = tmp_path / "kasm-jwt.pub"
    pub.write_bytes(keys["good"].public_key().public_bytes(
        Encoding.PEM, PublicFormat.SubjectPublicKeyInfo))
    users = tmp_path / "kasm-users.json"
    users.write_text(json.dumps({
        "users": {ALICE_ID: ALICE, BOB_ID: BOB},
        "active_sessions": [SESSION],
    }))
    monkeypatch.setenv("MEMD_KASM_JWT_PUBKEY", str(pub))
    monkeypatch.setenv("MEMD_KASM_USER_MAP", str(users))
    monkeypatch.setenv("MEMD_STORES_ROOT", str(tmp_path / "stores"))
    return users


def _tok(keys, which="good", **over):
    claims = {"kasm_id": SESSION, "profile_path": "", "image_id": "img",
              "user_id": ALICE_ID, "exp": int(time.time()) + 86400,
              "authorizations": [50]}
    claims.update(over)
    for k in [k for k, v in claims.items() if v is None]:
        del claims[k]
    return jwt.encode(claims, keys[which], algorithm="RS256")


class TestEnablement:
    def test_off_when_not_configured(self, monkeypatch):
        monkeypatch.delenv("MEMD_KASM_JWT_PUBKEY", raising=False)
        assert kasm_enabled() is False

    def test_on_when_configured(self, kasm):
        assert kasm_enabled() is True


class TestSignature:
    def test_a_kasm_signed_token_yields_its_owner(self, kasm, keys):
        assert kasm_identity("Bearer " + _tok(keys)) == ALICE

    def test_a_token_signed_by_another_key_is_refused(self, kasm, keys):
        """The forgery case: without this the whole design is a header."""
        with pytest.raises(KasmError):
            kasm_identity("Bearer " + _tok(keys, which="rogue"))

    def test_an_alg_none_token_is_refused(self, kasm, keys):
        tok = jwt.encode({"user_id": ALICE_ID, "kasm_id": SESSION},
                         key="", algorithm="none")
        with pytest.raises(KasmError):
            kasm_identity("Bearer " + tok)

    def test_a_tampered_user_id_is_refused(self, kasm, keys):
        """Alice editing her own token to claim Bob's uuid."""
        import base64

        head, payload, sig = _tok(keys).split(".")
        body = json.loads(base64.urlsafe_b64decode(payload + "=="))
        body["user_id"] = BOB_ID
        forged = base64.urlsafe_b64encode(
            json.dumps(body).encode()).decode().rstrip("=")
        with pytest.raises(KasmError):
            kasm_identity(f"Bearer {head}.{forged}.{sig}")


class TestClaims:
    def test_an_expired_token_is_refused(self, kasm, keys):
        with pytest.raises(KasmError):
            kasm_identity("Bearer " + _tok(keys, exp=int(time.time()) - 600))

    def test_an_unknown_user_id_is_refused(self, kasm, keys):
        """A real Kasm token for somebody memd has never heard of."""
        with pytest.raises(KasmError):
            kasm_identity("Bearer " + _tok(keys, user_id="99999999-0000-0000-0000-000000000000"))

    def test_a_token_with_no_user_id_is_refused(self, kasm, keys):
        with pytest.raises(KasmError):
            kasm_identity("Bearer " + _tok(keys, user_id=None))


class TestSessionMustBeLive:
    """The mitigation for a token that is otherwise valid for years."""

    def test_a_token_for_a_closed_session_is_refused(self, kasm, keys):
        with pytest.raises(KasmError):
            kasm_identity("Bearer " + _tok(keys, kasm_id="00000000-dead-dead-dead-000000000000"))

    def test_closing_the_session_stops_the_token_working(self, kasm, keys):
        tok = "Bearer " + _tok(keys)
        assert kasm_identity(tok) == ALICE
        kasm.write_text(json.dumps({"users": {ALICE_ID: ALICE}, "active_sessions": []}))
        reset_kasm_cache()
        with pytest.raises(KasmError):
            kasm_identity(tok)

    def test_a_missing_active_list_refuses_rather_than_admits(self, kasm, keys):
        """Fail closed. A map the job has not written yet must not mean
        'every session is live'."""
        kasm.write_text(json.dumps({"users": {ALICE_ID: ALICE}}))
        reset_kasm_cache()
        with pytest.raises(KasmError):
            kasm_identity("Bearer " + _tok(keys))


class TestMapHandling:
    def test_a_missing_map_refuses(self, kasm, keys, tmp_path, monkeypatch):
        monkeypatch.setenv("MEMD_KASM_USER_MAP", str(tmp_path / "gone.json"))
        reset_kasm_cache()
        with pytest.raises(KasmError):
            kasm_identity("Bearer " + _tok(keys))

    def test_the_map_is_reread_when_it_changes(self, kasm, keys):
        """A new starter must work without restarting memd."""
        new_id = "77777777-8888-9999-aaaa-bbbbbbbbbbbb"
        with pytest.raises(KasmError):
            kasm_identity("Bearer " + _tok(keys, user_id=new_id))
        kasm.write_text(json.dumps({
            "users": {ALICE_ID: ALICE, new_id: "newstarter@example.invalid"},
            "active_sessions": [SESSION]}))
        reset_kasm_cache()
        assert kasm_identity("Bearer " + _tok(keys, user_id=new_id)) \
            == "newstarter@example.invalid"

    def test_an_email_that_is_not_a_usable_store_name_is_refused(self, kasm, keys):
        kasm.write_text(json.dumps({
            "users": {ALICE_ID: "../etc/passwd"}, "active_sessions": [SESSION]}))
        reset_kasm_cache()
        with pytest.raises(KasmError):
            kasm_identity("Bearer " + _tok(keys))


class TestBinding:
    def test_a_valid_token_binds_the_store(self, kasm, keys):
        kasm_identity("Bearer " + _tok(keys))
        assert current_identity() == ALICE

    def test_a_refused_token_binds_nothing(self, kasm, keys):
        with pytest.raises(KasmError):
            kasm_identity("Bearer " + _tok(keys, which="rogue"))
        assert current_identity() is None

    def test_a_missing_or_malformed_header_is_refused(self, kasm):
        for bad in (None, "", "Basic x", "Bearer", "Bearer   "):
            with pytest.raises(KasmError):
                kasm_identity(bad)
