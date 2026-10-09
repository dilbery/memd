from memd.infer_host import infer_host
from memd.store import Note, parse_note


def test_vmhost_note_infers_vmhost(fixtures_dir):
    note = parse_note(fixtures_dir / "project_trackr_login_reset.md")
    assert infer_host(note) == "vmhost"  # mentions 10.10.1.11 / docker exec on dev host


def test_gpuhost_note_infers_gpuhost(fixtures_dir):
    note = parse_note(fixtures_dir / "project_aur_supply_chain_defense.md")
    # mentions gpuhost + paru/pkgaudit on the local Arch box (no vmhost IP)
    assert infer_host(note) == "gpuhost"


def test_lapbox_keyword_infers_lapbox():
    note = Note(title="t", slug="t", path="t.md",
                body="On lapbox the GPU KDE Plasma stutter fix")
    assert infer_host(note) == "lapbox"


def test_no_host_signal_defaults_any():
    note = Note(title="t", slug="t", path="t.md",
                body="A general feedback note about communication style")
    assert infer_host(note) == "any"


def test_explicit_host_field_is_respected():
    note = Note(title="t", slug="t", path="t.md", body="10.10.1.11 vmhost",
                host="gpuhost")
    # an already-set, non-default host is NOT overwritten
    assert infer_host(note) == "gpuhost"
