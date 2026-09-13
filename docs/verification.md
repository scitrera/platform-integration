# Verification status

The reference profiles below passed installed acceptance on Linux arm64.
The current source selection uses component commits; see [upstream ownership](upstream.md).
Its Compose acceptance and build status are recorded in versions.yaml. Kubernetes
installation results below apply to the earlier image baseline; this source update
reruns Helm rendering and configuration checks only. External OAuth, real
model-provider calls, backend/frontend source publication and image registry
availability remain pending.

| Capability | Docker Compose | Helm on disposable Kind |
| --- | --- | --- |
| Built web, signed auth-go login, deep links and matching source archive | Passed | Passed through Envoy TLS |
| Native WebSocket readiness/profile and Socket.IO compatibility | Passed | Passed |
| Forged tenant headers, missing membership and unauthorized embedded Admin | Refused | Refused |
| POST logout, operator revocation and real three-second session expiry | Passed | Passed |
| Authenticated tool host, delegated Admin and wrong-token/workspace refusal | Passed | Passed with explicit CA verification |
| Actual Sahara, Go sidecar and Python code execution | Docker allocation and SDK release passed | Kubernetes allocation and SDK release passed |
| Streamed chat, cancellation, provider failure, next turn and history reload | Passed | Passed |
| Tool approval grant/deny and exact initiating browser window | Passed | Passed |
| Real upload, ingestion, memory, VFS and Python byte checksum | Passed | Passed |
| Generated artifact bytes/MIME, workspace denial and reload | Passed | Passed |
| Paused worker, browser disconnect and resumed queued turn | Passed | Passed |
| Tenant catalog reader ACL, including changed replica identity | Passed | Passed |
| Concurrent publisher create, one CAS winner, stale refusal and retry | Passed | Passed |
| Provider credential rotation and refusal of the previous value | Atomic file replacement passed | Secret resourceVersion update passed |
| Actual Sahara stream ledger: tenant/user/workspace/thread/task and 10/4/14 fixture tokens | Passed | Passed |
| Sandbox network isolation | 15 TCP and two HTTP checks passed | Nine connection checks and download-method restrictions passed under Calico |
| Bootstrap failure, retry, repeat setup and upgrades | Passed | Passed for both tenants |
| Cold backup and independent restore | 13 volumes plus configuration restored; saved browser state passed | Seven databases and ten PVCs restored into unused destinations; all inventories matched |
| Retained-data uninstall/reinstall | Down retained original volumes | Beta Cluster/PVC/Secret identities retained; reinstall and nine browser scenarios passed |
| External Google/Entra OAuth and a real model provider | Pending by operator choice | Pending by operator choice |
| Anonymous source/image availability | Pending | Pending |

On 2026-09-12, all 17 enabled browser scenarios passed together on each
profile after the final Sahara accounting fix (24.7 seconds on Compose,
34.5 seconds on Kind). The three optional scenarios cover saved-state recovery,
controlled session expiry and controlled worker interruption; each passed in its
separate drill. Earlier unsuccessful runs remain private evidence; the final
results above do not count those runs as passes.

The streaming test uses the real browser, tenant services, allocated Sahara,
sidecar, SparkRoute and its PostgreSQL ledger. Only the upstream inference is
synthetic. Its token counts are deliberately fixed, not an estimate of real
model cost. Sahara now drains the trailing usage event after finish_reason;
the owning adapter's delayed-event regression test passed. Cancellation and
provider-failure recovery also passed with that change.

Credential rotation uses new random fixture values. Compose replaces a private
credential file atomically; Kubernetes updates one explicitly named Secret.
Successful calls before and after rotation record usage, and the fixture returns
401 when asked to validate the previous value. The provider key never enters
the browser or sandbox. The Kubernetes operator probe uses a temporary policy
limited to its run's pods and the gateway port; it restores the Secret GET
allowlist and deletes its test records, keys, Jobs and policy afterward.
No multi-replica gateway or zero-downtime failover claim is made.

Real auth-go verifies the fixture issuer's signed exchange. Expiry changes the
fixture workload's server-side lifetime and restores it afterward; it does not
edit a cookie to simulate expiration. Revocation refuses new authentication and
new sockets. Already accepted sockets retain the behavior specified by the
current auth/protocol contract. Ordinary tenant membership does not authorize
the embedded Admin API. Dedicated private superadmin is absent.

The unavailable-worker drill pauses only the selected Sahara process, submits
a turn, disconnects the browser, resumes the worker, and observes one completed
response after reconnect. It does not establish crashed-worker replacement,
durable queue failover or high availability.

The cluster used Kubernetes 1.34.0, Kind 0.30.0, Calico 3.32.2, CNPG 1.29.1,
Envoy Gateway 1.9.1 and local-path storage on two arm64 nodes. Allocated pods have
no mounted API token. Their enforced policy denies the other tenant, direct
MemoryLayer, the Kubernetes API and metadata endpoints. Single-instance
databases and local volumes do not establish cloud durability or HA.
CSI, gVisor and managed-cloud paths are outside this verified profile.

Compose restoration recovered application-visible saved chat and file bytes.
The Kubernetes drill verifies independent restored database/file contents and
resumes the original services; restored-state traffic promotion remains untested.
Database rollback, cross-version restoration, live backup and PITR are not claimed.

The current compatibility manifest records 19 local runtime/build images from
six clean component source revisions, with dependency image IDs and the matching
web source archive. Component changes are maintained upstream; the installation
does not apply patch snapshots. Local Docker image IDs are not registry manifest
digests. The permitted SparkRoute enterprise build excludes its source from this
repository and does not imply permission to redistribute its image.

A fresh directory populated only from the reviewed 147-file export passed all
eight smoke steps: independent configuration, complete installation, browser
dependency setup, 17 browser scenarios (32.7 seconds), actual streamed-task
ledger verification, repeated bootstrap and saved-file/chat recovery (1.9 seconds).
The smoke consumed only the exported installation and explicitly supplied local
image records. No private monorepo or incidental runtime configuration was copied.

See acceptance.md for reproducible fixture commands and operations.md for the
tested recovery procedures. Upstream code review status is recorded in upstream.md. Registry publication and
production deployment were not performed.
