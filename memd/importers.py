"""mem-import: bring memories from elsewhere into the review inbox.

Every importer files *candidates* (memd.inbox.propose, channel ``import:<kind>``,
proposer ``mem-import``); nothing reaches the Git store until a person approves
it. Four sources:

  * ``chatgpt EXPORT``: a ChatGPT data export (the zip, its extracted folder,
    or one JSON file). Saved memories (any ``memor*.json``/``.txt`` member)
    become candidates directly; ``conversations.json`` only with ``--distill``,
    through inbox distillation and the chat model (memd.llm).
  * ``claude EXPORT``: a Claude data export. Memory (``memories.json``, split
    into its headed sections) and project knowledge (``projects.json``:
    description, instructions and documents) directly; ``conversations.json``
    only with ``--distill``.
  * ``markdown DIR``: a folder of Markdown files. Title from frontmatter, the
    first H1 or the file name; tags and dates from frontmatter, or the date of
    the file's last commit when the folder is in a Git work tree (whose
    .gitignore is then honoured). Hidden paths, ``--exclude`` globs, symlinks,
    binaries and files over ``--max-bytes`` are skipped.
  * ``github-prs OWNER/REPO``: merged pull requests from the GitHub REST API
    (GITHUB_TOKEN optional, ``--since``), keeping only the title and the body
    sections that read as decisions, rationale or breaking changes, with the
    PR URL as the source. The only subcommand that uses the network.

Every text is redacted (memd.inbox.redact) before it is compared, printed or
stored. Each candidate carries its provenance in ``source`` (``<label> <ref>``:
the export member and id, the file path, the PR URL) and the tags ``import``
and ``import:<kind>``. An item is skipped when its source was already proposed
(a candidate of any status) or saved (a note's ``source``), when the same text
is already a candidate, or when save's dry run calls it a near-duplicate or an
update of an existing note, so re-running an import files nothing twice. A run
files at most ``--max`` candidates and never more than the inbox's pending
limit leaves room for. ``--dry-run`` prints what would be filed and writes
nothing.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

from memd import inbox

KINDS = ("chatgpt", "claude", "markdown", "github-prs")
DEFAULT_MAX = 100                    # candidates filed per run
MAX_MEMBER_BYTES = 512 * 1024 * 1024  # one export file read into memory
MAX_MARKDOWN_BYTES = 256 * 1024      # default --max-bytes for markdown files
MAX_ITEM_BODY = 100_000              # characters of one candidate body
MAX_PR_TEXT = 6000                   # characters of decision text kept per PR
GITHUB_API = "https://api.github.com"
GITHUB_MAX_PAGES = 20
PROPOSER = "mem-import"


class ExportError(ValueError):
    """The input is missing, unreadable or not in a shape mem-import understands."""


@dataclass
class Item:
    """One thing to propose: text plus where it came from."""
    title: str
    body: str
    ref: str                          # provenance within the source (id, path, URL)
    tags: list[str] = field(default_factory=list)
    observed_at: str | None = None
    importance: int | None = None
    footer: str = ""                  # appended to the body; left out of similarity checks


@dataclass
class Conversation:
    """An exported chat, distilled only on request."""
    ref: str
    title: str
    messages: list[tuple[str, str]]


# --------------------------------------------------------------------------- helpers


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _iso_date(value) -> str | None:
    """YYYY-MM-DD from an epoch number (s or ms) or an ISO string; None otherwise."""
    if isinstance(value, bool):
        return None
    if isinstance(value, datetime.datetime):
        return value.date().isoformat()
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 1e11 else value
        if not 0 < seconds < 4e9:
            return None
        return datetime.datetime.fromtimestamp(seconds, datetime.UTC).date().isoformat()
    if isinstance(value, str):
        try:
            return datetime.date.fromisoformat(value.strip()[:10]).isoformat()
        except ValueError:
            return None
    return None


def _first_str(entry: dict, keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _clip(text: str, limit: int = MAX_ITEM_BODY) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + "\n\n[...]"


def _title_from(text: str) -> str:
    from memd.normalize import derive_title
    return derive_title(text)


# --------------------------------------------------------------------------- export files


class Export:
    """A data export given as a zip, an extracted folder, or one file of it."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._zip: zipfile.ZipFile | None = None
        self._dir: Path | None = None
        self.single = False
        if self.path.is_dir():
            self._dir = self.path
            names = []
            for base, dirs, files in os.walk(self.path):
                dirs[:] = sorted(d for d in dirs if not d.startswith((".", "__MACOSX")))
                for name in files:
                    full = Path(base) / name
                    if not name.startswith(".") and not full.is_symlink():
                        names.append(full.relative_to(self.path).as_posix())
            self._names = sorted(names)
        elif self.path.is_file() and zipfile.is_zipfile(self.path):
            try:
                self._zip = zipfile.ZipFile(self.path)
            except (zipfile.BadZipFile, OSError) as exc:
                raise ExportError(f"{self.path.name}: not a readable zip ({exc})") from None
            self._names = sorted(i.filename for i in self._zip.infolist() if not i.is_dir()
                                 and not any(p.startswith((".", "__MACOSX")) for p in PurePosixPath(i.filename).parts))
        elif self.path.is_file() and self.path.suffix.lower() == ".zip":
            raise ExportError(f"{self.path.name}: not a readable zip")
        elif self.path.is_file():
            self._dir = self.path.parent
            self._names = [self.path.name]
            self.single = True       # one JSON file: a memories file unless named otherwise
        else:
            raise ExportError(f"{self.path}: no such file or folder")

    def names(self, *basenames: str, prefix: str | None = None) -> list[str]:
        """Members whose file name is one of basenames (or starts with prefix), any depth."""
        out = []
        for name in self._names:
            base = PurePosixPath(name).name.lower()
            if base in basenames or (prefix and (base.startswith(prefix) or (
                    self.single and base not in ("conversations.json", "projects.json")))):
                out.append(name)
        return out

    def read(self, name: str) -> bytes:
        if self._zip is not None:
            info = self._zip.getinfo(name)
            if info.file_size > MAX_MEMBER_BYTES:
                raise ExportError(f"{name}: larger than {MAX_MEMBER_BYTES // (1024 * 1024)} MiB")
            with self._zip.open(info) as fh:
                data = fh.read(MAX_MEMBER_BYTES + 1)
        else:
            path = self._dir / name
            if path.stat().st_size > MAX_MEMBER_BYTES:
                raise ExportError(f"{name}: larger than {MAX_MEMBER_BYTES // (1024 * 1024)} MiB")
            data = path.read_bytes()
        if len(data) > MAX_MEMBER_BYTES:
            raise ExportError(f"{name}: larger than {MAX_MEMBER_BYTES // (1024 * 1024)} MiB")
        return data

    def text(self, name: str) -> str:
        return self.read(name).decode("utf-8", errors="replace").lstrip("﻿")

    def json(self, name: str):
        try:
            return json.loads(self.text(name))
        except ValueError as exc:
            raise ExportError(f"{name}: not valid JSON ({str(exc)[:120]})") from None

    def close(self) -> None:
        if self._zip is not None:
            self._zip.close()


