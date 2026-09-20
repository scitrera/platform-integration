# Replicated browser sessions

`platform-sessions` installs two bounded Valkey data servers and three Sentinel
voters. Put the data copies on two independent nodes and the voters on three.
Required host anti-affinity prevents silently co-locating copies. All endpoints
are private, use a digest-pinned image, and allow only same-namespace auth clients
and session peers through NetworkPolicy. Ensure the cluster enforces those
policies. Voters need no Kubernetes credentials or application database access.

The chart uses AOF on retained data PVCs and persisted Sentinel configuration.
A starting data server first asks a majority which primary to join. An empty
server selected as primary refuses startup by default. For a fresh deployment
only, install with `allowEmptyBootstrap=true`, wait for all five pods to become
ready, then upgrade with `allowEmptyBootstrap=false` before connecting auth.
Do not leave that one-time override in the production values file.

The running primary periodically checks majority authority and stops after two
failed observations. This bounds minority service during a partition, at the cost
of temporarily failing closed when voters are unreachable. It is not a
linearizable session database. Async replication can lose recent login/logout
writes. Never restore a historical session snapshot to undo a revocation. If both
data copies are unrecoverable, explicitly reset the entire session set, bootstrap
empty, disable bootstrap again, and require fresh logins. A restart alone must
not authorize an empty primary to overwrite a surviving replica.

Configure `platform-shared.authSession.sentinel.master` and `.addresses` to use
this release; the default standalone session Deployment/PVC is then omitted.
The auth image must include Aether's published Sentinel-aware session client.
Set `authReplicas: 2` and opt-in `authTolerations` for the secondary shared pool.
Rolling auth updates allow one unavailable replica to avoid deadlock with strict
host anti-affinity on two nodes. A disruption budget retains one available auth
pod. PostgreSQL and ingress redundancy are separate dependencies.

`tests/acceptance/session_ha.py` is a disruptive acceptance test for a newly
created, isolated session release. It promotes the replica, restarts the old
primary, checks synthetic session preservation and revocation, removes voter
quorum, and restores it. It requires an explicit context/namespace/release and
`--allow-disruption`; do not run against active browser sessions. It writes timing
and idle resource evidence, which is not a loaded capacity estimate. For client
reconnection, Aether also has an isolated Go session-store failover test.

Initial data-server limits are 384 MiB each with a 128 MB dataset cap and
`noeviction`; Sentinel limits are 64 MiB each. Measure loaded memory including
replication buffers and AOF rewriting before increasing dataset size. Defaults
reserve headroom; hitting the dataset limit rejects new writes instead of
silently evicting existing sessions.

The new session chart has runtime acceptance on EKS 1.36.4 with Valkey 8.
Complete login, node drain, database promotion and ingress acceptance before
claiming end-to-end shared-service availability.
