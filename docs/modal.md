# Optional Modal task orchestration

`scripts/modal_configure.py` adds an ordinary Aether task orchestrator to a
configured Compose installation. It does not deploy models, relocate shared
Sahara work profiles, or contact Modal during configuration.

```sh
python3 scripts/modal_configure.py --tenant example \
  --image registry.example.com/orchestrator-modal:COMPATIBLE_REVISION \
  --worker-image registry.example.com/worker@sha256:IMMUTABLE_DIGEST \
  --worker-gateway aether.example.com:443 --environment main \
  --env-file /private/modal.env
python3 scripts/compose.py config --quiet
python3 scripts/compose.py up -d orchestrator-modal-example
```

Supply a backend orchestrator image with `MODAL_WORKER_TLS_DIR` support. The
helper selects the sandbox launcher, implementation/profile `modal`, tenant
network and an anonymous transport certificate. Remote workers identify with
Aether-issued task tokens. The public TLS route must already exist and present a
certificate trusted by the worker CA and valid for that hostname. Development
certificates cover local names and expire after 30 days.

`--env-file` is optional: credentials may instead come from the tenant-wide
Aether API-key store (`MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET`). If provided, the
file must be owner-only and contain a complete account credential pair. These
are not Modal model proxy credentials. Private registry settings may be supplied
there as `MODAL_REGISTRY_SECRET` and `MODAL_REGISTRY_AUTH`. The worker image must
be accessible to Modal. Source mounts on this host are not remote worker inputs.

The optional `.local/compose.modal.yaml` is included automatically by compose.py.
Stop its service before disabling the overlay. Existing remote sandboxes need
separate task completion/cancellation or termination. Retain this operator-owned
overlay and env file with external routing/secrets backups; the cold backup
allowlist does not include them. Restart the orchestrator after account-key
rotation because the backend caches Modal clients for its lifetime.

Gateway examples in `examples/models/` show separately configured hosted model
credentials, including `Modal-Key` / `Modal-Secret` file references. They are
catalog envelopes: publication uses the catalog CLI and intentional replacement
requires `--expected`. Read [model-catalog.md](model-catalog.md).