def _as_list(data, *keys: str) -> list | None:
    """The list itself, or the first list under one of keys of a dict."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in keys:
            if isinstance(data.get(key), list):
                return data[key]
    return None


# --------------------------------------------------------------------------- ChatGPT


_MEMORY_TEXT = ("content", "memory", "text", "value", "body", "fact")
_DATE_KEYS = ("updated_at", "update_time", "created_at", "create_time", "timestamp", "date")


def _memory_items(entries, kind: str, member: str, errors: list[str]):
    for index, entry in enumerate(entries):
        ident = None
        date = None
        if isinstance(entry, str):
            text = entry.strip()
        elif isinstance(entry, dict):
            text = _first_str(entry, _MEMORY_TEXT) or ""
            ident = entry.get("id") or entry.get("uuid")
            date = next((d for d in (_iso_date(entry.get(k)) for k in _DATE_KEYS) if d), None)
        else:
            errors.append(f"{member}: entry {index} is not a memory")
            continue
        if not text:
            errors.append(f"{member}: entry {index} has no text")
            continue
        ref = f"memory {ident}" if isinstance(ident, (str, int)) and str(ident).strip() else f"memory {_digest(text)}"
        yield Item(title=_title_from(text), body=_clip(text), ref=ref, tags=[f"{kind}-memory"],
                   observed_at=date)


def _text_memories(text: str) -> list[str]:
    """One memory per non-empty line (list markers removed)."""
    out = []
    for line in text.splitlines():
        line = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", line).strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def chatgpt_memories(export: Export, errors: list[str]):
    """Saved memories from any memor*.json/.txt/.md member."""
    for member in export.names(prefix="memor"):
        suffix = PurePosixPath(member).suffix.lower()
        if suffix == ".json":
            try:
                data = export.json(member)
            except ExportError as exc:
                errors.append(str(exc))
                continue
            entries = _as_list(data, "memories", "items", "data", "results")
            if entries is None and isinstance(data, dict) and _first_str(data, _MEMORY_TEXT):
                entries = [data]
            if entries is None:
                errors.append(f"{member}: no list of memories")
                continue
        elif suffix in (".txt", ".md"):
            entries = _text_memories(export.text(member))
        else:
            continue
        yield from ((member, item) for item in _memory_items(entries, "chatgpt", member, errors))


def _chatgpt_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, dict):
        return ""
    if content.get("content_type") not in (None, "text", "multimodal_text"):
        return ""          # code, tool output, browsing, reasoning, custom instructions
    parts = content.get("parts")
    if isinstance(parts, list):
        texts = [p if isinstance(p, str) else p.get("text") if isinstance(p, dict) else None for p in parts]
        return "\n".join(t for t in texts if isinstance(t, str) and t.strip())
    return content.get("text") if isinstance(content.get("text"), str) else ""


def _chatgpt_branch(mapping, current) -> list[dict]:
    """The conversation's visible branch: current_node back to the root, else all by time."""
    if not isinstance(mapping, dict):
        return []
    path = []
    node = mapping.get(current) if isinstance(current, str) else None
    seen = set()
    while isinstance(node, dict) and id(node) not in seen and len(path) <= len(mapping):
        seen.add(id(node))
        path.append(node)
        parent = node.get("parent")
        node = mapping.get(parent) if isinstance(parent, str) else None
    if not path:
        path = [n for n in mapping.values() if isinstance(n, dict)]
        path.sort(key=_created, reverse=True)
    messages = []
    for node in reversed(path):
        message = node.get("message")
        if isinstance(message, dict):
            messages.append(message)
    return messages


