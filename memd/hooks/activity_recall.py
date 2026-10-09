"""Claude Code PostToolUse hook: recall triggered by what the agent is doing.

The prompt hook (``auto_recall``) recalls on the words a person typed. This one
recalls on the target of a tool call: the host an agent SSHes into, the service it
restarts, the file and repository it edits. Memory about that target surfaces
once per session, next to the tool result, before the agent acts on it.

Rails:
  - off unless installed (``onboard.sh --activity-hook``); ``MEMD_ACTIVITY_HOOK=0``
    disables an installed hook,
  - each target (host, service, repo, file) recalls at most once per session, and
    each note is injected at most once per session; the state is a small file per
    session under ``$XDG_CACHE_HOME/memd/hook-state``, pruned by age,
  - small k, ``include_core: false``, a host filter when a known note host was
    identified, and a hard wall-clock deadline (1500 ms by default),
  - a note is injected only when it names one of the targets (or is scoped to the
    identified host), and the injected text has a hard character cap,
  - a failed Bash call (``PostToolUseFailure``, or a nonzero exit in a
    ``PostToolUse`` response) recalls on its normalised error lines instead, under
    "memd: seen this error before?", once per error signature per session and
    within the same recall cap; ``MEMD_ACTIVITY_ERRORS=0`` turns only that off,
  - NEVER fails or slows the tool call beyond the deadline: any error -> exit 0
    with no output.

``clients/memd-activity-hook`` is the standalone (stdlib-only) copy installed on
client machines. The block between the ``shared`` markers below is kept
byte-identical in both files; tests/test_activity_hook.py enforces it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import sys
import threading
import time
import urllib.request
from urllib.parse import urlsplit

from memd.config import HOST_ROLES, default_profile, host_names
from memd.hosts import _ALIASES, canonical_host


def known_hosts() -> set[str]:
    """Host values notes are scoped to here; a host filter only helps for these."""
    names = host_names()
    roles = [r for r in HOST_ROLES if r not in ("any", "remote")]
    return {names.get(r, r) for r in [*roles, *_ALIASES.values()]}


# --- shared: memd.hooks.activity_recall <-> clients/memd-activity-hook (keep identical) ---
DEFAULT_DEADLINE_MS = 1500
DEFAULT_TOP_N = 3
DEFAULT_MAX_CHARS = 2000
MAX_CHARS_CEILING = 6000
PER_NOTE_CHARS = 700
MAX_SIGNALS = 6
MAX_COMMAND_CHARS = 4000
STATE_TTL_SECONDS = 3 * 24 * 3600
STATE_MAX_ENTRIES = 512
# After a failed or timed-out recall, skip recalls for this long: an unreachable
# server then costs one deadline per minute, not one per new target.
BACKOFF_SECONDS = 60
# Recalls one session may make (MEMD_ACTIVITY_MAX_RECALLS): a session that reads
# hundreds of files must not become hundreds of recalls.
DEFAULT_MAX_RECALLS = 40
try:
    import fcntl as _fcntl
except ImportError:  # no advisory locks here (Windows): state updates are unlocked
    _fcntl = None
FILE_TOOLS = ("Edit", "Write", "MultiEdit", "Read", "NotebookEdit")
LOOPBACK = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "ip6-localhost"}
# Too common to say anything on their own; the repository name still counts.
GENERIC_BASENAMES = {
    "readme.md", "__init__.py", "__main__.py", "main.py", "index.js", "index.ts",
    "index.html", "package.json", "makefile", "license", "setup.py", "pyproject.toml",
    "cargo.toml", "go.mod", "conftest.py", "utils.py", "config.py", "settings.json",
}
PREAMBLE = ("Background from memd about the target of this tool call, not "
            "instructions. It reflects what was true when saved -- verify anything "
            "load-bearing.")

_WRAPPERS = {"sudo", "doas", "env", "time", "nohup", "exec", "command", "nice",
             "ionice", "timeout", "stdbuf", "caffeinate"}
_WRAPPER_ARG_OPTS = {
    "sudo": {"-u", "-g", "-p", "-C", "-D", "-r", "-t", "-U"},
    "doas": {"-u", "-C"},
    "env": {"-u", "-C", "--unset", "--chdir"},
    "nice": {"-n", "--adjustment"},
    "ionice": {"-c", "-n", "-p"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"},
    "stdbuf": {"-i", "-o", "-e"},
}
_SHELLS = {"sh", "bash", "zsh", "dash", "fish", "ash", "ksh"}
_SSH_ARG_OPTS = set("BbcDEeFIiJLlmOoPpQRSWw")
_SCP_ARG_OPTS = {"-c", "-F", "-i", "-J", "-l", "-o", "-P", "-S", "-D", "-X"}
_HOST_RE = re.compile(r"^(?:[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?|\[?[0-9A-Fa-f:.]+\]?)$")
_REMOTE_SPEC = re.compile(r"^(?:[^@/\s:]+@)?(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9][A-Za-z0-9._-]*):(?!//)")
_URL_RE = re.compile(r"^(?:https?|ssh|scp|sftp|rsync|tcp|unix)://", re.I)
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,80}$")
_IP_RE = re.compile(r"^[0-9.]+$|:")
_DOCKER_GLOBAL_ARGS = {"-H", "--host", "-c", "--context", "--config", "-l", "--log-level"}
_DOCKER_VERBS = {"logs", "restart", "start", "stop", "exec", "inspect", "rm", "kill",
                 "attach", "top", "stats", "pause", "unpause", "port", "update", "wait",
                 "cp", "run"}
_DOCKER_VERB_ARGS = {
    "logs": {"--tail", "-n", "--since", "--until"},
    "exec": {"-e", "--env", "--env-file", "-u", "--user", "-w", "--workdir"},
    "stop": {"-t", "--time", "-s", "--signal"},
    "restart": {"-t", "--time", "-s", "--signal"},
    "kill": {"-s", "--signal"},
    "inspect": {"-f", "--format", "--type"},
    "stats": {"--format"},
    "update": {"--cpus", "-m", "--memory", "--restart"},
}
_COMPOSE_GLOBAL_ARGS = {"-f", "--file", "-p", "--project-name", "--project-directory",
                        "--env-file", "--profile", "--ansi", "--progress", "--parallel"}
_COMPOSE_VERBS = {"logs", "restart", "up", "start", "stop", "ps", "exec", "pull", "build",
                  "rm", "down", "kill", "run", "top", "create", "pause", "unpause", "events"}
_COMPOSE_VERB_ARGS = {"--tail", "-n", "--since", "--until", "-e", "--env", "-u", "--user",
                      "-w", "--workdir", "-t", "--timeout", "--index", "--scale",
                      "--entrypoint", "--name", "-s", "--signal"}
_SYSTEMCTL_ARGS = {"-H", "--host", "-M", "--machine", "-p", "--property", "-t", "--type",
                   "-s", "--signal", "-n", "--lines", "-o", "--output", "--state",
                   "--kill-whom", "--root"}
_SYSTEMCTL_VERBS = {"status", "start", "stop", "restart", "reload", "enable", "disable",
                    "is-active", "is-enabled", "is-failed", "show", "cat", "edit", "mask",
                    "unmask", "kill", "try-restart", "reload-or-restart", "reenable",
                    "list-dependencies", "reset-failed"}
_UNIT_SUFFIXES = (".service", ".timer", ".socket", ".path", ".target")
_JOURNALCTL_ARGS = {"-u", "--unit", "--user-unit", "-H", "--host", "-n", "--lines", "-o",
                    "--output", "-S", "--since", "-U", "--until", "-p", "--priority",
                    "-t", "--identifier", "-b", "--boot"}
_KUBECTL_ARGS = {"-n", "--namespace", "--context", "--cluster", "--kubeconfig", "-l",
                 "--selector", "-c", "--container", "-o", "--output", "-f", "--filename",
                 "--field-selector", "--since", "--tail", "--replicas", "--image",
                 "--type", "--port", "--address", "--user", "-s", "--server"}
_KUBECTL_POD_VERBS = {"logs", "exec", "attach", "port-forward", "debug"}
_KUBECTL_TYPED_VERBS = {"get", "describe", "delete", "edit", "scale", "patch", "label",
                        "annotate", "expose", "autoscale", "top", "set"}
_POD_HASH = re.compile(r"(?:-[a-z0-9]{8,10})?-(?=[a-z0-9]*\d)[a-z0-9]{5}$")
_HTTP_CLIENTS = {"curl", "wget", "http", "https", "xh", "xhs", "httpie"}


def _env_int(name, default, low, high):
    try:
        value = _setting(name)
        return max(low, min(high, int(value))) if value else default
    except (TypeError, ValueError, OverflowError):
        return default


def _env_file_value(key):
    """Read ``key`` from the onboarding/secret env file (``export K=V`` or ``K=V``)."""
    path = os.environ.get("MEMD_ENV_FILE") or os.path.expanduser("~/.config/memd/client.env")
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("export "):
                    line = line[7:].lstrip()
                name, sep, val = line.partition("=")
                if sep and name.strip() == key:
                    try:
                        parts = shlex.split(val, comments=True)
                    except ValueError:
                        return ""
                    return parts[0] if len(parts) == 1 else ""
    except OSError:
        return ""
    return ""


def _setting(key):
    """Process environment first, then the env file (the hook command stays short)."""
    return os.environ.get(key, "") or _env_file_value(key)


def _resolve_token():
    """Same precedence as memd-recall-hook: an explicit MEMD_ENV_FILE wins."""
    file_token = _env_file_value("MEMD_TOKEN")
    if os.environ.get("MEMD_ENV_FILE") and file_token:
        return file_token
    return os.environ.get("MEMD_TOKEN", "") or file_token


def disabled():
    return _setting("MEMD_ACTIVITY_HOOK").strip().casefold() in {"0", "false", "off", "no"}


# ---- signal extraction (pure) ----

def _host_signal(raw):
    """A ``("host", name)`` signal for a raw host token, or None."""
    if not isinstance(raw, str):
        return None
    host = raw.strip().strip("[]").rstrip(".")
    if not host or not _HOST_RE.match(host) or host.casefold() in LOOPBACK:
        return None
    if host.startswith("127."):
        return None
    name = canonical_host(host)
    known = known_hosts()
    if name not in known and "." in name and not _IP_RE.search(name):
        short = canonical_host(name.split(".", 1)[0])
        if short in known:
            name = short
    return ("host", name)


def _name_signal(kind, raw):
    if not isinstance(raw, str):
        return None
    name = raw.strip()
    if not name or not _NAME_RE.match(name) or not re.search(r"[A-Za-z]", name):
        return None
    return (kind, name)


def _dest_host(dest):
    """Host part of an ssh destination: ``[user@]host`` or ``ssh://[user@]host[:port]``."""
    if _URL_RE.match(dest):
        try:
            return urlsplit(dest).hostname
        except ValueError:
            return None
    host = dest.rpartition("@")[2]
    if host.startswith("["):
        return host.partition("]")[0] + "]"
    return host.partition(":")[0]


