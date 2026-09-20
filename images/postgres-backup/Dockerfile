# SPDX-License-Identifier: AGPL-3.0-only
ARG POSTGRES_IMAGE=postgres:17.11-bookworm@sha256:639ab7ceb90e13123085b741fb31ef493fba25463002f6da665352e7b534b652
FROM ${POSTGRES_IMAGE}
RUN apt-get update && apt-get install -y --no-install-recommends python3 restic ca-certificates && rm -rf /var/lib/apt/lists/*
RUN mkdir -p /state && chown 999:999 /state
COPY runtime/postgres_backup.py /app/postgres_backup.py
ENV PYTHONDONTWRITEBYTECODE=1
COPY LICENSE /usr/share/licenses/platform-integration/LICENSE
USER 999:999
ENTRYPOINT ["python3", "/app/postgres_backup.py"]
