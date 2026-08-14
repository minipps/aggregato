# The frontend is its own deployable image: Nginx serves the immutable SPA and proxies API requests
# to the backend service. The development overlay selects the earlier Node stage instead.
# Base images use exact version tags plus immutable manifest digests, matching the backend image.

# --- Stage 1: build the SPA --------------------------------------------------------------------
FROM node:26.5.1-slim@sha256:deae974a69e140f44f434ab29cb519fb5f8fe250fd364b8ca446bd0761acdc6a AS frontend-build
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
FROM nginx:1.31.3-alpine-slim@sha256:45b82ed5f285b90d63df07ba70430fdd8f25624b416617d9e6dc93412b2006dc AS production
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=frontend-build /app/dist /usr/share/nginx/html

EXPOSE 80
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD wget --spider --quiet http://127.0.0.1/ || exit 1