def _split_opts(args, arg_opts, clustered=False):
    """Yield ``(option, value, next_index)``; option is None for a positional.

    ``--`` ends option parsing; everything after it is positional.
    """
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            for j in range(i + 1, len(args)):
                yield None, args[j], j + 1
            return
        if a.startswith("-") and len(a) > 1:
            if "=" in a and a.startswith("--"):
                name, _, val = a.partition("=")
                yield name, val, i + 1
            elif a in arg_opts:
                yield a, args[i + 1] if i + 1 < len(args) else "", i + 2
                i += 1
            elif clustered and not a.startswith("--"):
                for j, c in enumerate(a[1:], start=1):
                    if "-" + c in arg_opts:
                        val = a[j + 1:]
                        if not val and i + 1 < len(args):
                            val = args[i + 1]
                            i += 1
                        yield "-" + c, val, i + 1
                        break
                else:
                    yield a, "", i + 1
            else:
                yield a, "", i + 1
        else:
            yield None, a, i + 1
        i += 1


def _ssh(args, depth):
    """The destination host, plus signals from a remote command line."""
    arg_opts = {"-" + c for c in _SSH_ARG_OPTS}
    for opt, val, nxt in _split_opts(args, arg_opts, clustered=True):
        if opt is not None:
            continue
        out = [_host_signal(_dest_host(val))]
        rest = args[nxt:]
        if rest and depth < 2:
            out.extend(bash_signals(" ".join(rest), depth + 1))
        return out
    return []


