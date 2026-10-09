---
name: project-trackr-login-reset
description: Fresh-deploy Trackr login reset on 10.10.1.11 — wipe users/passwords/setup keys
type: project
metadata:
  node_type: memory
  source: hermes
---
# Trackr login reset procedure

Fresh-deploy reset on the Trackr host 10.10.1.11 (vmhost). To clear a stuck
login, DELETE the users / passwords / `setup%` keys from the SQLite DB inside the
container, then restart it. Run it over SSH on the dev host:

```bash
ssh svcuser@10.10.1.11 docker exec -i trackr sqlite3 /data/state.db \
  "DELETE FROM users; DELETE FROM passwords; DELETE FROM kv WHERE key LIKE 'setup%';"
ssh svcuser@10.10.1.11 docker restart trackr
```

The `docker exec` runs against the container on svcuser@10.10.1.11, never on the
gpuhost box.
