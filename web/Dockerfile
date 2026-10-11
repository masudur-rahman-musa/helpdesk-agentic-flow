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

# Pinned by an exact patch tag for the reason the root Dockerfile gives at its
# own ARG block: a floating minor drifts green and nobody ever sees the bump.
# Held at or above a recorded floor by packages/helpdesk/tests/
# test_container_build.py::test_the_node_base_is_not_older_than_the_recorded_floor,
# and kept three-component by ::test_the_pinned_tags_are_exact_patch_versions.
ARG NODE_IMAGE=node:22.23.3-bookworm-slim


# --- dependency stage --------------------------------------------------------
FROM ${NODE_IMAGE} AS deps

WORKDIR /app

# pnpm arrives from npm rather than through corepack. corepack
# verifies every package manager it installs against signing keys baked into
# the node image, npm has since rotated those keys, and a pinned node image can
# never learn the new ones: `corepack enable` fails on a signature error for
# pnpm@10.20.0 itself, not only for the "latest" it resolves by default. The
# version here is the same one `web/package.json`'s `packageManager` names; the
# two are kept in step by test_container_build.py::
# test_the_web_image_installs_the_pinned_package_manager.
RUN npm install --global pnpm@10.20.0

# The manifests alone, so an install layer survives every source-only change.
# --frozen-lockfile: a manifest that drifted from the lockfile fails here
# rather than resolving to a tree nobody tested, the same contract `make
# web-verify` holds on a developer's machine.
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./

RUN --mount=type=cache,target=/root/.pnpm-store \
    pnpm config set store-dir /root/.pnpm-store \
 && pnpm install --frozen-lockfile


# --- build stage -------------------------------------------------------------
FROM ${NODE_IMAGE} AS builder

# The same pinned pnpm as the deps stage, installed the same way and for the
# same reason. Both lines move together or the two stages run different
# package managers.
RUN npm install --global pnpm@10.20.0

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

# npm is deleted rather than ignored. This stage runs `node server.js` and
# never invokes a package manager: the standalone tree is already traced and
# copied, so nothing here installs, resolves or audits anything. What the
# bundled npm does carry is 8 HIGH findings the node image cannot shed within
# its own patch line (pacote, sigstore, brace-expansion, ip-address,
# picomatch), and a .trivyignore would keep both the findings and the code that
# produces them. Removing it deletes the finding and the attack surface at
# once. Both the package tree and the two entry points go, because the
# symlinks alone would leave `npm` on PATH pointing at nothing. Pinned by
# packages/helpdesk/tests/test_container_build.py::
# test_the_web_runtime_stage_removes_npm, which reads the RUNTIME stage only:
# the same deletion written into the deps or builder stage is discarded with
# that stage and never ships.
RUN rm -rf /usr/local/lib/node_modules/npm /usr/local/bin/npm /usr/local/bin/npx

# The pinned node image still ships perl-base 5.36.0-7+deb12u3, which carries
# three CRITICAL CVEs fixed in deb12u4. Upgrading that one package here clears
# them without waiting for a new node image; drop this once the base has it.
RUN apt-get update \
 && apt-get install -y --only-upgrade --no-install-recommends perl-base \
 && rm -rf /var/lib/apt/lists/*

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
