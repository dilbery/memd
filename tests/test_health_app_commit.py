"""/health must name the commit of the code it is running.

The notes clone's `head` says nothing about the deployed application version --
that gap is how a deployment can run a stale image undetected. A scheduled
deploy-drift check reads `app_commit` over HTTP.
"""
import memd.server as server


def test_env_stamp_wins(monkeypatch):
    monkeypatch.setenv("MEMD_GIT_SHA", "deadbeef" * 5)
    assert server._app_commit() == "deadbeef" * 5


def test_falls_back_to_checkout_git():
    # The dev tree IS a git checkout, so resolution must yield a real sha.
    sha = server._app_commit()
    assert sha != "unknown"
    assert len(sha) == 40


def test_health_carries_app_commit():
    body = server._health(force=True)
    assert body["app_commit"] == server._APP_COMMIT
    assert body["app_commit"]
