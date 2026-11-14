"""Review controls and the Inbox tab of the account web app, without a browser.

tests/web_review.node.cjs runs the page's own scripts (memd/web) under Node
against a small DOM built from index.html and a scripted server. Each scenario
there is one test here; nothing listens on a port and nothing leaves the process.
"""
from pathlib import Path
import shutil
import subprocess

import pytest

SCRIPT = Path(__file__).with_name("web_review.node.cjs")
SCENARIOS = [
    "the_page_loads_the_inbox_script_last",
    "no_inbox_tab_when_signed_out_or_unlocked_with_a_token",
    "inbox_tab_needs_review_on_a_store",
    "a_server_that_reports_no_review_means_write",
    "the_store_picker_lists_only_stores_with_review",
    "the_list_shows_text_flags_counts_and_pages",
    "a_page_that_failed_to_load_is_not_skipped",
    "an_older_list_answer_does_not_replace_a_newer_one",
    "a_reviewer_without_write_sees_the_candidate_and_changes_only_tags_and_importance",
    "a_reviewer_with_write_sends_only_the_fields_that_changed",
    "rejecting_takes_an_optional_reason",
    "an_approved_candidate_shows_its_note_and_who_decided",
    "a_refusal_is_shown_in_the_servers_words",
    "a_second_submit_while_deciding_sends_nothing",
    "a_candidate_that_arrives_after_leaving_it_is_not_shown",
    "the_reject_dialog_never_decides_another_candidate",
    "signing_out_empties_the_inbox",
    "a_four_eyes_store_says_so_and_keeps_the_text_as_proposed",
    "a_refused_self_decision_leaves_the_candidate_open",
    "administration_sets_review_per_store_and_per_token",
    "administration_reads_an_overview_without_review_as_write",
    "the_review_choice_round_trips_as_review_policy",
    "four_eyes_warns_of_too_few_reviewers_and_of_what_is_not_reviewed",
    "store_cards_name_a_policy_that_holds_saves_back",
    "a_policy_saved_in_settings_reaches_the_inbox_without_a_reload",
    "a_failed_check_on_a_node_is_reported_in_a_few_lines",
]


@pytest.fixture(scope="module")
def node():
    found = shutil.which("node")
    if not found:
        pytest.skip("Node is required to run the web app's scripts")
    major = int(subprocess.check_output([found, "--version"], text=True).lstrip("v").split(".")[0])
    if major < 18:
        pytest.skip("Node 18+ is required to run the web app's scripts")
    return found


def test_every_scenario_in_the_script_is_run_here(node):
    listed = subprocess.run([node, str(SCRIPT), "--list"], capture_output=True, text=True, timeout=30)
    assert listed.returncode == 0, listed.stderr
    assert listed.stdout.split() == SCENARIOS


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_web_review(node, scenario):
    run = subprocess.run([node, str(SCRIPT), scenario], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout.strip() == "PASS " + scenario


def test_an_unknown_scenario_fails(node):
    # A typo in the list above must not pass as an empty run.
    assert subprocess.run([node, str(SCRIPT), "no_such_scenario"], capture_output=True, timeout=30).returncode == 2