def _created(node: dict) -> float:
    message = node.get("message")
    value = message.get("create_time") if isinstance(message, dict) else None
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def chatgpt_conversations(export: Export, errors: list[str]) -> list[Conversation]:
    out: list[Conversation] = []
    for member in export.names("conversations.json"):
        try:
            data = _as_list(export.json(member), "conversations", "items")
        except ExportError as exc:
            errors.append(str(exc))
            continue
        if data is None:
            errors.append(f"{member}: no list of conversations")
            continue
        for index, conv in enumerate(data):
            if not isinstance(conv, dict):
                errors.append(f"{member}: conversation {index} is not an object")
                continue
            ident = conv.get("conversation_id") or conv.get("id") or f"#{index}"
            messages = []
            for message in _chatgpt_branch(conv.get("mapping"), conv.get("current_node")):
                author = message.get("author") if isinstance(message.get("author"), dict) else {}
                role = author.get("role")
                meta = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
                if role not in ("user", "assistant") or meta.get("is_visually_hidden_from_conversation"):
                    continue
                text = _chatgpt_text(message).strip()
                if text:
                    messages.append(("User" if role == "user" else "Assistant", _message_clip(text)))
            title = conv.get("title") if isinstance(conv.get("title"), str) else str(ident)
            out.append(Conversation(ref=f"conversation {ident}", title=title, messages=messages))
    return out


def _message_clip(text: str) -> str:
    return text if len(text) <= inbox.MESSAGE_CHARS else text[:inbox.MESSAGE_CHARS] + " [...]"


# --------------------------------------------------------------------------- Claude


_HEADING = re.compile(r"^(?:#{1,6}\s+(.+?)\s*#*|\*\*(.+?)\*\*:?)\s*$")


def _sections(text: str) -> list[tuple[str, str]]:
    """(heading, text) per headed section; a text without headings is one section."""
    sections: list[tuple[str, list[str]]] = [("", [])]
    for line in text.splitlines():
        match = _HEADING.match(line.strip())
        if match:
            sections.append(((match.group(1) or match.group(2)).strip(), []))
        else:
            sections[-1][1].append(line)
    return [(h, "\n".join(lines).strip()) for h, lines in sections if "\n".join(lines).strip()]


def _memory_texts(data) -> list[tuple[str, str]]:
    """(label, text) of every memory text in a Claude memories file, whatever its shape."""
    out: list[tuple[str, str]] = []
    if isinstance(data, str):
        return [("memory", data)] if data.strip() else []
    if isinstance(data, list):
        for entry in data:
            out.extend(_memory_texts(entry))
        return out
    if not isinstance(data, dict):
        return out
    for key in ("conversations_memory", "memory", "content", "text", "summary"):
        if isinstance(data.get(key), str) and data[key].strip():
            out.append(("memory", data[key]))
    projects = data.get("project_memories")
    if isinstance(projects, dict):
        for project, text in projects.items():
            if isinstance(text, str) and text.strip():
                out.append((f"project {project} memory", text))
            elif isinstance(text, dict) and _first_str(text, _MEMORY_TEXT):
                out.append((f"project {project} memory", _first_str(text, _MEMORY_TEXT)))
    elif isinstance(projects, list):
        for entry in projects:
            if isinstance(entry, dict) and _first_str(entry, _MEMORY_TEXT + ("memory",)):
                ident = entry.get("project_uuid") or entry.get("uuid") or entry.get("id") or "?"
                out.append((f"project {ident} memory", _first_str(entry, _MEMORY_TEXT + ("memory",))))
    for key in ("memories", "items"):
        if isinstance(data.get(key), list):
            out.extend(_memory_texts(data[key]))
    return out


def claude_memories(export: Export, errors: list[str]):
    for member in export.names(prefix="memor"):
        if PurePosixPath(member).suffix.lower() != ".json":
            continue
        try:
            texts = _memory_texts(export.json(member))
        except ExportError as exc:
            errors.append(str(exc))
            continue
        if not texts:
            errors.append(f"{member}: no memory text")
        for label, text in texts:
            for heading, section in _sections(text):
                title = heading or _title_from(section)
                yield member, Item(title=title[:200], body=_clip(section), tags=["claude-memory"],
                                   ref=f"{label} {_digest(heading + chr(10) + section)}")


