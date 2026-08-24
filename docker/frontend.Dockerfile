# The frontend is its own deployable image: Nginx serves the immutable SPA and proxies API requests
# to the backend service. The development overlay selects the earlier Node stage instead.
# Base images use exact version tags plus immutable manifest digests, matching the backend image.

# --- Stage 1: build the SPA --------------------------------------------------------------------
FROM node:26.7.0-slim@sha256:4ebb5ace66f15a24c14c492e01a8beeed4fddf970a856109f5126e703e5fe503 AS frontend-build
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
FROM nginx:1.31.0-alpine-slim@sha256:241b0d0fe06250e026e7a35a008d022c9a1d3bec19442d65cc33b84d0b5dd64d AS production
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=frontend-build /app/dist /usr/share/nginx/html

EXPOSE 80
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD wget --spider --quiet http://127.0.0.1/ || exit 1
