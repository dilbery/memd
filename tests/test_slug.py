import pytest

from memd.slug import slugify


@pytest.mark.parametrize(
    "title,expected",
    [
        ("gpuhost inference tuning", "gpuhost-inference-tuning"),
        ("Hermes context stall fix", "hermes-context-stall-fix"),
        ("embed-model:300m / dim==768", "embed-model-300m-dim-768"),
        ("  Leading & trailing  ", "leading-trailing"),
        ("Multiple---hyphens___here", "multiple-hyphens-here"),
        ("10.10.1.11 vmhost VM", "10-10-1-11-vmhost-vm"),
        ("CamelCase Words", "camelcase-words"),
        ("", ""),
        ("!!!", ""),
    ],
)
def test_slugify(title, expected):
    assert slugify(title) == expected


def test_idempotent():
    once = slugify("Trackr v0.3.2 status")
    twice = slugify(once)
    assert once == twice == "trackr-v0-3-2-status"


def test_lowercases_and_hyphenates():
    assert slugify("Trackr Docker Host") == "trackr-docker-host"


def test_non_alnum_collapses_to_single_hyphen():
    assert slugify("Trackr Docker Host: 10.10.1.11") == "trackr-docker-host-10-10-1-11"


def test_strips_repeat_and_edge_hyphens():
    assert slugify("  --Hello___World!!  ") == "hello-world"


def test_stable_across_trivial_edits():
    # punctuation/case/whitespace changes must NOT change the slug (dedup key)
    assert slugify("gpuhost inference tuning") == slugify("Gpuhost  Inference, Tuning")


def test_empty_title_is_empty_slug():
    assert slugify("") == ""
    assert slugify("!!!") == ""
