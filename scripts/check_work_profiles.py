#!/usr/bin/env python3
"""Verify per-user gateway accounting from the shared-worker browser acceptance."""
# SPDX-License-Identifier: AGPL-3.0-only
import argparse
import json
from pathlib import Path
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--record", type=Path, required=True)
    args = parser.parse_args()
    env = dict(line.split("=", 1) for line in (ROOT / ".local/compose.env").read_text().splitlines() if "=" in line)
    if not (ROOT / ".local/fixtures.enabled").is_file() or not args.project.startswith("platform-smoke-") or env.get("COMPOSE_PROJECT_NAME") != args.project:
        parser.error("Requires the exact disposable fixture Compose project")
    record = json.loads(args.record.read_text())
    assert record["users"] == ["alice@example.test", "denied@example.test"]
    expected_threads = dict(zip(record["users"], record["threads"]))
    tasks = {item["taskId"]: item for item in record["started"]}
    assert set(item["user"] for item in tasks.values()) == set(record["users"])
    count = 0
    for task, item in tasks.items():
        uuid.UUID(task)
        sql = "SELECT row_to_json(r) FROM (SELECT tenant_id,principal_id,attribution_json,http_status,input_tokens,output_tokens,total_tokens,stream FROM llm_requests WHERE attribution_json->>'sparkroute.task_id'='" + task + "' AND requested_model='sahara-default' AND completed_at IS NOT NULL) r;"
        deadline = time.monotonic() + 15
        while True:
            result = subprocess.run(["docker", "exec", "-i", args.project + "-sparkroute-postgres-1",
                "psql", "-U", "sparkroute", "-d", "sparkroute", "-At"], input=sql, text=True, capture_output=True, check=True, timeout=15)
            rows = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
            if rows or time.monotonic() >= deadline:
                break
            time.sleep(0.2)
        assert rows, "Missing completed gateway accounting for a profile task"
        for row in rows:
            assert row["tenant_id"] == "alpha" and row["principal_id"] == "platform-alpha"
            attrs = row["attribution_json"]
            for key, value in {"user": "user:" + item["user"], "source": "sahara", "tenant": "alpha",
                    "workspace": "default", "thread_id": expected_threads[item["user"]], "task_id": task}.items():
                assert attrs.get("sparkroute." + key) == value, "Incorrect shared-worker attribution: " + key
            assert row["http_status"] == 200 and row["stream"]
            assert [row[key] for key in ["input_tokens", "output_tokens", "total_tokens"]] == [10, 4, 14]
            count += 1
    print("Verified shared-worker accounting for", len(tasks), "tasks,", count, "model calls and two distinct users")

if __name__ == "__main__":
    main()
