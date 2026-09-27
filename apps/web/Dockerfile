# OXBOW web app (P8) -- the Next.js 15 application in apps/web.
#
# Build context is `./apps/web` and the file is `Dockerfile`, exactly as the `web` service in
# docker-compose.yml declares. Two stages: install-and-build on Bun, then run on Node. Nothing
# but Node's runtime and the build output reaches the final image.
#
# Supply chain (02 §F, plan §T2 as amended by DEV-022): base images pinned by tag *and* digest,
# resolved with `docker buildx imagetools inspect` on 2026-09-27. The install is
# `--frozen-lockfile`, so every package is the one whose integrity hash is in bun.lock, and Bun
# itself is the version the `packageManager` field names (1.4.2) rather than whatever `latest`
# means this week. tests/integration/test_p7_worker.py checks every FROM line carries a digest,
# and tests/unit/test_p0_toolchain.py checks this file agrees with package.json and the Makefile.
#
# WHY THE RUNTIME IS STILL NODE (DEV-022): Bun owns resolution and task running here -- it is the
# package manager that actually exists on the development host, where pnpm never did. The process
# that answers a request is the one `engines.node` names and the one every prior web gate
# measured, so "the artifact under test is the artifact served" survives the swap. The build step
# does run on Bun (this image carries no Node), which is what the runtime stage below re-validates
# by serving the build with Node: measured, `GET / -> 307 /dashboard -> 200`, `Ready in 2.4s`.

FROM oven/bun:1.4.2-debian@sha256:4f6e31d1a54d6a3dd312daef655fc998101b5043d52e12592ac293ef04b9bc73 AS build

# The official Bun image switches to the unprivileged `bun` user, whose home is the only path it
# can write, so the build lives there instead of a root-owned /build that would fail at install.
WORKDIR /home/bun/app

# Manifests alone first, so the dependency layer survives every source edit.
COPY --chown=bun:bun package.json bun.lock ./

ENV NEXT_TELEMETRY_DISABLED=1
RUN bun install --frozen-lockfile

# Explicit paths, not `COPY . .`: this repository has no .dockerignore, and `apps/web/` carries a
# host-built node_modules (Windows binaries, including the @tailwindcss/oxide native package),
# `.next/` and `tsconfig.tsbuildinfo`. Copying the directory would put those in the image, and the
# failure would look like a code defect rather than a context error.
COPY --chown=bun:bun src ./src
COPY --chown=bun:bun public ./public
COPY --chown=bun:bun next.config.ts biome.json postcss.config.mjs tsconfig.json package.json ./

# NEXT_PUBLIC_* values are inlined into the client bundle at build time, so the address the browser
# is told to call has to exist during the build. A runtime `environment:` entry alone would never
# reach the bundle; compose sets the same value and it is threaded through here so the built client
# and the running container cannot disagree.
ARG NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:8000
ENV NEXT_PUBLIC_API_BASE_URL=${NEXT_PUBLIC_API_BASE_URL}

# `bun run build` is `next build`, which type-checks as it compiles: a type error fails the image
# instead of producing a container that only breaks when someone clicks.
RUN bun run build

# ---------------------------------------------------------------------------

FROM node:22-slim@sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c AS runtime

WORKDIR /srv

# next.config.ts does not set `output: "standalone"`, so the runtime needs the installed modules as
# well as the build output: `next start` serves `.next` through them. Switching that config to
# standalone would shrink this image by an order of magnitude; the file is not part of this change,
# so the copy is honest about what it needs instead of quietly broken.
#
# Bun's default linker is hoisted, so this is an ordinary node_modules that Node resolves with no
# `.pnpm` store and no Bun-specific loader.
COPY --from=build --chown=node:node /home/bun/app/node_modules ./node_modules
COPY --from=build --chown=node:node /home/bun/app/.next ./.next
COPY --from=build --chown=node:node /home/bun/app/src ./src
COPY --from=build --chown=node:node /home/bun/app/public ./public
COPY --from=build --chown=node:node /home/bun/app/package.json /home/bun/app/next.config.ts /home/bun/app/postcss.config.mjs ./

ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
# Next 15 binds to localhost when HOSTNAME is unset, which inside a container is precisely the one
# address nothing outside the container can reach.
ENV HOSTNAME=0.0.0.0
ENV PORT=3000
# The server-side proxy in next.config.ts (`/api/*` -> the API) reads this at runtime. It is a
# different fact from the browser's base URL above, and compose passes both.
#
# This used to be `http://127.0.0.1:8000`, which inside this container is the web app itself: every
# server-rendered API call had no API behind it. Measured from this service -- `api:8000/healthz`
# answers 200 and `127.0.0.1:8000/healthz` answers ECONNREFUSED. `api` is the compose service
# name, and compose is the only topology this repository deploys, so the baked default now names
# something real.
ENV OXBOW_API_ORIGIN=http://api:8000

EXPOSE 3000

# The `start` package script is `next start --port 3000`, the port compose maps. Spelled as the
# direct Node invocation rather than `bun run start` because this stage is the Node one: Bun is not
# in it, and the server process is deliberately Node's (see the header).
USER node
CMD ["node", "node_modules/next/dist/bin/next", "start", "--port", "3000"]
