# OXBOW web app (P8) -- the Next.js 15 application in apps/web.
#
# Build context is `./apps/web` and the file is `Dockerfile`, exactly as the `web` service in
# docker-compose.yml declares. Two stages: install-and-build, then run. Nothing but Node's
# runtime and the build output reaches the final image.
#
# Supply chain (02 §F): base images pinned by tag *and* digest, resolved with `docker buildx
# imagetools inspect` on 2026-09-26. The install is `--frozen-lockfile`, so every package is
# the one whose integrity hash is in pnpm-lock.yaml, and pnpm itself is the version the
# `packageManager` field names (9.15.9) via corepack rather than whatever `latest` means this
# week. tests/integration/test_p7_worker.py checks every FROM line carries a digest.

FROM node:22-slim@sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c AS build

WORKDIR /build

# `engines.node` in package.json is >=20; 22-slim satisfies it and is pinned above.
RUN corepack enable

# Manifests alone first, so the dependency layer survives every source edit.
COPY package.json pnpm-lock.yaml ./

# COREPACK_ENABLE_DOWNLOAD_PROMPT=0 keeps corepack's first-run pnpm fetch non-interactive;
# with the prompt on, a build that has never cached that version of pnpm waits for a tty.
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0
ENV NEXT_TELEMETRY_DISABLED=1
RUN pnpm install --frozen-lockfile

# Explicit paths, not `COPY . .`: this repository has no .dockerignore, and `apps/web/`
# carries a host-built node_modules (Windows binaries, including the @tailwindcss/oxide
# native package), `.next/` and `tsconfig.tsbuildinfo`. Copying the directory would put those
# in the image, and the failure would look like a code defect rather than a context error.
COPY --chown=node:node src ./src
COPY --chown=node:node public ./public
COPY --chown=node:node next.config.ts biome.json postcss.config.mjs tsconfig.json package.json ./

# NEXT_PUBLIC_* values are inlined into the client bundle at build time, so the address the
# browser is told to call has to exist during the build. A runtime `environment:` entry alone
# would never reach the bundle; compose sets the same value and it is threaded through here so
# the built client and the running container cannot disagree.
ARG NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:8000
ENV NEXT_PUBLIC_API_BASE_URL=${NEXT_PUBLIC_API_BASE_URL}

# `pnpm build` is `next build`, which type-checks as it compiles: a type error fails the image
# instead of producing a container that only breaks when someone clicks.
RUN pnpm build

# ---------------------------------------------------------------------------

FROM node:22-slim@sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c AS runtime

WORKDIR /srv

# next.config.ts does not set `output: "standalone"`, so the runtime needs the installed
# modules as well as the build output: `next start` serves `.next` through them. Switching
# that config to standalone would shrink this image by an order of magnitude; the file is not
# part of this change, so the copy is honest about what it needs instead of quietly broken.
COPY --from=build --chown=node:node /build/node_modules ./node_modules
COPY --from=build --chown=node:node /build/.next ./.next
COPY --from=build --chown=node:node /build/src ./src
COPY --from=build --chown=node:node /build/public ./public
COPY --from=build --chown=node:node /build/package.json /build/next.config.ts /build/postcss.config.mjs ./

ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
# Next 15 binds to localhost when HOSTNAME is unset, which inside a container is precisely the
# one address nothing outside the container can reach.
ENV HOSTNAME=0.0.0.0
ENV PORT=3000
# The server-side proxy in next.config.ts (`/api/*` -> the API) reads this at runtime. It is a
# different fact from the browser's base URL above, and compose passes both.
ENV OXBOW_API_ORIGIN=http://127.0.0.1:8000

EXPOSE 3000

# `start` is the package script (`next start --port 3000`), which is the port compose maps.
USER node
CMD ["pnpm", "start"]