def claude_projects(export: Export, errors: list[str]):
    for member in export.names("projects.json"):
        try:
            data = _as_list(export.json(member), "projects", "items")
        except ExportError as exc:
            errors.append(str(exc))
            continue
        if data is None:
            errors.append(f"{member}: no list of projects")
            continue
        for index, project in enumerate(data):
            if not isinstance(project, dict):
                errors.append(f"{member}: project {index} is not an object")
                continue
            ident = project.get("uuid") or project.get("id") or f"#{index}"
            name = _first_str(project, ("name", "title")) or f"project {ident}"
            date = next((d for d in (_iso_date(project.get(k)) for k in _DATE_KEYS) if d), None)
            parts = []
            if _first_str(project, ("description",)):
                parts.append(project["description"].strip())
            if _first_str(project, ("prompt_template", "instructions")):
                parts.append("Instructions:\n" + _first_str(project, ("prompt_template", "instructions")))
            if parts:
                yield member, Item(title=f"Project {name}"[:200], body=_clip("\n\n".join(parts)),
                                   ref=f"project {ident}", tags=["claude-project"], observed_at=date)
            docs = project.get("docs")
            for dindex, doc in enumerate(docs if isinstance(docs, list) else []):
                if not isinstance(doc, dict) or not _first_str(doc, ("content", "text")):
                    errors.append(f"{member}: project {ident} document {dindex} has no text")
                    continue
                filename = _first_str(doc, ("filename", "name", "title")) or f"document {dindex}"
                dident = doc.get("uuid") or doc.get("id") or filename
                ddate = next((d for d in (_iso_date(doc.get(k)) for k in _DATE_KEYS) if d), date)
                yield member, Item(title=f"{name}: {filename}"[:200],
                                   body=_clip(_first_str(doc, ("content", "text"))),
                                   ref=f"project {ident} document {dident}", tags=["claude-project"],
                                   observed_at=ddate)


def _claude_message_text(message: dict) -> str:
    text = message.get("text")
    if isinstance(text, str) and text.strip():
        return text
    content = message.get("content")
    if isinstance(content, list):
        return "\n".join(c["text"] for c in content if isinstance(c, dict) and c.get("type") == "text"
                         and isinstance(c.get("text"), str) and c["text"].strip())
    return content if isinstance(content, str) else ""


def claude_conversations(export: Export, errors: list[str]) -> list[Conversation]:
    out: list[Conversation] = []
    for member in export.names("conversations.json"):
        try:
            data = _as_list(export.json(member), "conversations", "items")
        except ExportError as exc:
            errors.append(str(exc))
            continue
        if data is None:
            errors.append(f"{member}: no list of conversations")
            continue
        for index, conv in enumerate(data):
            if not isinstance(conv, dict):
                errors.append(f"{member}: conversation {index} is not an object")
                continue
            ident = conv.get("uuid") or conv.get("id") or f"#{index}"
            raw = conv.get("chat_messages") or conv.get("messages") or []
            messages = []
            for message in raw if isinstance(raw, list) else []:
                if not isinstance(message, dict):
                    continue
                role = message.get("sender") or message.get("role")
                if role not in ("human", "user", "assistant"):
                    continue
                text = _claude_message_text(message).strip()
                if text:
                    messages.append(("Assistant" if role == "assistant" else "User", _message_clip(text)))
            title = _first_str(conv, ("name", "title")) or str(ident)
            out.append(Conversation(ref=f"conversation {ident}", title=title, messages=messages))
    return out


# --------------------------------------------------------------------------- Markdown


def _excluded(rel: str, patterns: list[str], *, is_dir: bool = False) -> bool:
    """A .gitignore-style match: a bare name matches at any depth, a/b from the root,
    a trailing slash only directories; a file under an excluded directory is excluded."""
    parts = rel.split("/")
    for raw in patterns:
        pattern = raw.strip()
        if not pattern or pattern.startswith("#"):
            continue
        dir_only = pattern.endswith("/")
        pattern = pattern.strip("/")
        if not pattern:
            continue
        # Candidate paths: the path itself (a directory, or a file unless dir-only)
        # and every ancestor directory.
        paths = [("/".join(parts[:i]), True) for i in range(1, len(parts))]
        paths.append((rel, is_dir))
        for path, path_is_dir in paths:
            if dir_only and not path_is_dir:
                continue
            if "/" not in pattern:
                if fnmatchcase(path.rsplit("/", 1)[-1], pattern):
                    return True
            elif fnmatchcase(path, pattern) or (pattern.startswith("**/") and fnmatchcase(path, pattern[3:])):
                return True
    return False


def _git(root: Path, *args: str) -> str | None:
    try:
        done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                              timeout=60, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def _git_files(root: Path) -> set[str] | None:
    """Tracked and untracked-but-not-ignored files under root, or None outside Git."""
    if (_git(root, "rev-parse", "--is-inside-work-tree") or "").strip() != "true":
        return None
    out = _git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    return None if out is None else {p for p in out.split("\0") if p}


def _git_dates(root: Path) -> dict[str, str]:
    """Each file's last commit date (YYYY-MM-DD), relative to root."""
    out = _git(root, "log", "--format=%x00%cs", "--name-only", "--relative", "--no-renames", "--", ".")
    dates: dict[str, str] = {}
    current = None
    for line in (out or "").splitlines():
        if line.startswith("\0"):
            current = line[1:].strip()
        elif line.strip() and current:
            dates.setdefault(line.strip(), current)
    return dates


