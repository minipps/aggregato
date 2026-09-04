# The frontend is its own deployable image: Nginx serves the immutable SPA and proxies API requests
# to the backend service. The development overlay selects the earlier Node stage instead.
# Base images use exact version tags plus immutable manifest digests, matching the backend image.

# --- Stage 1: build the SPA --------------------------------------------------------------------
FROM node:26.8.1-slim@sha256:c0753125a3789977aefe869cbebccf70e3cfd7ea84ca48547458f02e4f1d7146 AS frontend-build
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json frontend/.npmrc ./
RUN npm install --global npm@12.0.2 --silent && npm --loglevel=error ci
COPY frontend/ ./
RUN npm --loglevel=error run build

# --- Stage 2: development image ----------------------------------------------------------------
# compose.dev.yml selects this target, then bind-mounts frontend/ over /app. Keeping the installed
# dependencies in the image means the named node_modules volume is ready for Vite immediately.
FROM frontend-build AS development
EXPOSE 80

# --- Stage 3: production static server ---------------------------------------------------------
FROM nginx:1.31.4-alpine-slim@sha256:1870de6d59aafee152589b64404556d2535922cdd998e6dac1c4888c938ed8f9 AS production
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=frontend-build /app/dist /usr/share/nginx/html

EXPOSE 80
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD wget --spider --quiet http://127.0.0.1/ || exit 1