def _scp_like(args, arg_opts):
    out = []
    for opt, val, _ in _split_opts(args, arg_opts, clustered=True):
        if opt is not None:
            continue
        if _URL_RE.match(val):
            out.append(_host_signal(_dest_host(val)))
            continue
        m = _REMOTE_SPEC.match(val)
        if m:
            out.append(_host_signal(m.group(1)))
    return out


def _sftp(args):
    for opt, val, _ in _split_opts(args, _SCP_ARG_OPTS, clustered=True):
        if opt is None:
            return [_host_signal(_dest_host(val))]
    return []


def _image_name(image):
    base = image.rsplit("/", 1)[-1]
    return base.partition("@")[0].partition(":")[0]


def _compose(args, out):
    verb, nxt = None, 0
    for opt, val, nxt in _split_opts(args, _COMPOSE_GLOBAL_ARGS):
        if opt in ("-p", "--project-name"):
            out.append(_name_signal("service", val))
        elif opt in ("-f", "--file") and os.path.dirname(val):
            # ~/docker/<stack>/compose.yaml names the stack by its directory.
            out.append(_name_signal("service", os.path.basename(os.path.dirname(val))))
        elif opt is None:
            verb = val
            break
    if verb not in _COMPOSE_VERBS:
        return
    for opt, val, _ in _split_opts(args[nxt:], _COMPOSE_VERB_ARGS):
        if opt is None:
            out.append(_name_signal("service", val))
            if verb in ("exec", "run"):
                break


def _docker(args, out):
    verb, nxt = None, 0
    for opt, val, nxt in _split_opts(args, _DOCKER_GLOBAL_ARGS):
        if opt in ("-H", "--host") and _URL_RE.match(val):
            out.append(_host_signal(_dest_host(val)))
        elif opt is None:
            verb = val
            break
    if verb is None:
        return
    rest = args[nxt:]
    if verb == "compose":
        _compose(rest, out)
        return
    if verb == "container" and rest:
        verb, rest = rest[0], rest[1:]
    if verb not in _DOCKER_VERBS:
        return
    if verb == "run":
        for opt, val, _ in _split_opts(rest, {"--name"}):
            if opt == "--name":
                out.append(_name_signal("service", val))
        return
    for opt, val, _ in _split_opts(rest, _DOCKER_VERB_ARGS.get(verb, set())):
        if opt is not None:
            continue
        if verb == "cp":
            if ":" in val and not val.startswith(("/", ".")):
                out.append(_name_signal("service", val.partition(":")[0]))
            continue
        out.append(_name_signal("service", val))
        if verb == "exec":
            break


def _unit(name):
    for suffix in _UNIT_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _systemctl(args, out):
    verb = None
    for opt, val, _ in _split_opts(args, _SYSTEMCTL_ARGS):
        if opt in ("-H", "--host"):
            out.append(_host_signal(_dest_host(val)))
        elif opt is None and verb is None:
            verb = val
            if verb not in _SYSTEMCTL_VERBS:
                return
        elif opt is None and not any(ch in val for ch in "*?["):
            out.append(_name_signal("service", _unit(val)))


def _journalctl(args, out):
    for opt, val, _ in _split_opts(args, _JOURNALCTL_ARGS, clustered=True):
        if opt in ("-u", "--unit", "--user-unit"):
            out.append(_name_signal("service", _unit(val)))
        elif opt in ("-H", "--host"):
            out.append(_host_signal(_dest_host(val)))


def _kubectl(args, out):
    positional = [val for opt, val, _ in _split_opts(args, _KUBECTL_ARGS) if opt is None]
    if not positional:
        return
    verb, rest = positional[0], positional[1:]
    if verb == "rollout" and rest:
        rest = rest[1:]
        verb = "get"
    if verb in _KUBECTL_POD_VERBS:
        if rest:
            name = rest[0].rsplit("/", 1)[-1]
            out.append(_name_signal("service", _POD_HASH.sub("", name) or name))
        return
    if verb not in _KUBECTL_TYPED_VERBS:
        return
    names = []
    for i, val in enumerate(rest):
        if "/" in val:
            names.append(val.rsplit("/", 1)[-1])
        elif i > 0 and "," not in val:
            names.append(val)
    for name in names:
        out.append(_name_signal("service", name))


def _http(args, out):
    for a in args:
        if re.match(r"^https?://", a, re.I):
            try:
                out.append(_host_signal(urlsplit(a).hostname))
            except ValueError:
                continue


def _strip_wrappers(argv):
    """Drop env assignments and wrappers such as sudo/env/timeout before argv[0]."""
    i = 0
    while i < len(argv):
        word = argv[i]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", word):
            i += 1
            continue
        if os.path.basename(word) in _WRAPPERS:
            wrapper = os.path.basename(word)
            i += 1
            while i < len(argv) and (argv[i].startswith("-") or
                                     re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv[i])):
                if argv[i] in _WRAPPER_ARG_OPTS.get(wrapper, ()):
                    i += 1
                i += 1
            if wrapper == "timeout" and i < len(argv) and re.match(r"^[0-9.]+[smhd]?$", argv[i]):
                i += 1
            continue
        break
    return argv[i:]


def _segments(command):
    """Split a shell command into argv lists at ``; & && || | ( )`` and newlines."""
    command = command[:MAX_COMMAND_CHARS].replace("\\\n", " ").replace("\n", " ; ")
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        lex.commenters = ""
        tokens = list(lex)
    except ValueError:
        tokens = command.split()
    segment, skip = [], False
    for tok in tokens:
        if skip:
            skip = False
            continue
        if tok and set(tok) <= set("();&|"):
            if segment:
                yield segment
            segment = []
        elif set(tok) <= set("<>&") or re.match(r"^\d?[<>]{1,2}&?\d?$", tok):
            # A redirection: drop its fd number (the "2" of 2>&1) and its target.
            if segment and segment[-1].isdigit():
                segment.pop()
            skip = not tok.endswith(("&1", "&2")) and not tok.endswith("&")
        else:
            segment.append(tok)
    if segment:
        yield segment


