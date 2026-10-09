import time

from memd import control
from memd.actor import current_actor


def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMD_ADMIN_DB", str(tmp_path / "control" / "admin.db"))
    control.initialize()
    monkeypatch.setattr(control, "_last_read_prune", 0.0)


def _rows():
    with control.db() as c:
        return [tuple(r) for r in c.execute("SELECT actor, action, target FROM audit ORDER BY id")]


def test_a_bearer_without_a_principal_is_audited_under_its_label(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    token = current_actor.set("laptop")
    try:
        control.audit_read("recall", "amber:3")
    finally:
        current_actor.reset(token)
    control.audit_read("read", "amber:note")
    assert _rows() == [("laptop", "recall", "amber:3"), ("anonymous", "read", "amber:note")]


def test_old_read_entries_expire_and_admin_entries_are_kept(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("MEMD_AUDIT_READ_DAYS", "30")
    old = time.time() - 31 * 86400
    with control.db() as c:
        for action in ("recall", "recall.federated", "read", "export", "login"):
            c.execute("INSERT INTO audit(actor,action,target,created) VALUES('a',?,'t',?)",
                      (action, old))
    control.audit_read("recall", "amber:1")
    assert sorted(a for _, a, _ in _rows()) == ["export", "login", "recall"]


def test_pruning_runs_at_most_hourly(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    control.audit_read("recall", "first")          # prunes, stamps the clock
    with control.db() as c:
        c.execute("INSERT INTO audit(actor,action,target,created) VALUES('a','read','t',0)")
    control.audit_read("recall", "second")         # within the hour: no prune
    assert ("a", "read", "t") in _rows()


def test_a_bad_retention_setting_falls_back(monkeypatch):
    monkeypatch.setenv("MEMD_AUDIT_READ_DAYS", "soon")
    assert control.read_audit_days() == 90.0
    monkeypatch.setenv("MEMD_AUDIT_READ_DAYS", "0")
    assert control.read_audit_days() == 1.0

