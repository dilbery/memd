"""Managed Git stores and bounded, non-destructive Obsidian vault connections."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import contextlib
import dataclasses
import fcntl
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import sqlite3
import subprocess
import threading
import time
from urllib.parse import urlsplit

from memd import control

_jobs = ThreadPoolExecutor(max_workers=2, thread_name_prefix="memd-source")


def settings_for_clone(clone):
    target = Path(clone).resolve()
    for row in control.stores().values():
        if Path(row["config"]["clone_path"]).resolve() == target:
            return row
    return None


def relative_path(value, *, field="Folder"):
    if not isinstance(value,str) or not value or Path(value).is_absolute() or any(p in {"..","."} or p.startswith(".") for p in Path(value).parts):
        raise ValueError(f"{field} must be a relative path without hidden directories or traversal.")
    return value.strip("/")


def vault_path(value):
    if not isinstance(value,str) or not value:
        raise ValueError("Specify the vault folder mounted on this server.")
    path = Path(value).resolve()
    allowed = [Path(p).resolve() for p in os.environ.get("MEMD_VAULT_ROOTS",str(control.root()/"vaults")).split(os.pathsep) if p]
    if not any(path == root or path.is_relative_to(root) for root in allowed):
        raise ValueError("Vault path must be inside an administrator-mounted vault root: " + ", ".join(map(str,allowed)))
    if not path.is_dir():
        raise ValueError("That vault folder is not mounted or does not exist on this server.")
    return path


def valid_remote(value):
    if not isinstance(value,str) or len(value)>2048 or any(c.isspace() for c in value):
        raise ValueError("Provide a Git HTTPS or SSH repository URL.")
    parsed = urlsplit(value)
    if parsed.scheme in {"https","ssh"} and parsed.hostname and not parsed.password:
        if parsed.scheme == "https" and parsed.username:
            raise ValueError("Put credentials in the credential field, not the repository URL.")
        return value
    if re.fullmatch(r"[a-zA-Z0-9_.-]+@[a-zA-Z0-9.-]+:[a-zA-Z0-9_./-]+",value):
        return value
    raise ValueError("Use https://host/repo.git or an SSH Git URL; local and executable transports are not accepted.")


def validate_config(kind, incoming, previous=None):
    cfg = dict(previous or {})
    if not isinstance(incoming,dict):
        raise ValueError("Settings must be an object.")
    for key in ("repo_ssh","branch","vault_path","write_folder","git_username","embed_url","embed_model","rerank_url","rerank_model"):
        if key in incoming:
            value = incoming[key]
            if value is not None and (not isinstance(value,str) or len(value)>2048):
                raise ValueError(f"Invalid {key}.")
            cfg[key] = value or ""
    if cfg.get("repo_ssh"):
        valid_remote(cfg["repo_ssh"])
    if kind == "git" and not cfg.get("repo_ssh"):
        raise ValueError("A Git store needs a repository URL.")
    branch = cfg.get("branch", "")
    if branch and (not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_./-]{0,120}",branch) or ".." in branch or "//" in branch):
        raise ValueError("Invalid Git branch.")
    for key in ("embed_url","rerank_url"):
        value = cfg.get(key)
        if value and (urlsplit(value).scheme not in {"http","https"} or not urlsplit(value).hostname or urlsplit(value).password):
            raise ValueError("Model services need an HTTP(S) URL without embedded credentials.")
    for key,default in (("include",["**/*.md"]),("exclude",[])):
        values = incoming.get(key,cfg.get(key,default))
        if not isinstance(values,list) or len(values)>100 or any(not isinstance(v,str) or len(v)>200 or v.startswith("/") or ".." in v.split("/") for v in values):
            raise ValueError("Folder filters must be a list of relative glob patterns.")
        cfg[key] = values
    interval = incoming.get("sync_interval",cfg.get("sync_interval",300))
    if not isinstance(interval,int) or interval not in range(0,86401) or (interval and interval<60):
        raise ValueError("Sync interval must be 0 (manual) or 60–86400 seconds.")
    cfg["sync_interval"] = interval
    if "publish_review" in incoming:
        # memd.share: a note published INTO this store waits in its review inbox
        # (true, the default) or is saved directly (false).
        if not isinstance(incoming["publish_review"], bool):
            raise ValueError("publish_review must be true or false.")
        cfg["publish_review"] = incoming["publish_review"]
    if "key_file" in incoming:
        value = incoming["key_file"]
        if value and (not isinstance(value,str) or len(value)>4096 or not Path(value).is_absolute()):
            raise ValueError("The encryption key file must be an absolute path on this server.")
        if value:
            from memd.crypt import load_key
            load_key(value)   # refuses a missing, malformed or group/world-readable key
        cfg["key_file"] = value or ""
    if kind == "obsidian" and cfg.get("key_file"):
        raise ValueError("Obsidian vault stores cannot be encrypted: the vault itself holds the notes in plaintext.")
    if kind == "obsidian":
        cfg["vault_path"] = str(vault_path(cfg.get("vault_path")))
        cfg["write_folder"] = relative_path(cfg.get("write_folder") or "Memories")
    return cfg


def write_credential(profile,cfg,incoming):
    if "secret" not in incoming or not incoming["secret"]:
        return
    value = incoming["secret"]
    if not isinstance(value,str) or len(value)>32768:
        raise ValueError("Invalid credential.")
    kind = incoming.get("credential_type","https")
    if kind not in {"https","ssh"}:
        raise ValueError("Credential type must be HTTPS token or SSH private key.")
    if kind == "https" and any(c in value for c in "\r\n"):
        raise ValueError("An HTTPS token must be a single line.")
    path = control.root()/"credentials"/profile
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    atomic_write(path,value.encode(),mode=0o600)
    cfg["credential"],cfg["credential_type"] = str(path),kind


def atomic_write(path,data,mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary = path.with_name(".memd-"+secrets.token_hex(8))
    try:
        fd = os.open(temporary,os.O_CREAT|os.O_EXCL|os.O_WRONLY,mode)
        with os.fdopen(fd,"wb") as out:
            out.write(data);out.flush();os.fsync(out.fileno())
        os.replace(temporary,path)
    finally:
        temporary.unlink(missing_ok=True)


def git_env(cfg):
    env = {**os.environ,"GIT_TERMINAL_PROMPT":"0","GIT_CONFIG_NOSYSTEM":"1"}
    credential = cfg.get("credential")
    if credential and cfg.get("credential_type") == "ssh":
        hosts = control.root()/"known_hosts"
        env["GIT_SSH_COMMAND"] = "ssh -F /dev/null -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="+shlex.quote(str(hosts))+" -i "+shlex.quote(credential)
    elif credential and cfg.get("credential_type") == "https":
        helper = control.root()/"git-askpass"
        if not helper.exists():
            atomic_write(helper,b'#!/usr/bin/env python3\nimport os,sys\nfrom pathlib import Path\nprint(os.environ.get("MEMD_GIT_USER","git") if "username" in sys.argv[1].lower() else Path(os.environ["MEMD_GIT_SECRET_FILE"]).read_text().strip())\n',mode=0o700)
        env.update(GIT_ASKPASS=str(helper),MEMD_GIT_SECRET_FILE=credential,MEMD_GIT_USER=cfg.get("git_username") or "git")
    return env


def git(cfg,*args,clone=None,timeout=60):
    command = ["git","-c","core.hooksPath=/dev/null","-c","protocol.file.allow=never"]
    if clone:
        command += ["-C",str(clone)]
    try:
        return subprocess.run(command+list(args),env=git_env(cfg),capture_output=True,text=True,check=True,timeout=timeout).stdout.strip()
    except subprocess.CalledProcessError as exc:
        message = (exc.stderr or "Git command failed.")[-1200:]
        secret = cfg.get("credential")
        if secret and Path(secret).exists():
            message = message.replace(Path(secret).read_text().strip(),"[credential]")
        raise ValueError(message) from None


def initialize_clone(cfg):
    clone = Path(cfg["clone_path"])
    if (clone/".git").exists():
        return
    clone.parent.mkdir(parents=True,exist_ok=True)
    if cfg.get("repo_ssh"):
        args = ["clone"]
        if cfg.get("branch"):
            args += ["--branch",cfg["branch"]]
        git(cfg,*args,"--",cfg["repo_ssh"],str(clone))
    else:
        clone.mkdir(parents=True,exist_ok=True)
        git(cfg,"init","-b","main",clone=clone)
    git(cfg,"config","user.name","memd",clone=clone)
    git(cfg,"config","user.email","memd@localhost",clone=clone)
    git(cfg,"config","core.hooksPath","/dev/null",clone=clone)
    if cfg.get("key_file"):
        # A new encrypted store starts with its marker, so no note is ever
        # written to it in plaintext. A clone that already holds plaintext
        # notes stays refused until `mem-crypt encrypt` converts it.
        from memd.codec import MARKER,marker_bytes,read_marker
        from memd.crypt import load_key
        has_notes = any(p.suffix == ".md" and ".git" not in p.relative_to(clone).parts for p in clone.rglob("*.md"))
        if read_marker(clone) is None and not has_notes:
            atomic_write(clone/MARKER,marker_bytes(load_key(cfg["key_file"])),mode=0o644)
            git(cfg,"add","--",MARKER,clone=clone)
            git(cfg,"commit","-m","Initialize encrypted memory store",clone=clone)
    if not subprocess.run(["git","-C",str(clone),"rev-parse","--verify","HEAD"],capture_output=True).returncode == 0:
        git(cfg,"commit","--allow-empty","-m","Initialize memory store",clone=clone)


def create_store(payload,*,actor):
    profile = control.identifier(payload.get("id"),"Store ID")
    from memd.profiles import registry
    if profile in registry():
        raise ValueError("That store already exists.")
    kind = payload.get("kind","local")
    if kind not in {"local","git","obsidian"}:
        raise ValueError("Choose local, Git or Obsidian storage.")
    cfg = validate_config(kind,payload.get("config",{}))
    directory = control.root()/"stores"/profile
    cfg.update(clone_path=str(directory/"clone"),db_path=str(directory/"index.db"),managed=True)
    # Only the administrator chooses remote/vault connections; paths for the
    # actual memory store and index are always generated server-side.
    write_credential(profile,cfg,payload.get("config",{}))
    control.put_store(profile,payload.get("name",profile),kind,cfg,actor=actor,create=True)
    start_job(profile,"sync",actor)
    return control.public_store(control.store(profile))


def update_store(profile,payload,*,actor):
    old = control.store(profile)
    if not old:
        raise ValueError("Unknown store.")
    with control.db() as c:
        if c.execute("SELECT 1 FROM jobs WHERE store_id=? AND state IN ('queued','running')",(profile,)).fetchone():
            raise ValueError("Wait for the current store operation to finish before changing its settings.")
    if payload.get("kind",old["kind"]) != old["kind"]:
        raise ValueError("Create a new store to change its storage type.")
    incoming = payload.get("config",{})
    cfg = validate_config(old["kind"],incoming,old["config"])
    if old["kind"] == "obsidian" and any(cfg.get(k) != old["config"].get(k) for k in ("vault_path","write_folder")):
        raise ValueError("Create a new store to change the vault or write folder; existing mappings are preserved.")
    for key in ("embed_url","embed_model"):
        # Unset and blank both mean the server default: a store registered from
        # the environment has no such key, and Settings sends "".
        if (old["config"].get(key) or "") != (cfg.get(key) or "") and Path(cfg["db_path"]).exists():
            from memd.index import open_db
            index = open_db(Path(cfg["db_path"]))
            try:
                if index.execute("SELECT COUNT(*) FROM notes").fetchone()[0]:
                    raise ValueError("An embedding model change needs a new store and reindex to avoid mixing incompatible vectors.")
            finally:
                index.close()
    write_credential(profile,cfg,incoming)
    control.put_store(profile,payload.get("name",old["name"]),old["kind"],cfg,actor=actor)
    return control.public_store(control.store(profile))


def included(relative,cfg):
    parts = Path(relative).parts
    if any(p.startswith(".") for p in parts):
        return False
    if cfg.get("write_folder") and Path(relative).is_relative_to(Path(cfg["write_folder"])):
        return True
    match = lambda pattern: fnmatch.fnmatchcase(relative,pattern) or (pattern.startswith("**/") and fnmatch.fnmatchcase(relative,pattern[3:]))
    return any(match(p) for p in cfg.get("include",["**/*.md"])) and not any(match(p) for p in cfg.get("exclude",[]))


def content_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def manifest(cfg):
    path = Path(cfg["clone_path"])/".git"/"memd-vault.json"
    return json.loads(path.read_text()) if path.exists() else {"files":{},"pending":{}}


def write_manifest(cfg,state):
    atomic_write(Path(cfg["clone_path"])/".git"/"memd-vault.json",json.dumps(state).encode())


def import_vault(row):
    from memd.store import parse_text,dump_note
    cfg = row["config"]
    vault = vault_path(cfg["vault_path"])
    clone = Path(cfg["clone_path"])
    state = manifest(cfg)
    if state.get("pending"):
        raise ValueError("Vault export conflict: resolve the pending files before synchronising: "+", ".join(state["pending"]))
    files = {}
    for path in sorted(vault.rglob("*.md")):
        rel = path.relative_to(vault).as_posix()
        if not included(rel,cfg) or path.is_symlink() or any(p.is_symlink() for p in path.parents if p != vault and p.is_relative_to(vault)):
            continue
        if not path.resolve().is_relative_to(vault):
            continue
        if path.stat().st_size > 4*1024*1024:
            raise ValueError(f"Note exceeds the 4 MiB import limit: {rel}")
        files[rel] = content_hash(path)
        if state["files"].get(rel) == files[rel] and (clone/rel).exists():
            continue
        note = parse_text(path.read_text(encoding="utf-8"),path=rel)
        note.profile = row["id"]
        if not Path(rel).is_relative_to(Path(cfg["write_folder"])):
            note.slug = "vault-"+hashlib.sha256(rel.encode()).hexdigest()[:24]
        note.metadata["vault_path"] = rel
        atomic_write(clone/rel,dump_note(note).encode())
    for rel in set(state["files"])-set(files):
        target = clone/rel
        if target.resolve().is_relative_to(clone.resolve()):
            target.unlink(missing_ok=True)
    git(cfg,"add","--all",clone=clone)
    dirty = git(cfg,"diff","--cached","--name-only",clone=clone)
    if dirty:
        git(cfg,"commit","-m","Sync Obsidian vault",clone=clone)
    write_manifest(cfg,{"files":files,"pending":{}})
    return len(files)


def guard_vault_write(cfg,note):
    row = control.store(cfg.profile)
    if row and row["kind"] == "obsidian" and note:
        relative = Path(note.path).relative_to(cfg.clone)
        if not relative.is_relative_to(Path(row["config"]["write_folder"])):
            raise PermissionError("Imported vault notes are read-only. Save agent memories in the configured memory folder.")


def write_directory(cfg):
    row = control.store(cfg.profile)
    if row and row["kind"] == "obsidian":
        path = cfg.clone/row["config"]["write_folder"]
        path.mkdir(parents=True,exist_ok=True)
        return path
    return cfg.clone


def export_note(cfg,path):
    row = control.store(cfg.profile)
    if not row or row["kind"] != "obsidian":
        return None
    settings = row["config"]
    rel = Path(path).relative_to(cfg.clone).as_posix()
    state = manifest(settings)
    vault = vault_path(settings["vault_path"])
    target = vault/rel
    if not target.resolve().is_relative_to(vault) or target.is_symlink():
        state.setdefault("pending",{})[rel] = content_hash(Path(path))
        write_manifest(settings,state)
        raise PermissionError("Vault export path is outside the mounted vault. Saved copy retained in memd.")
    expected = state["files"].get(rel)
    if content_hash(target) != expected:
        state.setdefault("pending",{})[rel] = content_hash(Path(path))
        write_manifest(settings,state)
        return "Saved in memd; vault export has a conflicting edit in "+rel+". Both copies were preserved."
    atomic_write(target,Path(path).read_bytes())
    state["files"][rel] = content_hash(target)
    state.get("pending",{}).pop(rel,None)
    write_manifest(settings,state)
    return None


def conflicts(profile):
    row = control.store(profile)
    if not row or row["kind"] != "obsidian":
        raise ValueError("This is not an Obsidian store.")
    cfg = row["config"]
    vault = vault_path(cfg["vault_path"])
    clone = Path(cfg["clone_path"])
    result = []
    with source_lock(profile):
        for rel in manifest(cfg).get("pending",{}):
            relative_path(rel)
            source = vault/rel
            if not source.resolve().is_relative_to(vault) or not (clone/rel).resolve().is_relative_to(clone):
                raise ValueError("A conflicting path is a symlink outside the store; fix its mount before resolving.")
            result.append({"path":rel,"vault_hash":content_hash(source),"memd_hash":content_hash(clone/rel),
                           "vault_preview":source.read_text()[:4000] if source.exists() else "(file absent)",
                           "memd_preview":(clone/rel).read_text()[:4000]})
    return result


def resolve_conflict(profile,payload,*,actor):
    row = control.store(profile)
    if not row or row["kind"] != "obsidian":
        raise ValueError("This is not an Obsidian store.")
    cfg = row["config"]; rel = relative_path(payload.get("path"))
    choice = payload.get("keep")
    if choice not in {"vault","memd"}:
        raise ValueError("Choose which copy to keep.")
    from memd.store import clone_lock,parse_text,dump_note
    clone = Path(cfg["clone_path"]); vault = vault_path(cfg["vault_path"])
    source,target = vault/rel,clone/rel
    if not source.resolve().is_relative_to(vault) or not target.resolve().is_relative_to(clone) or not Path(rel).is_relative_to(Path(cfg["write_folder"])):
        raise ValueError("Conflict path is outside the configured write folder.")
    with source_lock(profile),clone_lock(clone):
        state = manifest(cfg)
        if rel not in state.get("pending",{}):
            raise ValueError("That conflict is already resolved.")
        if content_hash(source)!=payload.get("vault_hash") or content_hash(target)!=payload.get("memd_hash"):
            raise ValueError("A copy changed since the preview. Reload the conflict before choosing.")
        backup = clone/".git"/"memd-conflicts"/(str(time.time_ns())+"-"+hashlib.sha256(rel.encode()).hexdigest()[:12])
        backup.mkdir(parents=True)
        atomic_write(backup/"memd.md",target.read_bytes())
        if source.exists():
            atomic_write(backup/"vault.md",source.read_bytes())
        if choice == "memd":
            atomic_write(source,target.read_bytes())
        elif source.exists():
            note = parse_text(source.read_text(),path=rel)
            note.profile = profile
            atomic_write(target,dump_note(note).encode())
        else:
            target.unlink()
        state["pending"].pop(rel)
        if source.exists(): state["files"][rel] = content_hash(source)
        else: state["files"].pop(rel,None)
        git(cfg,"add","--",rel,clone=clone)
        if git(cfg,"diff","--cached","--name-only","--",rel,clone=clone):
            git(cfg,"commit","-m","Resolve Obsidian conflict","--",rel,clone=clone)
        write_manifest(cfg,state)
    from memd.config import Config
    from memd.refresh import ensure_lexical,request_refresh
    resolved = Config.from_env({**os.environ,"MEMD_PROFILE":profile},env_file=None)
    ensure_lexical(resolved);request_refresh(resolved)
    with control.db() as c:
        control.audit(c,actor,"vault.resolve."+choice,profile+"/"+rel)


@contextlib.contextmanager
def source_lock(profile):
    directory = control.root()/"locks"
    directory.mkdir(exist_ok=True)
    with (directory/(profile+".lock")).open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        yield


def run_job(profile,action):
    row = control.store(profile)
    cfg = row["config"]
    if action == "test":
        if row["kind"] == "obsidian":
            vault_path(cfg["vault_path"])
            return "Vault is accessible."
        if cfg.get("repo_ssh"):
            git(cfg,"ls-remote","--",cfg["repo_ssh"])
            return "Git authentication and repository access succeeded."
        return "Local memory store is configured."
    from memd.config import Config
    from memd.store import clone_lock
    from memd import refresh
    with source_lock(profile):
        if cfg.get("managed"):
            initialize_clone(cfg)
        resolved = Config.from_env({**os.environ,"MEMD_PROFILE":profile},env_file=None)
        with clone_lock(resolved.clone):
            if row["kind"] == "obsidian":
                if cfg.get("repo_ssh"):
                    git(cfg,"pull","--ff-only",clone=resolved.clone)
                count = import_vault(row)
                if cfg.get("repo_ssh"):
                    git(cfg,"push",clone=resolved.clone)
            elif cfg.get("repo_ssh") and action == "sync":
                git(cfg,"remote","set-url","origin",cfg["repo_ssh"],clone=resolved.clone)
                if cfg.get("branch"):
                    current = git(cfg,"branch","--show-current",clone=resolved.clone)
                    if current != cfg["branch"]:
                        raise ValueError("The configured branch differs from the active clone. Create a separate store for another branch.")
                git(cfg,"pull","--ff-only",clone=resolved.clone)
        count = refresh.ensure_lexical(resolved)
        refresh.request_refresh(resolved)
    return f"Keyword index ready: {count} notes. Semantic refresh scheduled."


def _job(jid,profile,action):
    with control.db() as c:
        c.execute("UPDATE jobs SET state='running' WHERE id=?",(jid,))
    try:
        detail = run_job(profile,action)
        state = "complete"
    except Exception as exc:
        detail,state = str(exc)[:1500],"failed"
    with control.db() as c:
        c.execute("UPDATE jobs SET state=?,detail=?,finished=? WHERE id=?",(state,detail,time.time(),jid))


def start_job(profile,action,actor):
    jid = secrets.token_hex(12)
    try:
        with control.db() as c:
            if c.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0] >= 20:
                raise ValueError("The work queue is full. Try again shortly.")
            c.execute("INSERT INTO jobs(id,store_id,action,state,created) VALUES(?,?,?,'queued',?)",(jid,profile,action,time.time()))
            control.audit(c,actor,"store."+action,profile)
    except sqlite3.IntegrityError:
        raise ValueError("An operation is already running for this store.") from None
    _jobs.submit(_job,jid,profile,action)
    return jid


def scheduler(stop):
    while not stop.wait(30):
        try:
            for profile,row in control.stores().items():
                interval = row["config"].get("sync_interval",0)
                if row["kind"] not in {"git","obsidian"} or not interval:
                    continue
                with control.db() as c:
                    last = c.execute("SELECT MAX(created) FROM jobs WHERE store_id=? AND action='sync'",(profile,)).fetchone()[0] or 0
                if time.time()-last >= interval:
                    try:
                        start_job(profile,"sync","system")
                    except ValueError:
                        pass
        except Exception:
            import logging
            logging.getLogger(__name__).exception("Source scheduler failed")
