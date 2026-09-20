#!/usr/bin/env python3
"""Encrypted off-host logical PostgreSQL backups with a completion manifest.

One process per server. PGHOST/PGPORT/PGUSER/PGPASSWORD are libpq credentials;
RESTIC_* and the object-provider SDK variables configure encrypted storage.
Database names, retention and cron schedule are non-secret configuration.
Does not back up Aether/worker volumes or claim a cross-database transaction.
"""
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

NAME = re.compile(r"[a-z][a-z0-9_]{0,62}\Z")


def run(command, **kwargs):
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
    if result.returncode:
        # stderr can contain a database URI or provider error with credentials.
        raise RuntimeError(command[0] + " failed (exit " + str(result.returncode) + ")")
    return result.stdout


def file_hash(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def backup(databases, *, server, digest, state=Path("/state"), retention_days=30, runner=run):
    if not databases or any(not NAME.fullmatch(name) for name in databases) or len(set(databases)) != len(databases):
        raise ValueError("Supply unique PostgreSQL database names")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", server):
        raise ValueError("Invalid backup server identity")
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("A deployment config digest is required")
    if retention_days < 1:
        raise ValueError("Retention must be positive")
    state = Path(state)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    started = datetime.now(timezone.utc).isoformat()
    # The fixed subdirectory name gives restic a stable path across snapshots.
    with tempfile.TemporaryDirectory(prefix="pg-backup-", dir=state) as temp:
        root = Path(temp)
        runner(["pg_dumpall", "--roles-only", "--file", str(root / "roles.sql")])
        for name in databases:
            target = root / (name + ".dump")
            runner(["pg_dump", "--format=custom", "--compress=3", "--no-owner",
                    "--file", str(target), "--dbname", name])
            runner(["pg_restore", "--list", str(target)])
        manifest = {
            "schemaVersion": 1, "server": server, "databases": databases, "configDigest": digest,
            "startedAt": started, "completedAt": datetime.now(timezone.utc).isoformat(),
            "consistency": "per-database snapshot; no cross-database or non-Postgres state guarantee",
            "files": {p.name: file_hash(p)
                      for p in sorted(root.iterdir()) if p.is_file()},
        }
        (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        result = runner(["restic", "backup", "--json", "--host", server,
                         "--tag", "postgres", "--tag", "config:" + digest, "."], cwd=root)
        lines = [json.loads(line) for line in result.decode().splitlines() if line.strip()]
        summary = next((line for line in lines if line.get("message_type") == "summary"), {})
        snapshot = summary.get("snapshot_id")
        if not snapshot:
            raise RuntimeError("Backup did not return a completed snapshot ID")
        # Verify the uploaded manifest through the remote repository before
        # advancing success/retention. A successful pg_dump alone is not success.
        remote = json.loads(runner(["restic", "dump", snapshot, "/manifest.json"]))
        if remote != manifest:
            raise RuntimeError("Remote backup manifest verification failed")
        receipt = {**manifest, "snapshot": snapshot}
        temporary = state / ".last-success.tmp"
        temporary.write_text(json.dumps(receipt, indent=2) + "\n")
        temporary.replace(state / "last-success.json")
    # Retention only runs after a verified upload. Pruning is deliberately a
    # separate maintenance job: it can use significant CPU/RAM and locks.
    runner(["restic", "forget", "--host", server, "--tag", "postgres",
            "--group-by", "host", "--keep-within", str(retention_days) + "d"])
    return receipt


def due(schedule, moment):
    fields = schedule.split()
    if len(fields) != 5:
        raise ValueError("Expected five-field UTC cron")
    values = [moment.minute, moment.hour, moment.day, moment.month, (moment.weekday() + 1) % 7]
    ranges = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
    matched = []
    for field, value, (low, high) in zip(fields, values, ranges):
        allowed = set()
        for term in field.split(","):
            base, slash, step = term.partition("/")
            if slash and (not step.isdigit() or int(step) < 1):
                raise ValueError("Invalid cron step")
            stride = int(step) if slash else 1
            if base == "*":
                start, end = low, high
            elif "-" in base:
                start, end = map(int, base.split("-"))
            elif base.isdigit():
                start = end = int(base)
            else:
                raise ValueError("Invalid cron field")
            if not low <= start <= end <= high:
                raise ValueError("Cron field out of range")
            allowed.update(range(start, end + 1, stride))
        matched.append(value in allowed)
    # Standard cron OR semantics when both day fields are restricted.
    day = (matched[2] or matched[4]) if fields[2] != "*" and fields[4] != "*" else (matched[2] and matched[4])
    return matched[0] and matched[1] and day and matched[3]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    databases = os.environ["BACKUP_DATABASES"].split(",")
    options = {"server": os.environ["BACKUP_SERVER"], "digest": os.environ["DEPLOYMENT_CONFIG_DIGEST"],
               "retention_days": int(os.environ.get("BACKUP_RETENTION_DAYS", "30"))}
    if args.once:
        backup(databases, **options)
        return
    schedule = os.environ.get("BACKUP_SCHEDULE", "15 3 * * *")
    # Validate even if the current time does not match.
    due(schedule, datetime.now(timezone.utc))
    last_minute = None
    while True:
        now = datetime.now(timezone.utc)
        minute = now.strftime("%Y-%m-%dT%H:%M")
        if minute != last_minute and due(schedule, now):
            last_minute = minute
            try:
                receipt = backup(databases, **options)
                print(json.dumps({"event": "backup_complete", "snapshot": receipt["snapshot"]}), flush=True)
            except Exception as error:
                print(json.dumps({"event": "backup_failed", "error_type": type(error).__name__}), flush=True)
        time.sleep(10)


if __name__ == "__main__":
    main()
