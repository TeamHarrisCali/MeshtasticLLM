# The Meshtastic LLM bridge as a container image. See docs/setup.md ("Run with Docker") and docker-compose.yml.
#
# Base image: the official Python 3.12 slim image (Debian 12 "bookworm"), pinned by tag AND by digest so a build always starts from
# the same bytes. The digest is the multi-architecture index digest of python:3.12-slim-bookworm, so it works on amd64 and arm64.
# Dependabot (docker ecosystem, .github/dependabot.yml) proposes new digests.
FROM python:3.12-slim-bookworm@sha256:54c85f3c47607a77f32adec749d3c81d1348bf25833671f512b26a9b6d778cb3

# PYTHONUNBUFFERED: log lines reach `docker logs` at once. PYTHONDONTWRITEBYTECODE: nothing is written into the (read-only) code
# folder. MESHLLM_CONTAINER: tells the app it is inside a container, where binding 0.0.0.0 is how the published port reaches it.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MESHLLM_CONTAINER=1 \
    HOME=/tmp

WORKDIR /app

# Dependencies first, so a code change does not reinstall them.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Only what runs: the package, the licence, and the docs and saved evaluation results the dashboard's Evaluation page reads.
COPY LICENSE ./
COPY meshllm/ meshllm/
COPY docs/ docs/
COPY eval_results/ eval_results/
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh

# A numeric, unprivileged user (no login shell, no home). /data holds the database; the backups, map-tile cache and every other
# file the app writes go next to it, so one volume mounted there is all that needs to persist (docker-compose.yml does that). A new
# named volume copies this folder's ownership the first time it is used, so it is writable by this user; a bind-mounted host folder
# must be made writable for uid 10001 by hand.
RUN chmod 0755 /usr/local/bin/entrypoint.sh \
 && groupadd --gid 10001 meshllm \
 && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin meshllm \
 && mkdir /data \
 && chown 10001:10001 /data
USER 10001:10001

EXPOSE 8080

# "Healthy" means the dashboard's status route answers. It does not mean a radio is connected.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/status', timeout=4).read()"]

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
