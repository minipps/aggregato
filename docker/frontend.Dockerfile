# The frontend is its own deployable image: Nginx serves the immutable SPA and proxies API requests
# to the backend service. The development overlay selects the earlier Node stage instead.
# Base images use exact version tags plus immutable manifest digests, matching the backend image.

# --- Stage 1: build the SPA --------------------------------------------------------------------
FROM node:26.12.0-slim@sha256:e556a144463cc8dc53afe8de6d2fa0db5c132fd39ffa232c17641bd40df2bf11 AS frontend-build
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json frontend/.npmrc ./
RUN npm install --global npm@12.2.0 --silent && npm --loglevel=error ci
COPY frontend/ ./
RUN npm --loglevel=error run build

# --- Stage 2: development image ----------------------------------------------------------------
# compose.dev.yml selects this target, then bind-mounts frontend/ over /app. Keeping the installed
# dependencies in the image means the named node_modules volume is ready for Vite immediately.
FROM frontend-build AS development
EXPOSE 80

# --- Stage 3: production static server ---------------------------------------------------------
FROM nginx:1.31.8-alpine-slim@sha256:f45171d36c76a8b2f5ef97108a3a8a2294248a6578a0a180edc2d78f7d2edc26 AS production
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=frontend-build /app/dist /usr/share/nginx/html

EXPOSE 80
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD wget --spider --quiet http://127.0.0.1/ || exit 1