def _frontmatter(text: str) -> tuple[dict, str, bool]:
    """(frontmatter, body, malformed)."""
    import yaml
    if not text.startswith("---"):
        return {}, text, False
    match = re.match(r"^---[ \t]*\r?\n(.*?)(?:\r?\n)?^(?:---|\.\.\.)[ \t]*(?:\r?\n|\Z)", text, re.S | re.M)
    if not match:
        return {}, text, False
    rest = text[match.end():]
    try:
        data = yaml.safe_load(match.group(1)) if match.group(1).strip() else {}
    except yaml.YAMLError:
        return {}, rest, True
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return {}, rest, True
    return data, rest, False


def _tags(value) -> list[str]:
    if isinstance(value, str):
        value = re.split(r"[,\s]+", value)
    if not isinstance(value, list):
        return []
    tags = [str(t).strip().lstrip("#").strip() for t in value if isinstance(t, (str, int)) and str(t).strip()]
    return [t[:60] for t in tags if t][:12]


def markdown_note(text: str, rel: str) -> tuple[Item | None, str | None]:
    """(item, warning) for one Markdown file's text; item None when it has no body."""
    fm, body, malformed = _frontmatter(text)
    warning = f"{rel}: malformed frontmatter ignored" if malformed else None
    title = fm.get("title") if isinstance(fm.get("title"), str) and fm["title"].strip() else None
    lines = body.strip().splitlines()
    if title is None:
        for number, line in enumerate(lines):
            match = re.match(r"^#\s+(.+?)\s*#*\s*$", line)
            if match:
                title = match.group(1).strip()
                if not any(line.strip() for line in lines[:number]):
                    lines = lines[number + 1:]     # the H1 heads the body: drop it
                break
    if title is None:
        title = re.sub(r"[-_]+", " ", PurePosixPath(rel).stem).strip() or rel
    body = "\n".join(lines).strip()
    if not body:
        return None, warning
    date = next((d for d in (_iso_date(fm.get(k)) for k in
                             ("observed_at", "updated", "modified", "date", "created")) if d), None)
    importance = fm.get("importance")
    importance = importance if isinstance(importance, int) and not isinstance(importance, bool) else None
    tags = _tags(fm.get("tags", fm.get("tag", fm.get("keywords"))))
    return Item(title=" ".join(title.split())[:200], body=_clip(body), ref=rel, tags=tags,
                observed_at=date, importance=importance), warning


def markdown_items(root: str | Path, *, excludes: list[str] = (), max_bytes: int = MAX_MARKDOWN_BYTES,
                   skipped: list[dict] | None = None, errors: list[str] | None = None):
    """Items from the Markdown files under root, in path order."""
    root = Path(root)
    if not root.is_dir():
        raise ExportError(f"{root}: not a folder")
    root = root.resolve()
    skipped = [] if skipped is None else skipped
    errors = [] if errors is None else errors
    listed = _git_files(root)
    dates = _git_dates(root) if listed is not None else {}
    for base, dirs, files in os.walk(root):
        rel_base = Path(base).relative_to(root).as_posix()
        rel_base = "" if rel_base == "." else rel_base + "/"
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and not (Path(base) / d).is_symlink()
                         and not _excluded(rel_base + d, list(excludes), is_dir=True))
        for name in sorted(files):
            rel = rel_base + name
            path = Path(base) / name
            if name.startswith(".") or path.suffix.lower() not in (".md", ".markdown"):
                continue
            reason = None
            if _excluded(rel, list(excludes)):
                reason = "excluded"
            elif listed is not None and rel not in listed:
                reason = "ignored by Git"
            elif path.is_symlink() or not path.resolve().is_relative_to(root):
                reason = "symlink"
            elif path.stat().st_size > max_bytes:
                reason = f"larger than {max_bytes} bytes"
            if reason:
                skipped.append({"ref": rel, "reason": reason})
                continue
            data = path.read_bytes()
            if b"\0" in data[:8192]:
                skipped.append({"ref": rel, "reason": "binary"})
                continue
            try:
                text = data.decode("utf-8").lstrip("﻿")
            except UnicodeDecodeError:
                skipped.append({"ref": rel, "reason": "not UTF-8"})
                continue
            item, warning = markdown_note(text, rel)
            if warning:
                errors.append(warning)
            if item is None:
                skipped.append({"ref": rel, "reason": "empty"})
                continue
            item.observed_at = item.observed_at or dates.get(rel)
            yield item


# --------------------------------------------------------------------------- GitHub pull requests


_REPO = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
_DECISION_HEADING = re.compile(
    r"(?i)\b(decisions?|decided|rationale|why|motivation|reasons?|reasoning|context|background|breaking|"
    r"trade-?offs?|alternatives?|design|approach|migration|consequences?|considered)\b")
_DECISION_TEXT = re.compile(
    r"(?i)(breaking[ -]change|\bBREAKING\b|\bwe (?:decided|chose|agreed|opted)\b|\bdecided to\b|"
    r"\bdecision\b|\brationale\b|\binstead of\b|\bin favou?r of\b|\btrade-?off\b|\bdeprecat)")
