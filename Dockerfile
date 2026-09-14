# syntax=docker/dockerfile:1

# The three Python services of the design from one file.
#
#   docker build --build-arg SERVICE=agent-api   -t helpdesk-agent-api:local .
#   docker build --build-arg SERVICE=mcp-server  -t helpdesk-mcp-server:local .
#   docker build --build-arg SERVICE=scheduler   -t helpdesk-scheduler:local .
#
# One file rather than three near-identical service Dockerfiles because the
# three differ only in a module and a port: everything above that line is one
# uv install of one workspace, and three copies of it means a base bump lands
# in three places or drifts in two.
#
# The Next app is NOT built here. It has no uv install to share and its own
# file builds it from Next's standalone output.
#
# No migration runs in this image (decision D1). a project rule keeps Alembic
# against the local compose database, and an entrypoint that upgraded on start
# would have every replica of a rollout racing the same migration.
#
# The policy corpus is NOT copied in either: it arrives as a read-only runtime
# mount, so the corpus that decides access is not pinned to a build. Pinned by
# packages/helpdesk/tests/test_container_build.py::
# test_the_policy_dir_is_mounted_read_only_into_the_mcp_server.

# Pinned by digest-free exact tags so a rebuild is reproducible within a patch
# line and a bump is a visible diff here. The weekly image scan reports what
# these carry; see .github/workflows/image-scan.yml.
ARG PYTHON_IMAGE=python:3.12.8-slim-bookworm
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.5.11


# The installer arrives as a stage rather than a curl into the build: a global
# ARG is in scope on a FROM line and not inside a stage, and the binary is
# version-pinned either way.
FROM ${UV_IMAGE} AS uv


# --- build stage: resolve and install the workspace into its own venv --------
FROM ${PYTHON_IMAGE} AS builder

COPY --from=uv /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# The manifests first, on their own layer: dependency resolution is the slow
# half of this build and it is invalidated only when a manifest changes, not
# when a source file does.
COPY pyproject.toml uv.lock ./
COPY packages/helpdesk/pyproject.toml packages/helpdesk/
COPY services/agent-api/pyproject.toml services/agent-api/
COPY services/mcp-server/pyproject.toml services/mcp-server/
COPY services/scheduler/pyproject.toml services/scheduler/

# --no-dev: ruff, mypy, pytest and locust have no business in a runtime image.
# --frozen: the lockfile is the contract, so a manifest that drifted from it
# fails the build here rather than resolving to something nobody tested.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace

COPY packages/ packages/
COPY services/ services/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


# --- runtime stage: the venv, the source, and one unprivileged account -------
FROM ${PYTHON_IMAGE} AS runtime

# The service this image runs. Validated below, so a typo fails the build in
# seconds instead of producing an image that only fails when a container starts.
ARG SERVICE
ENV SERVICE=${SERVICE}

RUN set -eu; \
    case "${SERVICE}" in \
      agent-api|mcp-server|scheduler) ;; \
      *) echo "unknown SERVICE '${SERVICE}': expected agent-api, mcp-server or scheduler" >&2; \
         exit 1 ;; \
    esac

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# A fixed uid so a bind mount's ownership is predictable across machines, and a
# home under /nonexistent so nothing writes a cache into a layer.
RUN groupadd --gid 10001 helpdesk \
 && useradd --uid 10001 --gid 10001 --home-dir /nonexistent --no-create-home helpdesk

WORKDIR /app

COPY --from=builder --chown=10001:10001 /app /app

# After the last COPY, so no later --chown puts the process back on uid 0.
# Pinned by test_container_build.py::test_the_root_dockerfile_runs_as_a_non_root_user.
USER 10001:10001

# Documentation only, and all three are listed because the instruction cannot
# branch on a build argument. The port a container actually publishes is set by
# docker-compose.yml and by the companion console's manifests.
EXPOSE 8000 8081 8082

# The second guard, against a SERVICE overridden at `docker run` time rather
# than at build time: the case above cannot see that, and an unmatched value
# here exits non-zero instead of starting nothing and looking healthy.
# `exec` so the server is pid 1 and receives SIGTERM directly.
ENTRYPOINT ["/bin/sh", "-c", "\
case \"$SERVICE\" in \
  agent-api) exec uvicorn agent_api.main:app --host 0.0.0.0 --port 8000 ;; \
  mcp-server) exec uvicorn mcp_server.main:app --host 0.0.0.0 --port 8081 ;; \
  scheduler) exec uvicorn scheduler.main:app --host 0.0.0.0 --port 8082 ;; \
  *) echo \"unknown SERVICE '$SERVICE'\" >&2; exit 1 ;; \
esac"]
