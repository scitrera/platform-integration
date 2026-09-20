# SPDX-License-Identifier: AGPL-3.0-only
ARG PYTHON_IMAGE=python:3.13-alpine@sha256:1a63a53928ce53d2b0baf08092a703f4840ac5dfbd61fd48802dbf48e08c801e
FROM ${PYTHON_IMAGE}
WORKDIR /app
COPY runtime/auth_gate.py /app/auth_gate.py
COPY LICENSE /usr/share/licenses/platform-integration/LICENSE
USER 65532:65532
EXPOSE 8080
ENTRYPOINT ["python3", "-B", "/app/auth_gate.py"]
