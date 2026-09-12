# Reference topology and startup phases

The browser reaches a built web bundle, auth-go's public listener and a trusted
tenant gateway at one origin. The gateway strips client identity headers and
uses the private auth verification plane before proxying either WebSocket path.
Auth's operator dashboard has a separate private listener.

Each tenant owns an Aether lite instance with its own CA, MemoryLayer enterprise,
data-connectors, platform server, bridge, tool catalog and allocation provider.
Aether lite uses durable local state and is a single point of failure. It does
not claim full-tier parity. MemoryLayer's enterprise profile is independently
selected and uses PostgreSQL vector, AGE and pg_textsearch extensions.

Shared storage uses capability authorization, persistent refs/policy and S3
objects. Application callers use delegated Aether/Data Connectors VFS authority.
Legacy sidecar HTTP VFS is unsupported until its authenticated task context
publisher is integrated; direct Sahara VFS is the selected path.

Startup order: databases; auth migration; auth API tenant enrollment; tenant
Aether; ACL seed; MemoryLayer/Data Connectors migrations and services; catalog
and strict workspace reconciliation; skills/models; platform services; ingress.
Setup jobs are explicit and bounded. Teardown is never an install/upgrade job.

Docker allocation receives the exact project network and daemon-visible state
path. Only the provider receives the Docker socket (host-level authority).
Kubernetes allocation instead uses namespace-scoped API credentials. Neither
profile advertises gVisor, Modal, HA, or an untested CNI/storage implementation.
