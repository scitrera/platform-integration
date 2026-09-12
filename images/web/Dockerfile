# SPDX-License-Identifier: AGPL-3.0-only
# Build context is the frontend's packaged asset directory, including source.tar.gz.
ARG NGINX_IMAGE
FROM ${NGINX_IMAGE}
COPY . /usr/share/nginx/html/
RUN test -s /usr/share/nginx/html/source.tar.gz
LABEL org.opencontainers.image.source="https://github.com/scitrera/platform-integration" \
      org.opencontainers.image.licenses="AGPL-3.0-only AND BSD-2-Clause"
