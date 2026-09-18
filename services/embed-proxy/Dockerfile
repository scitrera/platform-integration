# SPDX-License-Identifier: AGPL-3.0-only
FROM python:3.12-slim-bookworm
WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY app.py .
USER 1000:1000
EXPOSE 8080
CMD ["uvicorn", "app:factory", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
