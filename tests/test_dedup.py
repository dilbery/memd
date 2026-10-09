from memd.dedup import build_bm25, find_duplicates, best_match
from memd.store import iter_notes, Note


def test_docker_host_pair_is_a_duplicate_by_bm25(corpus_dir):
    """The two trackr-docker-host fixture notes have DIFFERENT slugs (one has an H1,
    one doesn't) yet near-identical content -> caught by BM25, not slug collision."""
    notes = list(iter_notes(corpus_dir))
    slugs = {n.slug for n in notes}
    assert "trackr-docker-host-10-10-1-11" in slugs
    assert "project-trackr-docker-host-10-10-1-11" in slugs  # different slug

    dups = find_duplicates(notes)
    paired = {(a.slug, b.slug) for a, b in dups} | {(b.slug, a.slug) for a, b in dups}
    assert ("trackr-docker-host-10-10-1-11",
            "project-trackr-docker-host-10-10-1-11") in paired


def test_slug_collision_is_a_duplicate():
    """Two notes that DO share a slug are a duplicate pair."""
    notes = [
        Note(title="X", slug="dupe-slug", path="a.md", body="alpha body one"),
        Note(title="X again", slug="dupe-slug", path="b.md", body="alpha body two"),
    ]
    dups = find_duplicates(notes)
    assert any({a.slug, b.slug} == {"dupe-slug"} for a, b in dups)


def test_bm25_best_match_on_distinct_slug():
    existing = [Note(
        title="Trackr Docker Host",
        slug="trackr-docker-host",
        path="a.md",
        body="All Trackr Docker containers are hosted on 10.10.1.11 svcuser. "
             "Web UI prometheus loki alloy. SSH svcuser@10.10.1.11.",
    )]
    idx = build_bm25(existing)
    candidate = Note(
        title="Trackr containers host",
        slug="trackr-containers-host",  # different slug -> not a slug collision
        path="b.md",
        body="Trackr Docker containers hosted on 10.10.1.11 svcuser prometheus "
             "loki alloy SSH svcuser@10.10.1.11 always use 10.10.1.11 Docker host.",
    )
    match = best_match(idx, existing, candidate)
    assert match is not None
    assert match.slug == "trackr-docker-host"


def test_unrelated_note_does_not_match():
    existing = [Note(title="KDE stutter", slug="kde-stutter", path="k.md",
                     body="GPU DMCUB firmware timeout VRR automatic sg_display")]
    idx = build_bm25(existing)
    candidate = Note(title="Pushover wrapper", slug="pushover", path="p.md",
                     body="pushover wrapper notification hook phone push flaky")
    assert best_match(idx, existing, candidate) is None
