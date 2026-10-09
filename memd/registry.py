"""Durable token control plane. Never stores a presented bearer credential.

MEMD_CONTROL_DB is an explicit, one-way cutover: when configured the old file
is never consulted for authentication. Import is an operator action, not startup.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time
import uuid

from memd.stores import store_name

OPERATIONS = {"read", "recall", "save", "reindex", "stats"}
principal: ContextVar[str | None] = ContextVar("control_token_id", default=None)
PUBLIC = ("id", "label", "owner", "purpose", "stores", "operations", "legacy",
          "created", "expires", "revoked", "revision", "rotated_from", "last_seen")


class ControlError(ValueError):
    pass


class Conflict(ControlError):
    pass


class Denied(ControlError):
    pass


def configured() -> bool:
    return bool(os.environ.get("MEMD_CONTROL_DB", "").strip())


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def public(row) -> dict:
    result = {key: row[key] for key in PUBLIC}
    result["personal_subject"] = row["personal_subject"]
    result["personal_store"] = row["personal_store"]
    for key in ("stores", "operations"):
        result[key] = json.loads(result[key])
    result["legacy"] = bool(result["legacy"])
    result["status"] = ("revoked" if result["revoked"] is not None else
                        "expired" if result["expires"] is not None and result["expires"] <= time.time()
                        else "active")
    return result


class Registry:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or os.environ["MEMD_CONTROL_DB"])
        root = os.environ.get("MEMD_STORES_ROOT")
        if root and self.path.resolve().is_relative_to(Path(root).resolve()):
            raise ControlError("Control database must be outside memory stores")

    @contextmanager
    def connection(self, write=False):
        # Missing registry after cutover is an outage, never an empty re-import.
        db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=rw", uri=True, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Exclusive create protects accidental replacement and sets mode before SQLite opens.
        fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO metadata VALUES ('schema', '2');
                CREATE TABLE tokens(
                    id TEXT PRIMARY KEY, verifier TEXT NOT NULL UNIQUE,
                    label TEXT NOT NULL, owner TEXT NOT NULL, purpose TEXT NOT NULL,
                    stores TEXT NOT NULL, operations TEXT NOT NULL, legacy INTEGER NOT NULL,
                    created REAL NOT NULL, expires REAL, revoked REAL,
                    revision INTEGER NOT NULL DEFAULT 1, rotated_from TEXT REFERENCES tokens(id),
                    last_seen REAL, personal_subject TEXT, personal_store TEXT);
                CREATE TABLE audit(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL,
                    actor TEXT NOT NULL, action TEXT NOT NULL, token_id TEXT,
                    operation_id TEXT NOT NULL UNIQUE, detail TEXT NOT NULL);
                CREATE TABLE sessions(
                    id TEXT PRIMARY KEY, data TEXT NOT NULL, created REAL NOT NULL,
                    touched REAL NOT NULL, expires REAL NOT NULL);
            """)

    def upgrade(self):
        """Add immutable personal identity bindings; caller backs up before rollout."""
        with self.connection(write=True) as db:
            version = db.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()[0]
            if version == "2":
                return
            if version != "1":
                raise ControlError("Unsupported token registry schema")
            db.execute("ALTER TABLE tokens ADD COLUMN personal_subject TEXT")
            db.execute("ALTER TABLE tokens ADD COLUMN personal_store TEXT")
            db.execute("UPDATE metadata SET value='2' WHERE key='schema'")

    @staticmethod
    def _audit(db, actor, action, token_id, operation_id, detail=None):
        db.execute("INSERT INTO audit(at,actor,action,token_id,operation_id,detail) VALUES(?,?,?,?,?,?)",
                   (time.time(), actor, action, token_id, operation_id, json.dumps(detail or {})))

    @staticmethod
    def _once(db, operation_id):
        try:
            uuid.UUID(operation_id)
        except (ValueError, TypeError, AttributeError):
            raise ControlError("A UUID operation ID is required") from None
        row = db.execute("SELECT token_id FROM audit WHERE operation_id=?", (operation_id,)).fetchone()
        if row:
            raise Conflict("Operation already completed; inspect the token list. Secrets cannot be recovered.")

    @staticmethod
    def _fields(label, owner, purpose, stores, operations, days):
        for name, value, limit in (("label", label, 100), ("owner", owner, 200), ("purpose", purpose, 500)):
            if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(c) < 32 for c in value):
                raise ControlError(f"{name} must be nonempty text, at most {limit} characters")
        if not isinstance(stores, list) or not 1 <= len(stores) <= 50:
            raise ControlError("Choose 1 to 50 explicit stores")
        try:
            stores = sorted({store_name(s) for s in stores})
        except ValueError:
            raise ControlError("Invalid store name") from None
        if not isinstance(operations, list) or not operations or not all(isinstance(o, str) for o in operations) or not set(operations) <= OPERATIONS:
            raise ControlError("Choose supported operations")
        operations = sorted(set(operations))
        if len(stores) > 1 and not set(operations) <= {"reindex", "stats"}:
            raise ControlError("Multiple stores are allowed only for reindex/stats maintenance")
        if type(days) is not int or not 1 <= days <= 365:
            raise ControlError("Expiry must be 1 to 365 days")
        return stores, operations

    def issue(self, *, actor, operation_id, label, owner, purpose, stores, operations, days=90):
        stores, operations = self._fields(label, owner, purpose, stores, operations, days)
        with self.connection(write=True) as db:
            self._once(db, operation_id)
            return self._issue(db, actor, operation_id, label, owner, purpose, stores, operations, days)

    def _issue(self, db, actor, operation_id, label, owner, purpose, stores, operations, days, rotated_from=None,
               personal_subject=None, personal_store=None):
        token_id = uuid.uuid4().hex
        credential = "memd_" + token_id + "." + secrets.token_urlsafe(32)
        now = time.time()
        db.execute("""INSERT INTO tokens(id,verifier,label,owner,purpose,stores,operations,legacy,created,expires,rotated_from)
                      VALUES(?,?,?,?,?,?,?,0,?,?,?)""",
                   (token_id, digest(credential), label.strip(), owner.strip(), purpose.strip(),
                    json.dumps(stores), json.dumps(operations), now, now + days * 86400, rotated_from))
        if personal_subject:
            db.execute("UPDATE tokens SET personal_subject=?,personal_store=? WHERE id=?",
                       (personal_subject, personal_store, token_id))
        self._audit(db, actor, "rotate" if rotated_from else "create", token_id, operation_id,
                    {"stores": stores, "operations": operations, "rotated_from": rotated_from})
        row = db.execute("SELECT * FROM tokens WHERE id=?", (token_id,)).fetchone()
        return {"token": public(row), "secret": credential}

    def import_legacy(self, tokens: dict[str, str], actor="cli:migration"):
        with self.connection(write=True) as db:
            if db.execute("SELECT 1 FROM metadata WHERE key='legacy_imported'").fetchone():
                raise Conflict("Legacy import has already completed")
            if db.execute("SELECT 1 FROM tokens LIMIT 1").fetchone():
                raise Conflict("Import requires an empty registry")
            for credential, label in tokens.items():
                if len(credential) < 32:
                    raise ControlError("Legacy credential is shorter than 32 characters")
                token_id = uuid.uuid4().hex
                db.execute("""INSERT INTO tokens(id,verifier,label,owner,purpose,stores,operations,legacy,created)
                              VALUES(?,?,?,?,?,'[]',?,1,?)""",
                           (token_id, digest(credential), label, "Legacy — review owner",
                            "Migrated with existing access; replace with scoped automation or OIDC",
                            json.dumps(sorted(OPERATIONS)), time.time()))
                self._audit(db, actor, "import", token_id, str(uuid.uuid4()), {"label": label})
            db.execute("INSERT INTO metadata VALUES('legacy_imported', ?)", (str(time.time()),))
        return len(tokens)

    def list(self):
        with self.connection() as db:
            return [public(r) for r in db.execute("SELECT * FROM tokens ORDER BY created DESC,id")]

    def personal_list(self, subject, store):
        with self.connection() as db:
            return [public(r) for r in db.execute(
                "SELECT * FROM tokens WHERE personal_subject=? AND personal_store=? ORDER BY created DESC,id",
                (subject, store))]

    def issue_personal(self, *, subject, store, operation_id, label, days=30):
        from memd.owners import personal_entitled
        if not isinstance(subject, str) or not subject or not personal_entitled(subject, store):
            raise Denied("Your directory membership could not be verified. Try again after the next directory sync.")
        if type(days) is not int or days not in (7, 30, 90):
            raise ControlError("Choose a 7, 30 or 90 day expiry")
        stores, operations = self._fields(label, store, "Personal memory access", [store], ["read","recall","save"], days)
        with self.connection(write=True) as db:
            self._once(db, operation_id)
            count = db.execute("SELECT COUNT(*) FROM tokens WHERE personal_subject=? AND revoked IS NULL AND expires>?",
                               (subject, time.time())).fetchone()[0]
            if count >= 10:
                raise ControlError("You already have 10 active personal tokens. Revoke an unused token first.")
            return self._issue(db, "user:"+subject, operation_id, label, store, "Personal memory access",
                               stores, operations, days, personal_subject=subject, personal_store=store)

    def revoke_personal(self, token_id, *, subject, store, operation_id, revision):
        with self.connection(write=True) as db:
            row = db.execute("SELECT * FROM tokens WHERE id=? AND personal_subject=? AND personal_store=?",
                             (token_id, subject, store)).fetchone()
            if not row:
                raise Denied("Personal token not found")
            self._once(db, operation_id)
            self._revision(db, token_id, revision)
            if row["revoked"] is not None:
                raise Conflict("Token is already revoked")
            db.execute("UPDATE tokens SET revoked=?,revision=revision+1 WHERE id=?", (time.time(), token_id))
            self._audit(db, "user:"+subject, "revoke", token_id, operation_id)
            return public(db.execute("SELECT * FROM tokens WHERE id=?", (token_id,)).fetchone())

    def events(self, limit=1000):
        with self.connection() as db:
            return [dict(r) for r in db.execute("SELECT * FROM audit ORDER BY seq DESC LIMIT ?", (min(limit, 10000),))]

    @staticmethod
    def _active(row):
        if not row or row["revoked"] is not None or (row["expires"] is not None and row["expires"] <= time.time()):
            return False
        if row["personal_subject"]:
            from memd.owners import personal_entitled
            return personal_entitled(row["personal_subject"], row["personal_store"])
        return True

    def authenticate(self, credential):
        verifier = digest(credential)
        with self.connection(write=True) as db:
            row = db.execute("SELECT * FROM tokens WHERE verifier=?", (verifier,)).fetchone()
            if not self._active(row) or not hmac.compare_digest(row["verifier"], verifier):
                return None
            # Coalesce observed-use writes to one per minute per credential.
            if row["last_seen"] is None or row["last_seen"] < time.time() - 60:
                db.execute("UPDATE tokens SET last_seen=? WHERE id=?", (time.time(), row["id"]))
            return public(row)

    def authorize(self, token_id, operation, requested):
        with self.connection() as db:
            row = db.execute("SELECT * FROM tokens WHERE id=?", (token_id,)).fetchone()
        if not self._active(row):
            raise Denied("Token is expired or revoked")
        if row["legacy"]:
            return requested
        item = public(row)
        if operation not in item["operations"]:
            raise Denied("Token does not permit this operation")
        selected = requested or (item["stores"][0] if len(item["stores"]) == 1 else None)
        if selected not in item["stores"]:
            raise Denied("Token does not permit this store; specify an allowed store")
        return selected

    def revoke(self, token_id, *, actor, operation_id, revision):
        with self.connection(write=True) as db:
            self._once(db, operation_id)
            row = self._revision(db, token_id, revision)
            if row["revoked"] is not None:
                raise Conflict("Token is already revoked")
            db.execute("UPDATE tokens SET revoked=?,revision=revision+1 WHERE id=?", (time.time(), token_id))
            self._audit(db, actor, "revoke", token_id, operation_id)
            return public(db.execute("SELECT * FROM tokens WHERE id=?", (token_id,)).fetchone())

    @staticmethod
    def _revision(db, token_id, revision):
        row = db.execute("SELECT * FROM tokens WHERE id=?", (token_id,)).fetchone()
        if row is None:
            raise ControlError("Unknown token")
        if type(revision) is not int or row["revision"] != revision:
            raise Conflict("Token changed; reload before retrying")
        return row

    def rotate(self, token_id, *, actor, operation_id, revision, overlap_hours=24, days=90):
        if type(overlap_hours) is not int or not 0 <= overlap_hours <= 168:
            raise ControlError("Overlap must be 0 to 168 hours")
        with self.connection(write=True) as db:
            self._once(db, operation_id)
            row = self._revision(db, token_id, revision)
            if row["legacy"]:
                raise ControlError("Legacy tokens must be replaced with an explicitly scoped token or OIDC")
            if not self._active(row):
                raise Conflict("Only active tokens can rotate")
            if row["personal_subject"] and (type(days) is not int or not 1 <= days <= 90):
                raise ControlError("Personal token expiry cannot exceed 90 days")
            item = public(row)
            stores, operations = self._fields(item["label"], item["owner"], item["purpose"], item["stores"], item["operations"], days)
            expiry = min(row["expires"], time.time() + overlap_hours * 3600)
            db.execute("UPDATE tokens SET expires=?,revision=revision+1 WHERE id=?", (expiry, token_id))
            result = self._issue(db, actor, operation_id, item["label"], item["owner"], item["purpose"], stores, operations, days, token_id)
            if row["personal_subject"]:
                db.execute("UPDATE tokens SET personal_subject=?,personal_store=? WHERE id=?",
                           (row["personal_subject"],row["personal_store"],result["token"]["id"]))
                result["token"] = public(db.execute("SELECT * FROM tokens WHERE id=?",(result["token"]["id"],)).fetchone())
            self._audit(db, actor, "rotation-overlap", token_id, str(uuid.uuid4()), {"expires": expiry, "successor": result["token"]["id"]})
            return result

    def edit(self, token_id, *, actor, operation_id, revision, label, owner, purpose):
        self._fields(label, owner, purpose, ["validation"], ["read"], 90)
        with self.connection(write=True) as db:
            self._once(db, operation_id)
            row = self._revision(db, token_id, revision)
            if row["personal_subject"] and owner.strip() != row["personal_store"]:
                raise ControlError("A personal token's owner is fixed by its verified identity")
            db.execute("UPDATE tokens SET label=?,owner=?,purpose=?,revision=revision+1 WHERE id=?",
                       (label.strip(), owner.strip(), purpose.strip(), token_id))
            self._audit(db, actor, "edit-metadata", token_id, operation_id)
            return public(db.execute("SELECT * FROM tokens WHERE id=?", (token_id,)).fetchone())

    def backup(self, destination):
        target = Path(destination)
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        with self.connection() as source, sqlite3.connect(target) as dest:
            source.backup(dest)
            # Browser sessions and unfinished logins must never survive restore.
            dest.execute("DELETE FROM sessions")
            assert dest.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    def session_create(self, data, lifetime=28800):
        cookie = secrets.token_urlsafe(32)
        now = time.time()
        with self.connection(write=True) as db:
            db.execute("DELETE FROM sessions WHERE expires<? OR touched<?", (now, now - 900))
            db.execute("INSERT INTO sessions VALUES(?,?,?,?,?)", (digest(cookie), json.dumps(data), now, now, now + lifetime))
        return cookie

    def session_get(self, cookie):
        if not cookie:
            return None
        now = time.time()
        with self.connection(write=True) as db:
            row = db.execute("SELECT * FROM sessions WHERE id=? AND expires>? AND touched>?",
                             (digest(cookie), now, now - 900)).fetchone()
            if row:
                db.execute("UPDATE sessions SET touched=? WHERE id=?", (now, row["id"]))
                return json.loads(row["data"])
        return None

    def session_delete(self, cookie):
        with self.connection(write=True) as db:
            db.execute("DELETE FROM sessions WHERE id=?", (digest(cookie or ""),))

    def session_consume(self, cookie):
        # Atomic one-use login state, including parallel/replayed callbacks.
        with self.connection(write=True) as db:
            row = db.execute("DELETE FROM sessions WHERE id=? AND expires>? AND touched>? RETURNING data",
                             (digest(cookie or ""), time.time(), time.time() - 900)).fetchone()
            return json.loads(row[0]) if row else None


def enforce(operation, requested):
    token_id = principal.get()
    if token_id is not None:
        return Registry().authorize(token_id, operation, requested)
    return requested
