# syntax=docker/dockerfile:1.7
# Multi-stage build for the BlenderMCP docs site.
#   dev     -> Node + Astro dev server with HMR (mounted source via compose)
#   builder -> runs `astro build` to produce static files in /app/dist
#   prod    -> Caddy 2.10-alpine serving the static build out of /srv
#
# Telemetry is disabled at the env layer (no astro.config.mjs key for it).

ARG NODE_VERSION=22-alpine
ARG CADDY_VERSION=2.10-alpine

# ---------------------------------------------------------------------------
# dev stage — used by docker-compose `docs-dev` service for hot reload
# ---------------------------------------------------------------------------
FROM node:${NODE_VERSION} AS dev
ENV ASTRO_TELEMETRY_DISABLED=1 \
    NODE_ENV=development
WORKDIR /app
RUN apk add --no-cache libc6-compat
COPY package.json ./
# Allow either pinned or fresh installs — compose mounts /app over this anyway
RUN npm install --no-audit --no-fund
COPY . .
EXPOSE 4321
CMD ["npm", "run", "dev"]

# ---------------------------------------------------------------------------
# builder stage — produces /app/dist for the prod image
# ---------------------------------------------------------------------------
FROM node:${NODE_VERSION} AS builder
ENV ASTRO_TELEMETRY_DISABLED=1 \
    NODE_ENV=production
WORKDIR /app
RUN apk add --no-cache libc6-compat
COPY package.json ./
RUN npm install --no-audit --no-fund
COPY . .
RUN npm run build

# ---------------------------------------------------------------------------
# prod stage — Caddy serves the static site
# ---------------------------------------------------------------------------
FROM caddy:${CADDY_VERSION} AS prod
COPY Caddyfile /etc/caddy/Caddyfile
COPY --from=builder /app/dist /srv
EXPOSE 80
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD wget -qO- http://127.0.0.1/health || exit 1