def bash_signals(command, depth=0):
    """Signals (host/service) named by a shell command line."""
    if not isinstance(command, str) or not command.strip():
        return []
    out = []
    for argv in _segments(command):
        argv = _strip_wrappers(argv)
        if not argv:
            continue
        name, args = os.path.basename(argv[0]), argv[1:]
        if any(ch in name for ch in "$`"):
            continue
        if name in ("ssh", "mosh", "autossh", "et"):
            out.extend(_ssh(args, depth))
        elif name == "scp":
            out.extend(_scp_like(args, _SCP_ARG_OPTS))
        elif name == "rsync":
            out.extend(_scp_like(args, {"-e", "--rsh", "-f", "--filter", "--exclude",
                                        "--include", "-T", "-B", "-M"}))
        elif name == "sftp":
            out.extend(_sftp(args))
        elif name in ("docker", "podman", "nerdctl"):
            _docker(args, out)
        elif name in ("docker-compose", "podman-compose"):
            _compose(args, out)
        elif name == "systemctl":
            _systemctl(args, out)
        elif name == "journalctl":
            _journalctl(args, out)
        elif name in ("service", "rc-service") and args:
            out.append(_name_signal("service", args[0]))
        elif name in ("kubectl", "k3s", "oc", "microk8s"):
            _kubectl(args[1:] if name in ("k3s", "microk8s") and args[:1] == ["kubectl"] else args, out)
        elif name in _HTTP_CLIENTS:
            _http(args, out)
        elif name in _SHELLS and depth < 2:
            for opt, val, _ in _split_opts(args, {"-c"}):
                if opt == "-c":
                    out.extend(bash_signals(val, depth + 1))
                    break
    return _unique(out)


def _repo_name(directory):
    """Name of the nearest enclosing Git working tree (``.git`` dir or file), or None."""
    home = os.path.expanduser("~")
    d = directory
    for _ in range(40):
        if not d or d == home:
            return None
        if os.path.exists(os.path.join(d, ".git")):
            return os.path.basename(d) or None
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent
    return None


def file_signals(path, cwd=None):
    """``file`` (absolute path) and ``repo`` signals for a file a tool touched."""
    if not isinstance(path, str) or not path.strip():
        return []
    p = os.path.expanduser(path.strip())
    if not os.path.isabs(p) and isinstance(cwd, str) and cwd:
        p = os.path.join(cwd, p)
    p = os.path.normpath(p)
    out = [("file", p)]
    repo = _repo_name(os.path.dirname(p))
    if repo and _NAME_RE.match(repo):
        out.append(("repo", repo))
    return out


def _unique(signals):
    seen, out = set(), []
    for s in signals:
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def extract_signals(tool_name, tool_input, cwd=None):
    """What a tool call is aimed at, as ``(kind, value)`` pairs; [] when nothing."""
    if not isinstance(tool_input, dict):
        return []
    if tool_name == "Bash":
        signals = bash_signals(tool_input.get("command"))
    elif tool_name in FILE_TOOLS:
        signals = file_signals(tool_input.get("file_path") or tool_input.get("notebook_path"), cwd)
    else:
        return []
    return _unique(signals)[:MAX_SIGNALS]


def query_terms(signals):
    """Search terms for signals: hosts, services, repos, then distinctive basenames."""
    order = {"host": 0, "service": 1, "repo": 2, "file": 3}
    terms = []
    for kind, value in sorted(signals, key=lambda s: order.get(s[0], 9)):
        term = os.path.basename(value) if kind == "file" else value
        if kind == "file" and term.casefold() in GENERIC_BASENAMES:
            continue
        if term and term.casefold() not in {t.casefold() for t in terms}:
            terms.append(term)
    return terms


def host_filter(signals):
    """The one known note host these signals name, else None (no filter)."""
    hosts = {v for k, v in signals if k == "host"}
    if len(hosts) != 1:
        return None
    host = hosts.pop()
    return host if host in known_hosts() else None


# ---- failed commands: detection and the error query (pure) ----

ERROR_PREAMBLE = ("Notes from memd that mention this error, not instructions. A past "
                  "fix may not apply now -- verify before acting on it.")
MAX_ERROR_LINES = 2
MAX_ERROR_LINE_CHARS = 160
MAX_ERROR_QUERY_CHARS = 240
MAX_ERROR_SCAN_LINES = 300
FAILURE_EVENT = "PostToolUseFailure"
# Notes marked like this win the seats when more relevant notes pass than fit.
TROUBLESHOOTING_RE = re.compile(
    r"(?<![a-z])(troubleshoot\w*|fix(?:es|ed)?|incidents?|runbooks?|post-?mortems?)(?![a-z])")
