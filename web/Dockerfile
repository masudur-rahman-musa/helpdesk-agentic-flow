# syntax=docker/dockerfile:1

# The Next app, built from this directory:
#
#   docker build -t helpdesk-web:local web
#
# Separate from the root Dockerfile because it shares
# nothing with the three Python services: no uv install, a different package
# manager, and a compile step none of them has.
#
# The runtime stage copies Next's standalone output rather than node_modules
# (decision E1). `output: "standalone"` in next.config.ts makes Next trace the
# files the server actually imports and emit a self-contained tree; without it
# this image carries the whole dependency tree for a server that needs a
# fraction of it. The two halves are pinned together by
# packages/helpdesk/tests/test_container_build.py::
# test_the_web_image_is_built_from_the_standalone_output.

ARG NODE_IMAGE=node:22.12.0-bookworm-slim


# --- dependency stage --------------------------------------------------------
FROM ${NODE_IMAGE} AS deps

RUN corepack enable

WORKDIR /app

# The manifests alone, so an install layer survives every source-only change.
# --frozen-lockfile: a manifest that drifted from the lockfile fails here
# rather than resolving to a tree nobody tested, the same contract `make
# web-verify` holds on a developer's machine.
COPY package.json pnpm-lock.yaml ./
RUN --mount=type=cache,target=/root/.pnpm-store \
    pnpm config set store-dir /root/.pnpm-store \
 && pnpm install --frozen-lockfile


# --- build stage -------------------------------------------------------------
FROM ${NODE_IMAGE} AS builder

RUN corepack enable

WORKDIR /app

COPY --from=deps /app/node_modules ./node_modules
COPY . .

# Telemetry is off in the build as well as at runtime: a build that phones home
# is a build that fails in an air-gapped CI runner for no useful reason.
ENV NEXT_TELEMETRY_DISABLED=1
RUN pnpm build


# --- runtime stage -----------------------------------------------------------
FROM ${NODE_IMAGE} AS runtime

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

# The same fixed uid the Python images use, for the same reason: predictable
# ownership on a bind mount across machines. The node image ships its own
# `node` user on 1000, which is not it.
RUN groupadd --gid 10001 helpdesk \
 && useradd --uid 10001 --gid 10001 --home-dir /nonexistent --no-create-home helpdesk

WORKDIR /app

# Both pieces of a standalone deployment. `server.js` and its traced
# dependencies come from `.next/standalone`; `.next/static` is emitted outside
# that tree and is not traced into it, so a build that copies only the first
# serves an app with no CSS. There is no `public/` in this app -- every asset
# is imported through the bundler -- so nothing copies one; add the line with
# the directory, not before it, or the build fails on a missing path.
COPY --from=builder --chown=10001:10001 /app/.next/standalone ./
COPY --from=builder --chown=10001:10001 /app/.next/static ./.next/static

# After the last COPY, so no later --chown puts the process back on uid 0.
# Pinned by test_container_build.py::test_the_web_dockerfile_runs_as_a_non_root_user.
USER 10001:10001

EXPOSE 3000

# The standalone server, not `next start`: the Next CLI is not in this image.
CMD ["node", "server.js"]