_CHECKBOX_ONLY = re.compile(r"^(?:\s*[-*]\s*\[[ xX]\].*\n?)+$")


def decision_text(body: str) -> str:
    """The parts of a PR description that read as decisions, rationale or breaking changes."""
    body = re.sub(r"<!--.*?-->", "", body or "", flags=re.S).replace("\r\n", "\n")
    kept: list[str] = []
    for heading, text in _sections(body):
        if heading and _DECISION_HEADING.search(heading):
            if text and not _CHECKBOX_ONLY.match(text):
                kept.append(f"### {heading}\n{text}")
            continue
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if _DECISION_TEXT.search(p)]
        if paragraphs:
            kept.append((f"### {heading}\n" if heading else "") + "\n\n".join(paragraphs))
    out = "\n\n".join(kept).strip()
    return out if len(out) <= MAX_PR_TEXT else out[:MAX_PR_TEXT].rstrip() + " [...]"


def _github_error(resp, repo: str) -> ExportError:
    if resp.status_code == 404:
        return ExportError(f"GitHub: {repo} not found (a private repository needs GITHUB_TOKEN)")
    if resp.status_code in (401, 403, 429):
        detail = "rate limited" if resp.headers.get("x-ratelimit-remaining") == "0" or resp.status_code == 429 \
            else "access refused"
        return ExportError(f"GitHub HTTP {resp.status_code}: {detail} (set GITHUB_TOKEN)")
    return ExportError(f"GitHub HTTP {resp.status_code}: {resp.text[:200]}")


def github_items(repo: str, *, since: str | None = None, token: str | None = None,
                 api_url: str | None = None, skipped: list[dict] | None = None, client=None,
                 max_pages: int = GITHUB_MAX_PAGES):
    """Items from merged pull requests, newest first, fetched page by page as consumed."""
    import httpx
    if not _REPO.match(repo or ""):
        raise ExportError("Give the repository as OWNER/REPO")
    since_date = _iso_date(since) if since else None
    if since and since_date is None:
        raise ExportError("--since takes a date: YYYY-MM-DD")
    skipped = [] if skipped is None else skipped
    base = (api_url or os.environ.get("GITHUB_API_URL") or GITHUB_API).rstrip("/")
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": "memd-import"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    own = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0))
    try:
        for page in range(1, max_pages + 1):
            try:
                resp = client.get(f"{base}/repos/{repo}/pulls", headers=headers, params={
                    "state": "closed", "sort": "updated", "direction": "desc", "per_page": 100, "page": page})
            except httpx.HTTPError as exc:
                raise ExportError(f"GitHub unreachable: {type(exc).__name__}") from None
            if resp.status_code != 200:
                raise _github_error(resp, repo)
            try:
                pulls = resp.json()
            except ValueError:
                raise ExportError("GitHub answered with invalid JSON") from None
            if not isinstance(pulls, list):
                raise ExportError("GitHub answered with something other than a list of pull requests")
            older = False
            for pr in pulls:
                if not isinstance(pr, dict):
                    continue
                updated = _iso_date(pr.get("updated_at"))
                if since_date and updated and updated < since_date:
                    older = True          # sorted by update time: the rest are older still
                    break
                merged = _iso_date(pr.get("merged_at"))
                url = pr.get("html_url") if isinstance(pr.get("html_url"), str) else \
                    f"https://github.com/{repo}/pull/{pr.get('number')}"
                if not merged or (since_date and merged < since_date):
                    continue
                title = pr.get("title").strip() if isinstance(pr.get("title"), str) else ""
                text = decision_text(pr.get("body") if isinstance(pr.get("body"), str) else "")
                if not title or not text:
                    skipped.append({"ref": url, "reason": "no decision text"})
                    continue
                footer = f"Pull request #{pr.get('number')} in {repo}, merged {merged}: {url}"
                labels = [lb.get("name") for lb in pr.get("labels") or [] if isinstance(lb, dict)]
                yield Item(title=title[:200], body=text, footer=footer, ref=url, observed_at=merged,
                           tags=["decision", *_tags([lb for lb in labels if isinstance(lb, str)])[:5]])
            if older or len(pulls) < 100:
                return
    finally:
        if own:
            client.close()


# --------------------------------------------------------------------------- filing


def _note_sources(cfg) -> set[str]:
    from memd.store import list_notes
    try:
        return {n.source for n in list_notes(cfg.clone) if n.source}
    except Exception:
        return set()