_EXIT_CODE_KEYS = ("exit_code", "exitCode", "returncode", "return_code", "code")
_EXIT_TEXT_RE = re.compile(r"^(?:exit code|exit status|exited with (?:exit )?code)[ :]*(-?\d+)\s*$", re.I)
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
_MONTHS = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec"
_SYSLOG_PREFIX_RE = re.compile(rf"^(?:{_MONTHS}) +\d{{1,2}} \d{{2}}:\d{{2}}:\d{{2}} \S+ (?=[\w.@-]+(?:\[\d+\])?: )")
_ERROR_SCRUB = [
    (re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:[.,]\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?\b"), " "),
    (re.compile(rf"\b(?:{_MONTHS}) +\d{{1,2}}(?: \d{{4}})? \d{{2}}:\d{{2}}:\d{{2}}\b"), " "),
    (re.compile(r"\b\d{1,2}:\d{2}:\d{2}(?:[.,]\d+)?\b"), " "),
    (re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s'\"<>)]+", re.I), " "),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), " "),
    (re.compile(r"\b(?:sha256:)?0x[0-9a-f]+\b|\bsha256:[0-9a-f]+\b", re.I), " "),
    (re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,}\b"), " "),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), " "),
    (re.compile(r"(?:(?<=[\s\"'(=,])|^)(?:~|\.{1,2})?(?:/[^\s\"'(),:;]+)+/?(?::\d+){0,2}"), " "),
    (re.compile(r"\b(?:[\w.-]+/)+[\w-]+\.[A-Za-z][A-Za-z0-9]{0,5}\b(?::\d+){0,2}"), " "),
    (re.compile(r"\b[A-Za-z]:\\[^\s\"'(),;]*"), " "),
    (re.compile(r"\bline \d+\b", re.I), " "),
    (re.compile(r"(?<=\S):\d+(?::\d+)?(?=[:\s),]|$)"), ""),
    (re.compile(r"\[\d+\]"), ""),
    (re.compile(r"\b(?:pid|process)[ =:#]*\d+\b", re.I), " "),
    (re.compile(r"\b\d{4,}\b"), " "),
]
_SKIP_LINE_RE = re.compile(
    r"^(?:traceback \(most recent call last\)|file \"|during handling of the above|"
    r"the above exception was the direct cause|at \S+ \(|at <anonymous>|"
    r"npm err! a complete log of this run|npm err! +(?:/|$)|see \"?systemctl status|"
    r"hint: |\^+$|~+$)", re.I)
_EXCEPTION_RE = re.compile(r"\b[A-Z][A-Za-z0-9]*(?:Error|Exception|Exit|Interrupt|Fault)\b")
_ERRNO_RE = re.compile(r"\bE[A-Z]{3,}\b")
_NOT_ERRNO = {"ERROR", "ERRORS", "EXIT", "EXITED", "EMERG", "ELSE", "ENABLED", "ENTER", "EVENT"}
_CODE_RE = re.compile(r"\b[A-Z]{1,5}-?\d{3,5}\b")
_ERROR_WORDS_RE = re.compile(
    r"\b(?:error|errors|fatal|fail|failed|failure|denied|refused|cannot|can't|could not|"
    r"unable|not found|no such|invalid|timed out|timeout|unreachable|panic|segmentation "
    r"fault|killed|out of memory|emerg|crit|abort|aborted|unknown|missing|conflict)\b", re.I)
_ERROR_PHRASES = (
    "permission denied", "connection refused", "no such file or directory",
    "address already in use", "no space left on device", "command not found",
    "connection timed out", "port is already allocated", "no module named",
    "could not resolve host", "certificate verify failed", "name or service not known",
    "read-only file system", "too many open files", "out of memory", "exec format error",
    "unable to resolve dependency tree", "no matching distribution found",
    "operation not permitted", "connection reset by peer", "broken pipe",
    "no such container", "unit not found", "start request repeated too quickly",
)
_QUOTED_NAME_RE = re.compile(r"['\"`‘“]([A-Za-z][\w.@+-]{1,60})['\"`’”]")
_PACKAGE_RE = re.compile(r"\b(?:found for|requirement|package|module named)\s+([A-Za-z][\w.-]{1,60})", re.I)
_UNIT_NAME_RE = re.compile(r"\b([A-Za-z][\w@-]{1,60})\.(?:service|timer|socket|mount)\b")
_BORING_COMMANDS = {"cd", "echo", "printf", "tail", "head", "grep", "cat", "less", "more",
                    "true", "false", "tee", "sort", "wc", "set", "export", "sleep"}


def errors_disabled():
    """MEMD_ACTIVITY_ERRORS=0 turns off recall on failed commands only."""
    return _setting("MEMD_ACTIVITY_ERRORS").strip().casefold() in {"0", "false", "off", "no"}


def _nonzero(value):
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str) and re.match(r"^-?\d+$", value.strip()):
        return int(value.strip()) != 0
    return False


def _exit_text(text):
    """True when ``text`` carries an "Exit code N" / "exit status N" marker, N != 0."""
    if not isinstance(text, str):
        return False
    lines = [ln.strip() for ln in text.replace("\r", "\n").split("\n") if ln.strip()]
    for line in lines[:1] + lines[-1:]:
        m = _EXIT_TEXT_RE.match(_ANSI_RE.sub("", line))
        if m and int(m.group(1)) != 0:
            return True
    return False


def bash_failure(event):
    """The output text of a failed Bash call, or None when it did not fail.

    A ``PostToolUseFailure`` event carries the failure as ``error`` (and
    ``is_interrupt`` for a user interrupt). A ``PostToolUse`` event counts as a
    failure only when its ``tool_response`` has a nonzero exit-code field or an
    "Exit code N" / "exit status N" line first in stderr (or last in it). Output
    on stderr alone is not a failure; an interrupted call and missing fields are not.
    """
    if not isinstance(event, dict) or event.get("tool_name") != "Bash":
        return None
    if event.get("hook_event_name") == FAILURE_EVENT:
        error = event.get("error")
        if event.get("is_interrupt") is True or not isinstance(error, str) or not error.strip():
            return None
        return error
    response = event.get("tool_response")
    if isinstance(response, str):
        return response if _exit_text(response) else None
    if not isinstance(response, dict) or response.get("interrupted") is True:
        return None
    stdout = response.get("stdout") if isinstance(response.get("stdout"), str) else ""
    stderr = response.get("stderr") if isinstance(response.get("stderr"), str) else ""
    if any(_nonzero(response.get(k)) for k in _EXIT_CODE_KEYS) or _exit_text(stderr):
        return "\n".join(t for t in (stderr, stdout) if t)
    return None


def _looks_secret(value):
    """A credential-like value: 6+ characters with a digit, a symbol or an inner capital."""
    return len(value) >= 6 and (
        any(c.isdigit() or c in "_-+/=@!$%&*." for c in value)
        or (any(c.isupper() for c in value[1:]) and any(c.islower() for c in value)))


def _redact_value(match):
    """Keep a match up to its ``value`` group; drop the value when it looks secret."""
    value = match.group("value")
    bare = value[1:-1] if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'" else value
    if not _looks_secret(bare):
        return match.group(0)
    return match.group(0)[:match.start("value") - match.start()] + " "


def _redact_random(match):
    text = match.group(0)
    if any(c.isdigit() for c in text) and any(c.isalpha() for c in text):
        return " "
    return text


# Credentials a failed command's output can carry. They are removed before the
# line is normalised, so an error query never sends them to the memd server.
_SECRET_SCRUB = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*"), " "),
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
                r"|sk-(?:proj-|ant-[a-z0-9]+-)?[A-Za-z0-9_-]{16,}|xox[abprs]-[A-Za-z0-9-]{10,}"
                r"|(?:AKIA|ASIA)[0-9A-Z]{16}|glpat-[A-Za-z0-9_-]{20,}|hf_[A-Za-z0-9]{20,}"
                r"|tskey-[A-Za-z0-9-]{10,}|memd_[A-Za-z0-9_-]+\.[A-Za-z0-9_-]{20,}"
                r"|mem_(?=[A-Za-z0-9_-]*[0-9A-Z])[A-Za-z0-9_-]{24,}"
                r"|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})"), " "),
    (re.compile(r"\b(?:bearer|basic|token|digest)\s+(?P<value>[A-Za-z0-9._~+/=-]{8,})", re.I),
     _redact_value),
    (re.compile(r"[\w.-]*(?:pass|pwd|secret|token|api[_-]?key|auth|credential|private[_-]?key"
                r"|access[_-]?key|session|cookie)[\w.-]*\s*[:=]\s*(?P<value>\"[^\"]*\"|'[^']*'|\S+)",
                re.I), _redact_value),
    (re.compile(r"(?<![\w+/-])[A-Za-z0-9+/_-]{32,}={0,2}"), _redact_random),
]


