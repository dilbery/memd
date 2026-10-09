from memd.store import (
    Note,
    parse_text,
    parse_note,
    iter_notes,
    dump_note,
    index_line,
    list_notes,
    read_note,
    git_head_sha,
)


LEGACY_WITH_H1 = """---
name: project-trackr-docker-host-10-10-1-11
description: Trackr dev Docker containers run on svcuser@10.10.1.11
type: project
metadata:
  node_type: memory
  source: hermes
---
# Trackr Docker Host: 10.10.1.11

All Docker ops target 10.10.1.11.
"""

LEGACY_NO_H1 = """---
name: project-trackr-docker-host-10-10-1-11
description: same fact, no H1 in this one
type: project
---
All Trackr Docker containers are hosted on 10.10.1.11.
"""

V2 = """---
title: gpuhost inference tuning
slug: gpuhost-inference-tuning
profile: amber
host: gpuhost
importance: 4
last_used: 2026-06-16T09:14:00Z
superseded_by: null
tags: [lemonade, gpu]
grounding: ok
---
body here
"""


def test_h1_drives_title_and_slug():
    n = parse_text(LEGACY_WITH_H1, path="project_trackr_docker_host_10_10_1_11.md")
    assert n.title == "Trackr Docker Host: 10.10.1.11"
    assert n.slug == "trackr-docker-host-10-10-1-11"
    assert n.profile == "amber"          # default
    assert n.host == "any"              # legacy default until backfilled
    assert n.importance == 3            # default
    assert n.grounding == "ok"          # default (unchecked)
    assert "10.10.1.11" in n.body


def test_no_h1_falls_back_to_name_for_title_and_slug():
    n = parse_text(LEGACY_NO_H1, path="project_trackr_docker_host_10_10_1_11_2.md")
    # no H1 -> title from legacy `name` -> a DIFFERENT slug than the H1 note
    assert n.title == "project-trackr-docker-host-10-10-1-11"
    assert n.slug == "project-trackr-docker-host-10-10-1-11"


def test_parse_v2_reads_all_fields():
    n = parse_text(V2, path="x.md")
    assert n.title == "gpuhost inference tuning"
    assert n.slug == "gpuhost-inference-tuning"
    assert n.host == "gpuhost"
    assert n.importance == 4
    assert n.last_used == "2026-06-16T09:14:00Z"
    assert n.superseded_by is None
    assert n.tags == ["lemonade", "gpu"]
    assert n.grounding == "ok"


def test_parse_no_frontmatter_uses_filename():
    n = parse_text("just a body, no yaml\n", path="loose_note.md")
    assert n.slug == "loose-note"
    assert n.body.strip() == "just a body, no yaml"


def test_dump_roundtrips_v2_fields():
    n = parse_text(V2, path="x.md")
    text = dump_note(n)
    n2 = parse_text(text, path="x.md")
    assert n2.host == "gpuhost"
    assert n2.importance == 4
    assert n2.slug == "gpuhost-inference-tuning"
    assert n2.body.strip() == "body here"


def test_index_line_format():
    n = parse_text(V2, path="project_x.md")
    line = index_line(n)
    assert line.startswith("- [gpuhost inference tuning](project_x.md) — ")


def test_iter_notes_reads_fixture_corpus(corpus_dir):
    notes = list(iter_notes(corpus_dir))
    assert len(notes) == 4
    slugs = {n.slug for n in notes}
    # the H1 note and the no-H1 note get DIFFERENT slugs (load-bearing for dedup)
    assert "trackr-docker-host-10-10-1-11" in slugs
    assert "project-trackr-docker-host-10-10-1-11" in slugs


# --- Task 5: clone read-path (list_notes / read_note / git_head_sha) ---------


def test_list_notes_parses_all(config):
    notes = list_notes(config.clone)
    slugs = {n.slug for n in notes}
    assert slugs == {
        "gpuhost-inference-tuning", "vmhost-proxmox-vm", "repo-hosting-policy"
    }


def test_frontmatter_defaults_applied(config):
    notes = {n.slug: n for n in list_notes(config.clone)}
    tuning = notes["gpuhost-inference-tuning"]
    assert tuning.title == "gpuhost inference tuning"
    assert tuning.host == "gpuhost"
    assert tuning.importance == 4
    assert tuning.profile == "amber"
    assert tuning.superseded_by is None
    assert "lemonade" in tuning.tags
    assert "Vulkan" in tuning.body
    assert tuning.git_blob  # non-empty blob sha


def test_missing_optional_fields_get_defaults(config, git_clone):
    # A note with only a title gets every default per the frontmatter contract.
    (git_clone / "minimal.md").write_text(
        "---\ntitle: Just A Title\n---\nbody text\n"
    )
    notes = {n.slug: n for n in list_notes(config.clone)}
    m = notes["just-a-title"]
    assert m.profile == "amber"
    assert m.host == "any"          # read-path default (write-path defaults to host)
    assert m.importance == 3
    assert m.superseded_by is None
    assert m.tags == []
    assert m.grounding == "unverified-local"


def test_read_single_note(config):
    n = read_note(config.clone, "vmhost-proxmox-vm")
    assert isinstance(n, Note)
    assert n.host == "vmhost"
    assert n.grounding == "unverified-remote"


def test_git_head_sha(config, git_head):
    assert git_head_sha(config.clone) == git_head()


def test_provenance_and_unknown_metadata_survive_round_trip():
    from memd.store import parse_text, dump_note
    original = '''---
title: Evidence
slug: stable-id
source: physical inspection
observed_at: 2026-09-08
verified_at: 2026-09-09T00:00:00Z
pinned: true
last_used: 2026-09-09
type: project
metadata:
  source: hermes
---
Original evidence.
'''
    note = parse_text(dump_note(parse_text(original, path='legacy.md')), path='legacy.md')
    assert note.source == 'physical inspection'
    assert note.observed_at == '2026-09-08'
    assert note.verified_at == '2026-09-09T00:00:00Z'
    assert note.last_used == '2026-09-09'
    assert note.pinned is True
    assert note.metadata == {'type': 'project', 'metadata': {'source': 'hermes'}}
