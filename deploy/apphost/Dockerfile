FROM python:3.13-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git openssh-client ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# memd shells out to git for its per-profile clone; it must trust the Forgejo host key
# and use the mounted deploy identity, never the ambient agent.
ENV GIT_SSH_COMMAND="ssh -i /run/secrets/memd_forgejo -o StrictHostKeyChecking=accept-new -o IdentitiesOnly=yes -o UserKnownHostsFile=/tmp/known_hosts"
ENV PYTHONUNBUFFERED=1 HOME=/home/memd

WORKDIR /app
COPY src/ /app/
RUN pip install --no-cache-dir . && useradd -m -u 1000 -d /home/memd memd

# Stamp the image with the commit it was built from (deploy.sh passes it).
# /health reports this as app_commit so memd-doctor can catch deploy drift
# over plain HTTP -- a silently stale image is otherwise invisible.
ARG GIT_SHA=unknown
ENV MEMD_GIT_SHA=$GIT_SHA

USER memd
EXPOSE 8077
CMD ["uvicorn", "memd.server:app", "--host", "0.0.0.0", "--port", "8077"]