def scrub_secrets(text):
    """``text`` without tokens, keys, passwords or other credential-like values."""
    if not isinstance(text, str):
        return ""
    for pattern, repl in _SECRET_SCRUB:
        text = pattern.sub(repl, text)
    return text


def normalise_error_line(line):
    """One output line without volatile detail: ANSI codes, CRs, timestamps, URLs,
    paths, hex ids, IP addresses, line numbers, PIDs, long numbers and credentials.
    Keeps error codes, exception and errno names, and package/unit names."""
    if not isinstance(line, str):
        return ""
    text = _ANSI_RE.sub("", line).replace("\r", "").replace("\t", " ").strip()
    text = scrub_secrets(text)
    text = _SYSLOG_PREFIX_RE.sub("", text)
    for pattern, repl in _ERROR_SCRUB:
        text = pattern.sub(repl, text)
    text = re.sub(r"\(\s*\)|\[\s*\]|([\"'`])\s*\1", " ", text)
    text = re.sub(r"\s*,(?:\s*,)+", ",", text)
    text = re.sub(r"(?<=:)(?:\s+:)+", "", text)          # "bash: <path>: x" -> "bash: x"
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r" (?=[:,;)])", "", text).strip(" :-,;|")
    return text[:MAX_ERROR_LINE_CHARS].rstrip()


def _error_score(line):
    score = 0
    if _EXCEPTION_RE.search(line):
        score += 3
    if any(m not in _NOT_ERRNO for m in _ERRNO_RE.findall(line)):
        score += 3
    low = line.casefold()
    if any(p in low for p in _ERROR_PHRASES):
        score += 2
    if _ERROR_WORDS_RE.search(line):
        score += 2
    if _CODE_RE.search(line):
        score += 1
    return score


def error_lines(text):
    """The most distinctive normalised error lines of a failure's output (in order)."""
    if not isinstance(text, str):
        return []
    raw = _ANSI_RE.sub("", text).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if len(raw) > MAX_ERROR_SCAN_LINES:
        raw = raw[:50] + raw[-(MAX_ERROR_SCAN_LINES - 50):]
    scored, seen = [], set()
    for i, line in enumerate(raw):
        stripped = line.strip()
        if not stripped or _SKIP_LINE_RE.match(stripped) or _EXIT_TEXT_RE.match(stripped):
            continue
        norm = normalise_error_line(stripped)
        key = norm.casefold()
        if len(norm) < 4 or not re.search(r"[A-Za-z]{3}", norm) or key in seen:
            continue
        seen.add(key)
        score = _error_score(norm)
        if score:
            scored.append((score, i, norm))
    best = sorted(scored, key=lambda s: (-s[0], -s[1]))[:MAX_ERROR_LINES]
    return [norm for _, _, norm in sorted(best, key=lambda s: s[1])]


def failed_command_name(command):
    """The command that most likely failed: the last segment's program name."""
    if not isinstance(command, str):
        return ""
    names = []
    for argv in _segments(command):
        argv = _strip_wrappers(argv)
        if argv:
            names.append(os.path.basename(argv[0]))
    for name in reversed(names):
        if name not in _BORING_COMMANDS and _NAME_RE.match(name) and not any(c in name for c in "$`"):
            return name
    return ""


def error_keywords(lines):
    """Terms a note must mention to count as being about this error."""
    out = []
    for line in lines:
        low = line.casefold()
        out.extend(_EXCEPTION_RE.findall(line))
        out.extend(m for m in _ERRNO_RE.findall(line) if m not in _NOT_ERRNO)
        out.extend(_CODE_RE.findall(line))
        out.extend(p for p in _ERROR_PHRASES if p in low)
        out.extend(m.group(1) for m in _QUOTED_NAME_RE.finditer(line) if len(m.group(1)) >= 3)
        out.extend(m.group(1).split("==")[0] for m in _PACKAGE_RE.finditer(line))
        out.extend(m.group(1) for m in _UNIT_NAME_RE.finditer(line))
    seen, unique = set(), []
    for k in out:
        k = k.strip(".-")
        if len(k) >= 3 and k.casefold() not in seen:
            seen.add(k.casefold())
            unique.append(k)
    return unique


def error_query(command, lines, terms):
    """Bounded recall query: command name, error lines, then the call's targets."""
    parts = [failed_command_name(command), *lines, *terms]
    query = re.sub(r"[\"'`‘’“”()\[\]{}<>|]", " ", " ".join(p for p in parts if p))
    query = re.sub(r"\s+", " ", query).strip()
    return query[:MAX_ERROR_QUERY_CHARS].rsplit(" ", 1)[0] if len(query) > MAX_ERROR_QUERY_CHARS else query


def error_signature(command, lines):
    """Stable key for "this error": the command name plus its normalised lines."""
    basis = "\n".join([failed_command_name(command), *lines]).casefold()
    return "error:" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:20]


def troubleshooting(note):
    """True for a note tagged or titled as troubleshooting / fix / incident / runbook."""
    tags = note.get("tags") if isinstance(note.get("tags"), list) else []
    text = " ".join([str(note.get("slug") or ""), str(note.get("title") or ""),
                     " ".join(map(str, tags))]).casefold()
    return bool(TROUBLESHOOTING_RE.search(text))


