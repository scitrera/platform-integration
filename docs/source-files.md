# Source review files

Source-review images use the existing storage HTTP data plane. Common Compose config supplies MemoryLayer's source export, edge and restricted fetch endpoints. No additional public port or LLM credential is required.

Deploy compatible versions of MemoryLayer enterprise, platform backend, Sahara and sandbox-provider together. The backend returns small file descriptors instead of inline image bytes. Sahara materializes and verifies files in a task-scoped filesystem, rechecks source permission, and constructs visual model input locally.

The Docker provider persists files in an owner-specific `task-files` directory mounted only into Sahara at `/task-files`. It is separate from the shared `/sahara` workspace and is not mounted into the code-sidecar. Shared work profiles expose bounded task-local scratch tools; general filesystem and code execution remain excluded.

Page exports follow canonical document deletion. Local task files expire after eight hours; cleanup runs at startup and every five minutes. Published application outputs/evaluation records live separately. The task's authority is checked on every access and can expire before local files are deleted.

Existing local document blobs do not need to be migrated. Kubernetes/gVisor deployments require an equivalent harness-only persistent volume and explicit `SAHARA_SOURCE_FILES_FETCH_URL`/`SAHARA_TASK_FILES_ROOT` configuration; the new automatic mount is currently Docker-specific. Ordinary chat-thread scratch is not enabled by this rollout.
