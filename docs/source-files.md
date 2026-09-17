# Source review files

Source-review images use the restricted storage HTTP proxy. Common Compose config supplies MemoryLayer's `MEMORYLAYER_SOURCE_FILES_FETCH_URL` and tenant ID. MemoryLayer authorizes the owning workspace, then signs a page-specific capability valid for 120 seconds. The proxy resolves only `memorylayer-<tenant>:8000`, converts the capability to an Authorization header, and serves canonical page bytes without creating duplicate storage objects. No additional public port or LLM credential is required.

Deploy compatible versions of MemoryLayer enterprise, platform backend, Sahara and sandbox-provider together. The backend returns small file descriptors instead of inline image bytes. Sahara materializes and verifies files in a task-scoped filesystem, rechecks source permission, and constructs visual model input locally.

The Docker provider persists files in an owner-specific `task-files` directory mounted only into Sahara at `/task-files`. It is separate from the shared `/sahara` workspace and is not mounted into the code-sidecar. Shared work profiles expose bounded task-local scratch tools; general filesystem and code execution remain excluded.

Deleted, changed, expired or cross-tenant source capabilities fail closed. Local task files expire after eight hours; cleanup runs at startup and every five minutes. Published application outputs/evaluation records live separately. The task's authority is checked on every access and can expire before local files are deleted.

Existing local document blobs do not need to be migrated. The default signing key is private to each MemoryLayer process: a restart invalidates outstanding capabilities. Multiple workers/replicas must share `MEMORYLAYER_SOURCE_FILES_SIGNING_KEY` (at least 32 random bytes, supplied as a secret) or use routing affinity. Configure access logs/proxies to omit signed URLs; the included download proxy disables access logs and sends credentials only in headers to MemoryLayer. Kubernetes/gVisor deployments require an equivalent harness-only persistent volume and explicit `SAHARA_SOURCE_FILES_FETCH_URL`/`SAHARA_TASK_FILES_ROOT` configuration; the new automatic mount is currently Docker-specific. Ordinary chat-thread scratch is not enabled by this rollout.

The bridge and Docker provider both need `SANDBOX_BLOB_FETCH_BASE_URL`. The bridge uses it when pushing a shared work profile’s sidecar allowlist; the provider uses it when provisioning the private mount and harness environment. Setting it only on the web/platform service leaves background reviews unable to download sources.
