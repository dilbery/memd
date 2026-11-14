"""Persistent administration data. Never stores plaintext login/API credentials."""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time


def enabled():
    return bool(os.environ.get("MEMD_ADMIN_DB"))


def root():
    return Path(os.environ["MEMD_ADMIN_DB"]).resolve().parent


@contextlib.contextmanager
def db():
    path = Path(os.environ["MEMD_ADMIN_DB"])
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    connection = sqlite3.connect(path, timeout=15)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        _wal(connection)
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _wal(connection):
    """Switch the database to WAL, waiting for a connection that is making the same switch.

    Only a database not yet in WAL mode takes a lock for this, and SQLite
    refuses that lock at once, without the busy timeout, when two connections
    ask for it together: instances that share a new database and start at the
    same moment. On every later connection the pragma changes nothing.
    """
    deadline = time.monotonic() + 15
    while True:
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or time.monotonic() > deadline:
                raise
            time.sleep(0.02)


def initialize():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, password TEXT NOT NULL,
            role TEXT NOT NULL, disabled INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS stores(
            id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
            config TEXT NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS grants(
            user_id TEXT REFERENCES users(id), store_id TEXT REFERENCES stores(id),
            permission TEXT NOT NULL, PRIMARY KEY(user_id,store_id));
        CREATE TABLE IF NOT EXISTS tokens(
            id TEXT PRIMARY KEY, digest TEXT NOT NULL UNIQUE, label TEXT NOT NULL,
            user_id TEXT REFERENCES users(id), store_id TEXT NOT NULL,
            scope TEXT NOT NULL, masked TEXT NOT NULL, legacy INTEGER NOT NULL DEFAULT 0,
            created REAL NOT NULL, last_used REAL, expires REAL, revoked REAL);
        CREATE TABLE IF NOT EXISTS sessions(
            digest TEXT PRIMARY KEY, user_id TEXT REFERENCES users(id), csrf TEXT NOT NULL,
            created REAL NOT NULL, expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS audit(
            id INTEGER PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL,
            target TEXT NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS login_attempts(
            key TEXT PRIMARY KEY, failures INTEGER NOT NULL, since REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS jobs(
            id TEXT PRIMARY KEY, store_id TEXT NOT NULL, action TEXT NOT NULL,
            state TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', created REAL NOT NULL,
            finished REAL);
        CREATE UNIQUE INDEX IF NOT EXISTS one_running_job ON jobs(store_id)
            WHERE state IN ('queued','running');
        """)
    _add_columns()
    os.chmod(os.environ["MEMD_ADMIN_DB"], 0o600)


def _add_columns():
    """Add the columns an earlier database lacks, each with its backfill, in one transaction.

    sqlite3's implicit transactions would commit an ALTER before the UPDATE that
    follows it. A failure between the two would leave the column in place and
    its backfill skipped for good, because the column check then passes. So
    this connection is in autocommit mode and the transaction is explicit.

    The column lists are read after the write lock is taken: instances that
    share this database and start together then cannot both decide to add a
    column, and the later one finds nothing left to do.
    """
    connection = sqlite3.connect(os.environ["MEMD_ADMIN_DB"], timeout=15, isolation_level=None)
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            grants = {r[1] for r in connection.execute("PRAGMA table_info(grants)")}
            tokens = {r[1] for r in connection.execute("PRAGMA table_info(tokens)")}
            if "review" not in grants:
                connection.execute("ALTER TABLE grants ADD COLUMN review INTEGER NOT NULL DEFAULT 0")
                # Until review was its own flag, write included it. Only here, when
                # the column is new: a later run must not undo a review since removed.
                connection.execute("UPDATE grants SET review=1 WHERE permission='write'")
            # Whether a token carries its owner's review (memd.access), off unless issued with it.
            if "review" not in tokens:
                connection.execute("ALTER TABLE tokens ADD COLUMN review INTEGER NOT NULL DEFAULT 0")
            # Additional stores a token may reach ({store: read|write}), each still
            # intersected with the owner's current grants on every request.
            if "extra" not in tokens:
                connection.execute("ALTER TABLE tokens ADD COLUMN extra TEXT NOT NULL DEFAULT '{}'")
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
    finally:
        connection.close()


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def identifier(value, field="identifier"):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,47}", value):
        raise ValueError(f"{field} must start with a lowercase letter and contain only letters, digits or hyphens (max 48).")
    return value


def _scrypt_maxmem(n, r=8):
    """scrypt needs 128*n*r bytes and RAISES if maxmem merely EQUALS that.

    Passing the exact figure makes every verify throw, and password_matches
    swallows exceptions as False -- a silent, total lockout with nothing
    logged. Always leave headroom.
    """
    return 128 * n * r * 2


# OWASP Password Storage guidance for scrypt is n=2**17 at r=8, p=1.
SCRYPT_N = 1 << 17
LEGACY_SCRYPT_N = 1 << 14


def _scrypt_slots():
    try:
        return max(1, int(os.environ.get("MEMD_SCRYPT_CONCURRENCY", "2")))
    except ValueError:
        return 2


# Each hash at SCRYPT_N holds about 128 MB while it runs. The login rate limit
# is per key, so a burst across many usernames could still start one per
# request thread; this caps how many run at once and queues the rest.
_scrypt_gate = threading.BoundedSemaphore(_scrypt_slots())


def _scrypt(password, salt, n):
    with _scrypt_gate:
        return hashlib.scrypt(password.encode(), salt=salt.encode(), n=n, r=8, p=1,
                              maxmem=_scrypt_maxmem(n)).hex()


def needs_rehash(stored):
    """True when a stored hash predates the current cost, so login can upgrade."""
    parts = (stored or "").split("$")
    try:
        return len(parts) != 4 or int(parts[1]) < SCRYPT_N
    except (ValueError, TypeError):
        return False


def password_hash(password):
    if not isinstance(password, str) or not 12 <= len(password) <= 1024:
        raise ValueError("Use a password of 12–1024 characters.")
    salt = secrets.token_hex(16)
    value = _scrypt(password, salt, SCRYPT_N)
    return f"scrypt${SCRYPT_N}${salt}${value}"


def password_matches(password, stored):
    try:
        if not isinstance(password, str) or len(password) > 1024:
            return False
        parts = stored.split("$")
        # 3 parts = the original scrypt$salt$hash at n=2**14. Still accepted, so
        # raising the cost can never lock an existing account out. 4 parts carry
        # their own n, so it can be raised again later the same way.
        if len(parts) == 4:
            _, n_text, salt, expected = parts
            n = int(n_text)
            if n < LEGACY_SCRYPT_N or n > (1 << 22) or n & (n - 1):
                return False
        else:
            _, salt, expected = parts
            n = LEGACY_SCRYPT_N
        actual = _scrypt(password, salt, n)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError, AttributeError):
        return False


def audit_read(action, target):
    """Record a read against the corpus. Best effort: never fails a request.

    Writes were audited from the start; reads were not, so copying the whole
    corpus left no trace at all -- and reads are the exfiltration path for a
    memory service.

    Recall QUERIES are deliberately not stored: they are the user's own
    prompts, and a log of every question asked is a second sensitive corpus
    sitting beside the first, with weaker protection.
    """
    if not enabled():
        return
    try:
        from memd import access
        from memd.actor import current_actor
        principal = access.current.get()
        # A legacy or tokens-file bearer has no principal; require_token stamps
        # its label as the actor, which says more than "anonymous".
        actor = principal.label if principal else (current_actor.get() or "anonymous")
        with db() as c:
            audit(c, actor, action, str(target)[:200])
            _prune_read_audit(c)
    except Exception:
        pass  # auditing must never turn a working read into a 500


def audit_decision(action, actor, target):
    """Record a review decision (memd.inbox). Best effort, like audit_read.

    The decision is already made when this runs, so a failure here must not
    reach the reviewer as one. Without MEMD_ADMIN_DB it does nothing: the inbox
    row's own reviewer and time are then the only record.
    """
    if not enabled():
        return
    try:
        with db() as c:
            audit(c, actor or "anonymous", action, str(target)[:200])
    except Exception:
        pass


# Read entries are high volume (the recall hook runs on every prompt), so they
# expire; administrative entries (logins, tokens, grants, exports) are kept.
READ_AUDIT_ACTIONS = ("recall", "recall.federated", "read")
_last_read_prune = 0.0


def read_audit_days() -> float:
    try:
        return max(1.0, float(os.environ.get("MEMD_AUDIT_READ_DAYS", "90")))
    except ValueError:
        return 90.0


def _prune_read_audit(c, *, now=None):
    """Delete read entries older than MEMD_AUDIT_READ_DAYS, at most hourly."""
    global _last_read_prune
    now = time.time() if now is None else now
    if now - _last_read_prune < 3600:
        return
    _last_read_prune = now
    marks = ",".join("?" * len(READ_AUDIT_ACTIONS))
    c.execute(f"DELETE FROM audit WHERE action IN ({marks}) AND created < ?",
              (*READ_AUDIT_ACTIONS, now - read_audit_days() * 86400))


def audit(c, actor, action, target):
    c.execute("INSERT INTO audit(actor,action,target,created) VALUES(?,?,?,?)",
              (actor, action, target, time.time()))


def create_user(username, password, role="member", *, actor="operator"):
    identifier(username, "Username")
    if role not in {"admin", "member"}:
        raise ValueError("Role must be admin or member.")
    hashed = password_hash(password)
    uid = secrets.token_hex(12)
    with db() as c:
        c.execute("INSERT INTO users(id,username,password,role,created) VALUES(?,?,?,?,?)",
                  (uid, username, hashed, role, time.time()))
        audit(c, actor, "user.create", username)
    return uid


def stores():
    if not enabled():
        return {}
    with db() as c:
        rows = c.execute("SELECT * FROM stores ORDER BY created,id").fetchall()
    return {r["id"]: {**dict(r), "config": json.loads(r["config"])} for r in rows}


def store(profile):
    return stores().get(profile)


def put_store(profile, name, kind, config, *, actor="operator", create=False):
    identifier(profile, "Store ID")
    if kind not in {"existing", "local", "git", "obsidian"}:
        raise ValueError("Unknown store type.")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120:
        raise ValueError("A store name is required (max 120 characters).")
    with db() as c:
        if create:
            c.execute("INSERT INTO stores VALUES(?,?,?,?,?)", (profile,name,kind,json.dumps(config),time.time()))
        else:
            before = c.execute("SELECT config FROM stores WHERE id=?", (profile,)).fetchone()
            if not c.execute("UPDATE stores SET name=?,kind=?,config=? WHERE id=?", (name,kind,json.dumps(config),profile)).rowcount:
                raise ValueError("Unknown store.")
        audit(c, actor, "store.create" if create else "store.update", profile)
        if not create:
            # Whoever may change the policy can step around it (under four-eyes
            # not even an administrator approves their own save), so a change
            # is named in the log with what it was and what it became.
            from memd.inbox import review_policy
            old = review_policy(profile, config=json.loads(before["config"]))
            new = review_policy(profile, config=config)
            if old != new:
                audit(c, actor, "store.review_policy", f"{profile}:{old}->{new}")


def register_existing():
    """Register only this instance's configured store; never change its paths."""
    from memd.config import Config
    cfg = Config.from_env()
    if not store(cfg.profile):
        import subprocess
        remote = subprocess.run(["git","-C",str(cfg.clone),"remote","get-url","origin"],capture_output=True,text=True,timeout=5)
        config = {"clone_path": str(cfg.clone.resolve()), "db_path": str(cfg.db.resolve()),
                  "repo_ssh": remote.stdout.strip() if remote.returncode == 0 else "", "credential": "", "managed": False,
                  "tokens_file": os.environ.get("MEMD_TOKENS_FILE","/home/memd/.memd/tokens"),
                  "sync_interval": 0}
        with db() as c:
            c.execute("INSERT OR IGNORE INTO stores VALUES(?,?,?,?,?)", (cfg.profile,cfg.profile,"existing",json.dumps(config),time.time()))
    from memd.mcp_http import load_tokens
    token_records(load_tokens(),cfg.profile)


def public_store(row):
    from memd.inbox import review_policy
    cfg = row["config"]
    return {"id": row["id"], "name": row["name"], "kind": row["kind"], "config": {
        key: cfg.get(key) for key in ("repo_ssh", "branch", "vault_path", "include", "exclude",
                                     "write_folder", "sync_interval", "embed_url", "embed_model",
                                     "rerank_url", "rerank_model", "git_username", "key_file")},
        "has_credential": bool(cfg.get("credential")), "managed": bool(cfg.get("managed")),
        "encrypted": bool(cfg.get("key_file")), "publish_review": publish_review(row),
        # The effective policy, as publish_review is: the setting, else the environment, else open.
        "review_policy": review_policy(row["id"], config=cfg), "reviewers": reviewers(row["id"])}


def reviewers(profile):
    """How many enabled accounts may review this store, administrators included.

    Settings warns when it is below two: under the four-eyes policy a candidate
    is decided from an account other than its proposer's, so a lone reviewer
    could have nothing of their own approved.
    """
    with db() as c:
        return c.execute("SELECT COUNT(*) FROM users u WHERE u.disabled=0 AND (u.role='admin' OR EXISTS("
                         "SELECT 1 FROM grants g WHERE g.user_id=u.id AND g.store_id=? AND g.review=1))",
                         (profile,)).fetchone()[0]


def publish_review(row):
    """The store's effective publish_review: its setting, else memd.share's
    default (MEMD_<STORE>_PUBLISH_REVIEW, then on), so Settings shows what
    publishing will actually do for a store registered from the environment."""
    if "publish_review" in row["config"]:
        return row["config"]["publish_review"] is not False
    from memd.share import publish_review as effective
    return effective(row["id"])


def token_records(legacy_tokens=None, profile=None, *, preserve_master=False):
    now = time.time()
    with db() as c:
        for value, label in (legacy_tokens or {}).items():
            hashed = digest(value)
            c.execute("INSERT OR IGNORE INTO tokens(id,digest,label,store_id,scope,masked,legacy,created) VALUES(?,?,?,?,?,?,1,?)",
                      (hashed[:24],hashed,label,profile,"write","••••"+value[-4:],now))
        if legacy_tokens is not None:
            live = {digest(value) for value in legacy_tokens}
            for row in c.execute("SELECT id,digest,label FROM tokens WHERE legacy=1 AND store_id=? AND revoked IS NULL",(profile,)).fetchall():
                if preserve_master and row["label"] == "legacy":
                    continue
                if row["digest"] not in live:
                    c.execute("UPDATE tokens SET revoked=? WHERE id=?",(now,row["id"]))
        rows = c.execute("SELECT t.id,t.label,t.store_id,t.scope,t.masked,t.legacy,t.created,t.last_used,t.expires,t.revoked,t.extra,t.review,u.username FROM tokens t LEFT JOIN users u ON t.user_id=u.id ORDER BY t.created DESC").fetchall()
    return [{**dict(r), "extra": json.loads(r["extra"] or "{}"), "review": bool(r["review"])} for r in rows]


def refresh_legacy_files():
    """Discover CLI-issued tokens from every explicitly registered store."""
    for profile,row in stores().items():
        path = row["config"].get("tokens_file")
        if not path:
            continue
        try:
            content = Path(path).read_text()
        except OSError:
            continue
        values = {}
        for line in content.splitlines():
            parts = line.strip().split(None,1)
            if len(parts)==2 and not parts[0].startswith("#"):
                values[parts[1]] = parts[0]
        token_records(values,profile,preserve_master=True)


MAX_EXTRA_STORES = 15


def issue_token(label, user_id, profile, scope, expires, *, actor, extra_stores=None, review=False):
    """Issue a token for one store, plus optional `extra_stores` ({store: read|write}).

    Extra stores let one agent token publish between stores and recall across
    them (memd.share). Each is checked against the user's grants now and, like
    the main store, intersected with them again on every request.

    `review` lets the token review the inbox of those of its stores that its
    owner may review, again decided on every request (memd.access). It is
    refused for an owner who may review none of them.
    """
    if not isinstance(review, bool):
        raise ValueError("Review must be true or false.")
    extra = {} if extra_stores is None else extra_stores
    if not isinstance(extra, dict) or len(extra) > MAX_EXTRA_STORES:
        raise ValueError(f"Additional stores must be an object of at most {MAX_EXTRA_STORES} stores.")
    for key, value in extra.items():
        identifier(key, "Store ID")
        if value not in {"read", "write"}:
            raise ValueError("Token access must be read or write.")
        if key == profile:
            raise ValueError("An additional store must differ from the token's store.")
        if not store(key):
            raise ValueError("Unknown store.")
    if not isinstance(label, str) or not 1 <= len(label.strip()) <= 100:
        raise ValueError("Token name must contain 1–100 characters.")
    if scope not in {"read", "write"}:
        raise ValueError("Token access must be read or write.")
    if expires is not None and (not isinstance(expires, (float,int)) or expires <= time.time()):
        raise ValueError("Expiry must be in the future.")
    if not store(profile):
        raise ValueError("Unknown store.")
    value = f"mem_{profile}_{secrets.token_urlsafe(32)}"
    tid = secrets.token_hex(12)
    with db() as c:
        user = c.execute("SELECT * FROM users WHERE id=? AND disabled=0", (user_id,)).fetchone()
        if not user:
            raise ValueError("Choose an active user.")
        grant = c.execute("SELECT permission FROM grants WHERE user_id=? AND store_id=?", (user_id,profile)).fetchone()
        if user["role"] != "admin" and (not grant or (scope == "write" and grant[0] != "write")):
            raise ValueError("The user does not have the requested access to this store.")
        for key, wanted in extra.items():
            grant = c.execute("SELECT permission FROM grants WHERE user_id=? AND store_id=?", (user_id,key)).fetchone()
            if user["role"] != "admin" and (not grant or (wanted == "write" and grant[0] != "write")):
                raise ValueError("The user does not have the requested access to an additional store.")
        if review and user["role"] != "admin":
            marks = ",".join("?" * (1 + len(extra)))
            if not c.execute(f"SELECT 1 FROM grants WHERE user_id=? AND review=1 AND store_id IN ({marks})",
                             (user_id, profile, *extra)).fetchone():
                raise ValueError("The user does not hold review on any of this token's stores.")
        c.execute("INSERT INTO tokens(id,digest,label,user_id,store_id,scope,masked,created,expires,extra,review) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                  (tid,digest(value),label.strip(),user_id,profile,scope,"••••"+value[-4:],time.time(),expires,
                   json.dumps(dict(sorted(extra.items()))),int(review)))
        audit(c,actor,"token.issue",tid)
    return {"id": tid, "token": value}


def main():
    """Local-only administrator bootstrap / recovery. Password arrives on stdin."""
    import argparse
    import getpass
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["bootstrap", "reset-password"])
    parser.add_argument("username")
    args = parser.parse_args()
    initialize()
    password = getpass.getpass("Password: ")
    if args.action == "bootstrap":
        with db() as c:
            if c.execute("SELECT 1 FROM users WHERE role='admin' AND disabled=0").fetchone():
                raise SystemExit("An administrator already exists. Use reset-password for recovery.")
        create_user(args.username,password,"admin")
    else:
        hashed = password_hash(password)
        with db() as c:
            row = c.execute("SELECT id FROM users WHERE username=?",(args.username,)).fetchone()
            if not row:
                raise SystemExit("Unknown account.")
            c.execute("UPDATE users SET password=? WHERE id=?",(hashed,row[0]))
            c.execute("DELETE FROM sessions WHERE user_id=?",(row[0],))
            audit(c,"operator","password.reset",args.username)
    print("Account ready.")


if __name__ == "__main__":
    main()