def prefer_troubleshooting(notes, top_n):
    """``top_n`` of ``notes`` (already relevant, in recall order); marked notes take
    the seats first when more notes pass than fit. Recall order is kept."""
    if len(notes) <= top_n:
        return list(notes)
    marked = {id(n) for n in notes if troubleshooting(n)}
    ranked = sorted(range(len(notes)), key=lambda i: (id(notes[i]) not in marked, i))
    return [notes[i] for i in sorted(ranked[:top_n])]


# ---- per-session state ----

def state_dir():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "memd", "hook-state")


def _state_path(directory, session_id):
    digest = hashlib.sha256(str(session_id).encode("utf-8")).hexdigest()[:24]
    return os.path.join(directory, f"activity-{digest}.json")


def prune_state(directory, now, ttl=STATE_TTL_SECONDS):
    """Delete session state files not touched within ``ttl`` seconds."""
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return
    for entry in entries:
        if not (entry.name.startswith("activity-") and ".json" in entry.name):
            continue
        try:
            if entry.stat().st_mtime < now - ttl:
                os.unlink(entry.path)
        except OSError:
            continue


def _update_state(session_id, now, directory, change):
    """Apply ``change(state)`` to this session's state under a lock; its result.

    ``change`` edits {"seen": {key: time}, "recalls": n} in place and returns
    (result, changed). Raises OSError when the state cannot be read or written:
    the caller then injects nothing, since without state every call would inject.
    """
    os.makedirs(directory, mode=0o700, exist_ok=True)
    path = _state_path(directory, session_id)
    # One lock for the directory: hook runs are brief, and a per-session lock
    # file would need pruning of its own.
    with open(os.path.join(directory, "activity.lock"), "a", encoding="utf-8") as lock:
        if _fcntl is not None:
            _fcntl.flock(lock, _fcntl.LOCK_EX)   # parallel tool calls in one session
        state = {"seen": {}, "recalls": 0}
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and isinstance(data.get("seen"), dict):
                state["seen"] = {k: v for k, v in data["seen"].items()
                                 if isinstance(v, (int, float)) and v >= now - STATE_TTL_SECONDS}
            if isinstance(data, dict) and isinstance(data.get("recalls"), int):
                state["recalls"] = max(0, data["recalls"])
        except FileNotFoundError:
            prune_state(directory, now)
        except ValueError:
            pass
        result, changed = change(state)
        if changed:
            if len(state["seen"]) > STATE_MAX_ENTRIES:
                state["seen"] = dict(sorted(state["seen"].items(), key=lambda kv: kv[1])[-STATE_MAX_ENTRIES:])
            tmp = f"{path}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(state, fh)
            os.replace(tmp, path)
        return result


def claim(keys, session_id, now, directory=None):
    """Record ``keys`` as seen for this session; return the ones not seen before."""
    def change(state):
        new = [k for k in dict.fromkeys(keys) if k not in state["seen"]]
        for k in new:
            state["seen"][k] = now
        return new, bool(new)
    return _update_state(session_id, now, directory or state_dir(), change)


def take_recall(session_id, now, directory, cap):
    """Count one recall against this session's cap; False once the cap is reached."""
    def change(state):
        if state["recalls"] >= cap:
            return False, False
        state["recalls"] += 1
        return True, True
    return _update_state(session_id, now, directory, change)


def backing_off(directory, now):
    try:
        return os.stat(os.path.join(directory, "activity-backoff")).st_mtime > now - BACKOFF_SECONDS
    except OSError:
        return False


def start_backoff(directory, now):
    path = os.path.join(directory, "activity-backoff")
    try:
        with open(path, "w", encoding="utf-8"):
            pass
        os.utime(path, (now, now))
    except OSError:
        pass


# ---- recall, relevance and rendering ----

def with_deadline(fn, seconds):
    """Run ``fn`` in a daemon thread; its value, or None on error or timeout."""
    box = {}

    def target():
        try:
            box["value"] = fn()
        except BaseException as e:  # noqa: BLE001
            box["error"] = e

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive() or "error" in box:
        return None
    return box.get("value")


def remote_fetch(query, host, k, timeout):
    """POST /recall on MEMD_REMOTE; returns ``(notes, recall_id)``. Raises on failure."""
    remote = _setting("MEMD_REMOTE").strip()
    if not remote:
        raise RuntimeError("MEMD_REMOTE is not set")
    body = {"query": query, "k": k, "include_core": False, "max_chars": 4000}
    profile = _setting("MEMD_PROFILE").strip()
    if profile:
        body["profile"] = profile
    if host:
        body["host"] = host
    headers = {"Content-Type": "application/json"}
    token = _resolve_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"{remote.rstrip('/')}/recall",
                                 data=json.dumps(body).encode("utf-8"),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    notes = data.get("notes") if isinstance(data, dict) else None
    if not isinstance(notes, list):
        raise ValueError("recall response has no notes")
    return [n for n in notes if isinstance(n, dict)], data.get("recall_id")


def _note_text(note):
    return str(note.get("body") or note.get("text") or note.get("content") or "")


def relevant_notes(notes, terms, host, top_n):
    """Query matches that name a target (or are scoped to the filtered host)."""
    wanted = set()
    for t in terms:
        wanted.add(t.casefold())
        stem = os.path.splitext(t)[0]
        if stem != t and len(stem) >= 4:
            wanted.add(stem.casefold())
    out = []
    for note in notes:
        if not note.get("matched", True) or not _note_text(note).strip():
            continue
        tags = note.get("tags") if isinstance(note.get("tags"), list) else []
        hay = " ".join([str(note.get("slug") or ""), str(note.get("title") or ""),
                        " ".join(map(str, tags)), _note_text(note)]).casefold()
        on_host = bool(host) and str(note.get("host") or "").casefold() == host.casefold()
        if on_host or any(w in hay for w in wanted):
            out.append(note)
        if len(out) >= top_n:
            break
    return out


