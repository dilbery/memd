"""Portable container initialization; live legacy deployments don't use this."""
import os
from pathlib import Path


def main():
    data = Path(os.environ.get("MEMD_DATA_DIR","/data"))
    os.environ.setdefault("MEMD_ADMIN_DB",str(data/"control"/"admin.db"))
    os.environ.setdefault("MEMD_PROFILE","personal")
    os.environ.setdefault("MEMD_CLONE",str(data/"default"/"clone"))
    os.environ.setdefault("MEMD_DB",str(data/"default"/"index.db"))
    os.environ.setdefault("MEMD_ENFORCE_PROFILE","1")
    os.environ.setdefault("MEMD_REQUIRE_RECALL_TOKEN","1")
    from memd import control,sources
    control.initialize()
    profile = control.identifier(os.environ["MEMD_PROFILE"],"Default store ID")
    cfg = {"clone_path":os.environ["MEMD_CLONE"],"db_path":os.environ["MEMD_DB"],"managed":True,"sync_interval":0}
    if not control.store(profile):
        sources.initialize_clone(cfg)
        control.put_store(profile,"Personal memory","local",cfg,create=True)
    password_file = os.environ.get("MEMD_ADMIN_PASSWORD_FILE")
    if password_file:
        with control.db() as c:
            exists = c.execute("SELECT 1 FROM users WHERE role='admin'").fetchone()
        if not exists:
            control.create_user(os.environ.get("MEMD_ADMIN_USERNAME","admin"),Path(password_file).read_text().strip(),"admin")
    os.execvp("uvicorn",["uvicorn","memd.server:app","--host","0.0.0.0","--port","8077"])


if __name__ == "__main__":
    main()