def run(kind: str, items, *, cfg, profile: str, label: str, dry_run: bool = False,
        max_items: int = DEFAULT_MAX, conversations: list[Conversation] | None = None,
        distill: bool = False, max_chunks: int = inbox.MAX_CHUNKS, report: dict | None = None) -> dict:
    """File items (then distilled conversations) as candidates; returns the run report.

    ``items`` yields Item or (member, Item); it is consumed only until the cap.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown import kind {kind!r}")
    report = report if report is not None else {}
    report.update({"ok": True, "kind": kind, "label": label, "dry_run": dry_run, "read": 0,
                   "filed": [], "would_file": [], "capped": False})
    report.setdefault("skipped", [])
    report.setdefault("errors", [])
    sources, hashes, pending_count = inbox.known(cfg, profile)
    sources |= _note_sources(cfg)
    cap = max(0, min(int(max_items), inbox.MAX_PENDING - pending_count))
    report["cap"] = cap
    if conversations and distill:
        from memd.llm import enabled
        if not enabled(cfg):
            raise RuntimeError("--distill needs a chat model (set MEMD_LLM_URL)")
    pending = inbox.pending_texts(cfg, profile)
    seen: list[tuple[str, str]] = []
    channel = f"import:{kind}"
    base_tags = ["import", channel]

    def count() -> int:
        return len(report["filed"]) + len(report["would_file"])

    items = iter(items)
    if cap == 0:
        report["capped"] = True           # the inbox is full: read (and fetch) nothing
        items = iter(())
    while True:
        try:
            entry = next(items)
        except StopIteration:
            break
        except ExportError as exc:        # e.g. GitHub refused a later page
            report["errors"].append(str(exc))
            report["ok"] = False
            break
        item = entry[1] if isinstance(entry, tuple) else entry
        if count() >= cap:
            report["capped"] = True
            break
        report["read"] += 1
        title = inbox.redact(item.title).strip()[:inbox.MAX_TITLE] or "untitled note"
        own = inbox.redact(item.body).strip()
        fact: dict = {"title": title,
                      "body": own + ("\n\n" + inbox.redact(item.footer) if item.footer else ""),
                      "source": inbox.redact(f"{label} {item.ref}".strip())[:200],
                      "tags": list(dict.fromkeys([*item.tags, *base_tags]))}
        if item.observed_at:
            fact["observed_at"] = item.observed_at
        if item.importance is not None:
            fact["importance"] = max(1, min(5, item.importance))
        dup = _duplicate(fact, own, sources, hashes, pending, seen)
        report_lint = None
        if dup is None:
            report_lint = inbox.lint(fact, profile, cfg=cfg)
            near = report_lint.get("near_duplicates") or []
            if near:
                dup = f"note {near[0]['slug']}"
            elif report_lint.get("action") == "update":
                dup = f"note {report_lint.get('slug')}"
        seen.append((title, own))
        sources.add(fact["source"])
        hashes.add(inbox.content_hash(fact["title"], fact["body"]))
        if dup is not None:
            report["skipped"].append({"ref": fact["source"], "title": fact["title"], "duplicate_of": dup})
            continue
        if dry_run:
            report["would_file"].append(fact)
            continue
        try:
            filed = inbox.propose(fact, profile, cfg=cfg, source=channel, proposer=PROPOSER,
                                  precomputed_lint=report_lint)
        except ValueError as exc:            # the pending limit, reached concurrently
            report["errors"].append(f"{fact['source']}: {exc}")
            report["capped"] = True
            break
        if filed["duplicate"]:
            report["skipped"].append({"ref": fact["source"], "title": fact["title"],
                                      "duplicate_of": f"candidate {filed['id']}"})
        else:
            report["filed"].append({"id": filed["id"], "title": fact["title"], "source": fact["source"]})

    conversations = conversations or []
    if not report["read"] and not conversations and report["errors"]:
        report["ok"] = False              # nothing usable, only errors
    report["conversations"] = len(conversations)
    report["conversations_distilled"] = 0
    if conversations and not distill:
        report["notes"] = [f"{len(conversations)} conversation(s) not read: add --distill to extract "
                           "facts from them with the chat model"]
    elif conversations and not report["capped"]:
        for conv in conversations:
            if count() >= cap:
                report["capped"] = True
                break
            source = inbox.redact(f"{label} {conv.ref}".strip())[:200]
            if source in sources:
                report["skipped"].append({"ref": source, "title": conv.title, "duplicate_of": "already distilled"})
                continue
            if not conv.messages:
                continue
            out = inbox.distill_messages(conv.messages, cfg=cfg, profile=profile, name=conv.title,
                                         dry_run=dry_run, max_chunks=max_chunks, channel=channel,
                                         proposer=PROPOSER, fact_source=source,
                                         tags=[*base_tags, f"{kind}-conversation"],
                                         limit=cap - count(), seen=seen)
            report["conversations_distilled"] += 1
            report["filed"].extend({**f, "source": source} for f in out["filed"])
            report["would_file"].extend(out["facts"])
            report["skipped"].extend({"ref": source, **s} for s in out["skipped"])
            report["errors"].extend(f"{conv.ref}: {e}" for e in out["errors"])
            report["capped"] = report["capped"] or out["capped"]
    return report


def _duplicate(fact: dict, own: str, sources: set[str], hashes: set[str], pending, seen) -> str | None:
    """Why fact repeats something already proposed or imported, else None.

    Similarity compares the title and the item's own text (``own``), not a
    footer every item of a kind shares.
    """
    if fact["source"] in sources:
        return "already imported"
    if inbox.content_hash(fact["title"], fact["body"]) in hashes:
        return "an existing candidate"
    text = fact["title"] + "\n" + own
    for cid, title, body in pending:
        if inbox._similar(text, title + "\n" + body):
            return f"candidate {cid}"
    for title, body in seen:
        if inbox._similar(text, title + "\n" + body):
            return f"imported item '{title}'"
    return None


# --------------------------------------------------------------------------- CLI


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def _print(report: dict) -> None:
    verb = "would file" if report["dry_run"] else "filed"
    number = len(report["would_file"]) if report["dry_run"] else len(report["filed"])
    print(f"{report['kind']}: {report['read']} item(s) read; {verb} {number}; "
          f"{len(report['skipped'])} skipped (cap {report['cap']}{', reached' if report['capped'] else ''})")
    for item in report["filed"]:
        print(f"  filed {item['id']}  {item['title']}")
    for item in report["would_file"]:
        print(f"  would file: {item['title']}  [{item['source']}]")
    for item in report["skipped"]:
        why = item.get("duplicate_of") or item.get("reason")
        print(f"  skipped ({why}): {item.get('title') or item.get('ref')}")
    for note in report.get("notes") or []:
        print(f"  note: {note}")
    for error in report["errors"]:
        print(f"  error: {error}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    from memd.config import Config
    ap = argparse.ArgumentParser(
        prog="mem-import",
        description="Import memories from chat exports, Markdown folders or GitHub pull requests "
                    "into the review inbox; nothing is saved until a person approves it.")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dry-run", action="store_true", help="print what would be filed; write nothing")
    common.add_argument("--json", action="store_true", help="print the report as JSON")
    common.add_argument("--max", type=_positive, default=DEFAULT_MAX,
                        help=f"file at most N candidates this run (default {DEFAULT_MAX})")
    common.add_argument("--source-label", help="provenance label that starts each candidate's source")
    sub = ap.add_subparsers(dest="kind", required=True)
    for kind, what in (("chatgpt", "a ChatGPT data export (zip, folder or JSON file)"),
                       ("claude", "a Claude data export (zip, folder or JSON file)")):
        sp = sub.add_parser(kind, parents=[common], help=f"import {what}")
        sp.add_argument("export")
        sp.add_argument("--distill", action="store_true",
                        help="also extract facts from conversations with the chat model (MEMD_LLM_URL)")
        sp.add_argument("--max-chunks", type=_positive, default=inbox.MAX_CHUNKS,
                        help=f"model calls per conversation (default {inbox.MAX_CHUNKS})")
    md = sub.add_parser("markdown", parents=[common], help="import a folder of Markdown files")
    md.add_argument("folder")
    md.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                    help=".gitignore-style pattern to skip (repeatable)")
    md.add_argument("--max-bytes", type=_positive, default=MAX_MARKDOWN_BYTES,
                    help=f"skip files larger than this (default {MAX_MARKDOWN_BYTES})")
    gh = sub.add_parser("github-prs", parents=[common],
                        help="import decisions from merged pull requests (GITHUB_TOKEN optional)")
    gh.add_argument("repo", metavar="OWNER/REPO")
    gh.add_argument("--since", help="only pull requests merged on or after this date (YYYY-MM-DD)")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)

    cfg = Config.from_env()
    if cfg.clone is None or cfg.db is None:
        print("mem-import: no clone/index configured (MEMD_CLONE / MEMD_DB / MEMD_PROFILE)", file=sys.stderr)
        return 2
    report: dict = {"skipped": [], "errors": []}
    export = None
    try:
        conversations = None
        if args.kind in ("chatgpt", "claude"):
            export = Export(args.export)
            label = args.source_label or f"{args.kind} export {export.path.name}"
            if args.kind == "chatgpt":
                items = chatgpt_memories(export, report["errors"])
                parse = chatgpt_conversations
            else:
                items = _chain(claude_memories(export, report["errors"]), claude_projects(export, report["errors"]))
                parse = claude_conversations
            if not (export.names(prefix="memor") or export.names("conversations.json", "projects.json")):
                raise ExportError(f"{export.path.name}: no memories, projects.json or conversations.json found")
            conversations = parse(export, report["errors"])
        elif args.kind == "markdown":
            folder = Path(args.folder)
            label = args.source_label or f"markdown {folder.resolve().name}"
            items = markdown_items(folder, excludes=args.exclude, max_bytes=args.max_bytes,
                                   skipped=report["skipped"], errors=report["errors"])
        else:
            label = args.source_label or ""
            items = github_items(args.repo, since=args.since, token=os.environ.get("GITHUB_TOKEN") or None,
                                 skipped=report["skipped"])
        report = run(args.kind, items, cfg=cfg, profile=cfg.profile, label=label, dry_run=args.dry_run,
                     max_items=args.max, conversations=conversations,
                     distill=getattr(args, "distill", False),
                     max_chunks=getattr(args, "max_chunks", inbox.MAX_CHUNKS), report=report)
    except (ExportError, RuntimeError, ValueError, OSError) as exc:
        print(f"mem-import: {exc}", file=sys.stderr)
        return 1
    finally:
        if export is not None:
            export.close()
    if args.json:
        print(json.dumps(report, default=str))
    else:
        _print(report)
    return 0 if report["ok"] else 1


def _chain(*iterables):
    for iterable in iterables:
        yield from iterable


if __name__ == "__main__":
    raise SystemExit(main())
