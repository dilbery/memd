---
name: project-trackr-docker-host-10-10-1-11
description: Trackr dev Docker containers run on svcuser@10.10.1.11 (same fact, no H1)
type: project
metadata:
  node_type: memory
  source: hermes
---
All Trackr Docker containers are hosted on 10.10.1.11. The Trackr development
Docker containers run on the dedicated host svcuser@10.10.1.11 (vmhost, a Proxmox
VM), not on the gpuhost local-LLM box. Use `ssh svcuser@10.10.1.11` then
`docker compose ps` / `docker exec` against the Trackr container there. The web
UI, prometheus, loki, and alloy all live on 10.10.1.11. Always deploy Trackr to
10.10.1.11.
