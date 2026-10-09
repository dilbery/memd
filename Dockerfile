FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends git openssh-client ca-certificates \
    && rm -rf /var/lib/apt/lists/* && useradd -m -u 1000 memd \
    && mkdir /data /vaults && chown memd:memd /data /vaults
WORKDIR /app
COPY --chown=memd:memd . /app
# Install the pinned dependency set FIRST, then the package with --no-deps,
# so pyproject's open `>=` ranges cannot re-resolve around requirements.lock
# on a rebuild. The lock file was already in the tree but nothing used it.
RUN pip install --no-cache-dir -r requirements.lock \
    && pip install --no-cache-dir --no-deps .
ENV PYTHONUNBUFFERED=1 MEMD_DATA_DIR=/data MEMD_ADMIN_DB=/data/control/admin.db MEMD_VAULT_ROOTS=/vaults
USER memd
EXPOSE 8077
CMD ["python", "-m", "memd.bootstrap"]
