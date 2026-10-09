"""Disposable local browser-test server, never connected to production."""
import os
from pathlib import Path
import subprocess
import sys

root = Path(sys.argv[1]).resolve()
root.mkdir(parents=True, exist_ok=True)
for key in list(os.environ):
    if key.startswith("MEMD_"):
        del os.environ[key]
clone = root / "clone"
clone.mkdir(exist_ok=True)
def git(*args):
    subprocess.run(["git", "-C", str(clone), *args],check=True,capture_output=True)
git("init", "-b", "main")
git("config", "user.name", "Browser fixture")
git("config", "user.email", "fixture@example.test")
(clone / "example.md").write_text("---\ntitle: Backup procedures\nslug: backup-procedures\nprofile: amber\nimportance: 3\n---\nRestore from a verified backup.\n")
git("add", "."); git("commit", "-m", "Fixture")
vault = root / "vaults" / "demo"
vault.mkdir(parents=True,exist_ok=True)
(vault / "Research.md").write_text("# Research\nMarkdown, [[links]], and context for agents.\n")
os.environ.update(MEMD_ADMIN_DB=str(root/"control"/"admin.db"),MEMD_CLONE=str(clone),
                  MEMD_DB=str(root/"index.db"),MEMD_PROFILE="amber",MEMD_TOKEN="preview-legacy-token",
                  MEMD_ENV_FILE=str(root/"absent.env"),MEMD_ENFORCE_PROFILE="1",
                  MEMD_EMBED_URL="http://127.0.0.1:9",MEMD_RERANK_URL="http://127.0.0.1:9",
                  MEMD_BACKGROUND_REFRESH="0",MEMD_STARTUP_REFRESH="0",MEMD_VAULT_ROOTS=str(root/"vaults"))
from memd import server,control,refresh
from memd.config import Config
control.create_user("admin","preview-password-only","admin")
refresh.ensure_lexical(Config.from_env())
import uvicorn
uvicorn.run(server.app,host="127.0.0.1",port=int(sys.argv[2]),log_level="warning")