def _excerpt(body, terms, cap):
    if len(body) <= cap:
        return body
    low = body.casefold()
    hits = [low.find(t.casefold()) for t in terms]
    hits = [h for h in hits if h >= 0]
    start = max(0, min(hits) - cap // 4) if hits else 0
    start = min(start, len(body) - cap)
    text = body[start:start + cap]
    return ("… " if start else "") + text + (" …" if start + cap < len(body) else "")


def render(notes, terms, recall_id=None, max_chars=DEFAULT_MAX_CHARS, error=None):
    """Bounded context block; never longer than ``max_chars``.

    With ``error`` (the normalised error lines) the block is headed as a
    seen-before error instead of memory for the call's targets.
    """
    if not notes:
        return ""
    if error:
        shown = "; ".join(error)[:MAX_ERROR_LINE_CHARS]
        head = f"## memd: seen this error before?\n\nError: {shown}\n\n{ERROR_PREAMBLE}\n\n"
    else:
        targets = ", ".join(terms[:4])
        head = f"## memd: memory for {targets}\n\n{PREAMBLE}\n\n"
    footer = "[Excerpts; use read(slug"
    footer += f", recall_id=\"{recall_id}\")" if recall_id else ")"
    footer += " for a full note.]"
    available = max_chars - len(head) - len(footer) - 2
    blocks = []
    for note in notes:
        heading = f"### {note.get('slug') or note.get('title') or 'note'}\n"
        cap = min(PER_NOTE_CHARS, available - len(heading) - 8)
        if cap < 80:
            break
        block = heading + _excerpt(_note_text(note).strip(), terms, cap) + "\n\n"
        blocks.append(block)
        available -= len(block)
    if not blocks:
        return ""
    return (head + "".join(blocks) + footer)[:max_chars]


def handle(event, fetch, now=None, directory=None):
    """The hook's output object for one PostToolUse event, or None for no output."""
    try:
        return _handle(event, fetch, now, directory)
    except Exception:  # noqa: BLE001 -- the tool call must never see a hook error
        return None


def _handle(event, fetch, now, directory):
    if disabled() or not isinstance(event, dict):
        return None
    now = time.time() if now is None else now
    directory = directory or state_dir()
    session = str(event.get("session_id") or "unknown")
    signals = extract_signals(event.get("tool_name"), event.get("tool_input"), event.get("cwd"))
    event_name = FAILURE_EVENT if event.get("hook_event_name") == FAILURE_EVENT else "PostToolUse"
    failure = None if errors_disabled() else bash_failure(event)
    if failure is not None:
        lines = error_lines(failure)
        tool_input = event.get("tool_input")
        command = tool_input.get("command") if isinstance(tool_input, dict) else None
        # One recall per hook run: a new error takes this run; a repeated one
        # (same signature this session) falls through to the target recall.
        if lines and claim([error_signature(command, lines)], session, now, directory):
            terms = query_terms(signals)
            query = error_query(command, lines, terms)
            wanted = error_keywords(lines) + terms
            return _recall(query, wanted, host_filter(signals), session, now, directory,
                           fetch, event_name, error=lines)
    if not signals:
        return None
    if not claim([f"{k}:{v}" for k, v in signals], session, now, directory):
        return None
    terms = query_terms(signals)
    if not terms:
        return None
    return _recall(" ".join(terms)[:200], terms, host_filter(signals), session, now, directory,
                   fetch, event_name)


def _recall(query, terms, host, session, now, directory, fetch, event_name, error=None):
    """Recall under the deadline, the per-session cap and backoff; the hook output."""
    top_n = _env_int("MEMD_ACTIVITY_TOP_N", DEFAULT_TOP_N, 1, 8)
    deadline = _env_int("MEMD_ACTIVITY_DEADLINE_MS", DEFAULT_DEADLINE_MS, 100, 10000) / 1000
    max_chars = _env_int("MEMD_ACTIVITY_MAX_CHARS", DEFAULT_MAX_CHARS, 400, MAX_CHARS_CEILING)
    if backing_off(directory, now):
        return None
    cap = _env_int("MEMD_ACTIVITY_MAX_RECALLS", DEFAULT_MAX_RECALLS, 1, 1000)
    if not take_recall(session, now, directory, cap):
        return None
    result = with_deadline(lambda: fetch(query, host, top_n + 2, deadline), deadline)
    if not result:
        start_backoff(directory, now)
        return None
    notes, recall_id = result
    picked = relevant_notes(notes, terms, host, top_n + 2)
    if not picked:
        return None
    # A note surfaces once per session even when several targets lead to it.
    fresh = claim([f"note:{n.get('slug') or n.get('title')}" for n in picked], session, now, directory)
    picked = [n for n in picked if f"note:{n.get('slug') or n.get('title')}" in fresh]
    picked = prefer_troubleshooting(picked, top_n) if error else picked[:top_n]
    text = render(picked, terms, recall_id, max_chars, error=error)
    if not text:
        return None
    return {"hookSpecificOutput": {"hookEventName": event_name, "additionalContext": text}}


def run(stdin, stdout, fetch):
    """Read one event, maybe write one JSON object. Always returns 0."""
    try:
        raw = stdin.read()
        event = json.loads(raw) if raw and raw.strip() else None
        out = handle(event, fetch)
        if out:
            stdout.write(json.dumps(out) + "\n")
            stdout.flush()
    except BaseException as e:  # noqa: BLE001
        try:
            sys.stderr.write(f"memd-activity-hook: {type(e).__name__}\n")
        except BaseException:  # noqa: BLE001
            pass
    return 0
# --- end shared ---


def _local_fetch(query, host, k, timeout):
    """In-process recall for a machine that holds the store (no MEMD_REMOTE)."""
    from memd.config import Config
    from memd.recall import recall as core_recall
    profile = os.environ.get("MEMD_PROFILE") or default_profile()
    env = dict(os.environ)
    env["MEMD_PROFILE"] = profile
    kw = {"host": host} if host else {}
    notes = core_recall(query, profile=profile, k=k, cfg=Config.from_env(env),
                        include_core=False, **kw)
    return [n.to_dict() for n in notes], None


def fetch(query, host, k, timeout):
    if os.environ.get("MEMD_REMOTE"):
        return remote_fetch(query, host, k, timeout)
    return _local_fetch(query, host, k, timeout)


def main() -> int:
    return run(sys.stdin, sys.stdout, fetch)


if __name__ == "__main__":
    raise SystemExit(main())
