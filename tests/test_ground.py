from memd.ground import ground
from memd.store import Note, parse_note


def _checker(present: set[str]):
    """Stub local host-checker: only names in `present` exist (no live which)."""
    return lambda kind, target: target in present


def test_gpuhost_with_missing_command_flags_unverified_local(fixtures_dir):
    """parux hallucination: gpuhost note references `parux`, which does NOT exist."""
    note = parse_note(fixtures_dir / "project_aur_supply_chain_defense.md")
    note.host = "gpuhost"  # scope it local (the real note is about gpuhost+lapbox)
    # pkgaudit + paru exist on the box; parux does NOT
    result = ground(note, host_checker=_checker({"pkgaudit", "paru"}))
    assert result == "unverified-local"


def test_gpuhost_with_all_commands_present_is_ok():
    note = Note(title="t", slug="t", path="t.md",
                body="run `pkgaudit` then `paru`", host="gpuhost")
    result = ground(note, host_checker=_checker({"pkgaudit", "paru"}))
    assert result == "ok"


def test_vmhost_docker_exec_is_unverified_remote_never_flagged(fixtures_dir):
    """vmhost docker-exec fact: remote host -> NO local check, NEVER unverified-local."""
    note = parse_note(fixtures_dir / "project_trackr_login_reset.md")
    note.host = "vmhost"
    called = {"n": 0}

    def spy(kind, target):
        called["n"] += 1
        return False

    result = ground(note, host_checker=spy)
    assert result == "unverified-remote"
    assert called["n"] == 0  # local checker never invoked for a remote note


def test_host_any_is_unverified_remote():
    note = Note(title="t", slug="t", path="t.md", body="`anything`", host="any")
    assert ground(note, host_checker=_checker(set())) == "unverified-remote"


def test_grounding_is_advisory_returns_value_does_not_raise():
    note = Note(title="t", slug="t", path="t.md", body="`nonexistent_cmd`",
                host="gpuhost")
    # must return a value, never raise (advisory, never blocks the write)
    assert ground(note, host_checker=_checker(set())) == "unverified-local"
