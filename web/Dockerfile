# syntax=docker/dockerfile:1.7
# Multi-stage build for blender.bet (BlenderMCP user-facing web).
#   dev     -> Node + Astro dev server with HMR (source mounted by compose)
#   builder -> runs `astro build`, producing /app/dist (server + client)
#   prod    -> Node 22-alpine running the standalone Astro server
#
# Unlike docs-site (Starlight static), this site is Astro SSR-first
# because the value proposition is /login-complete reading per-request
# Authentik forward_auth headers. The Node adapter in `standalone` mode
# serves both static and SSR routes from one process.

ARG NODE_VERSION=22-alpine

# ---------------------------------------------------------------------------
# dev stage
# ---------------------------------------------------------------------------
FROM node:${NODE_VERSION} AS dev
ENV ASTRO_TELEMETRY_DISABLED=1 \
    NODE_ENV=development
WORKDIR /app
RUN apk add --no-cache libc6-compat
COPY package.json ./
RUN npm install --no-audit --no-fund
COPY . .
EXPOSE 4321
CMD ["npm", "run", "dev"]

# ---------------------------------------------------------------------------
# builder stage
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
# prod stage
# ---------------------------------------------------------------------------
FROM node:${NODE_VERSION} AS prod
ENV ASTRO_TELEMETRY_DISABLED=1 \
    NODE_ENV=production \
    HOST=0.0.0.0 \
    PORT=4321
WORKDIR /app

# node:22-alpine ships with a built-in ``node`` user at uid/gid 1000;
# reuse it rather than creating another at the same id (which fails).
COPY --from=builder --chown=node:node /app/dist ./dist
COPY --from=builder --chown=node:node /app/node_modules ./node_modules
COPY --from=builder --chown=node:node /app/package.json ./
USER node

EXPOSE 4321
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD wget -qO- http://127.0.0.1:4321/ -O /dev/null || exit 1

CMD ["node", "./dist/server/entry.mjs"]
