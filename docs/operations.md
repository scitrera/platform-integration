# State and recovery

The reference profiles use one instance of each stateful service. Kubernetes
operators and retained PVCs do not establish high availability. Keep image
manifests, chart values and private configuration together with each backup.
A Helm rollback does not reverse schema migrations.

## State owners

| Owner | Compose state | Kubernetes state | Recovery unit |
| --- | --- | --- | --- |
| Auth-go | mt-data PostgreSQL volume | shared-auth-db CNPG cluster | Database plus signing/OAuth/operator secrets |
| SparkRoute | sparkroute-postgres | shared-gateway-db CNPG cluster | Database, caller/provider policy and credential files |
| Aether lite | aether-alpha / aether-beta | tenant Aether PVC | Entire stopped state directory plus tenant CA, token HMAC and service credentials |
| Sessions | sessions Valkey volume | shared-sessions-state PVC | Stopped data plus matching auth configuration |
| MemoryLayer | ml-postgres-* and memorylayer-data-* | tenant ML CNPG cluster and data PVC | Database, derived blob files, embedding/model configuration |
| Data Connectors | dc-postgres-* | tenant DC CNPG cluster | Database and matching tenant storage registration |
| Storage gateway/edge | storage-postgres | storage-db CNPG cluster | Metadata, capability signing keys and S3 credentials |
| Object store | objects MinIO volume | operator-provided S3; fixture objects PVC | All referenced object versions plus metadata |
| Sandbox provider | .local/sandbox-state | provider state and allocated workspace PVCs | Workspace files and provider configuration; release active allocations first |

Database-only backups cannot recover files whose objects or signing keys are
missing. This profile does not claim restoration across database major versions,
schema downgrades or incompatible component releases.

## Offline Compose backup

Release each allocated sandbox using the tenant's public lifecycle SDK, checking
its expected owner. For an alpha allocation:

    python3 scripts/compose.py run --rm --no-deps catalog-alpha python /integration/sandbox_release.py --sandbox SANDBOX_ID --owner user:alice@example.test

Then stop this project and create a new private backup directory:

    python3 scripts/dev.py stop
    python3 scripts/cold_backup.py backup --directory /private/backups/platform-first
    python3 scripts/cold_backup.py verify --directory /private/backups/platform-first

The backup command refuses running project containers, unreleased dynamic
allocations and volumes without the exact Compose project label. It archives
every configured named volume and the allowlisted private configuration,
including tenant credentials and sandbox workspace files. It records checksums
and exact image IDs. Archives contain secrets and remain private.

This is a cold backup: application and database processes stay stopped for the
entire archive. It has no incremental, point-in-time or live snapshot guarantee.
Backup verification checks bytes against the manifest; the browser recovery
scenario establishes application usability after a restore.

## Restore into a separate Compose project

Use a fresh copy of the same integration revision with an empty .local directory
and the recorded images already available to Docker. Run:

    python3 scripts/cold_restore.py --directory /private/backups/platform-first --project platform-integration-restored
    python3 scripts/dev.py up --fixtures

The fixture flag applies only to a fixture backup. For an external installation,
retain its provider configuration and omit that flag.

The restorer verifies every archive before creating destination volumes, refuses
existing target project resources, preserves numeric file owners and rewrites
only the new project name and daemon-visible sandbox state path. The source
volumes remain intact. It retains the original ports and callback URLs, so keep
the original project stopped while running the restored project. The current
restore profile refuses archive symlinks, devices and special files; external
PostgreSQL tablespaces and custom linked workspace layouts need a separately
reviewed procedure.

Record and verify representative state using the browser suite:

    PERSISTENCE_RECORD=/private/persistence.json npx playwright test --grep 'authenticated upload'
    PERSISTENCE_CHECK=/private/persistence.json npx playwright test --grep 'previously recorded'

The second command verifies the original chat, original file bytes, completed
MemoryLayer document and VFS-to-Python checksum after recovery. Set
PLATFORM_ORIGIN to the selected public origin.

## Kubernetes retention

The charts retain CNPG Cluster resources and application PVCs using
helm.sh/resource-policy: keep. Secrets belong to the operator and are referenced
by name. Ordinary install/upgrade contains no tenant teardown operation.
Uninstall removes workload resources while leaving these state owners.

Release allocated sandbox pods through the tenant lifecycle API before uninstall.
Do not delete the tenant namespace to retain data: namespace deletion would
remove retained namespaced resources too. A retained CNPG Cluster still runs its
database and consumes storage until the operator explicitly retires it.

Kubernetes data restoration and retained-data reinstall passed the local drills;
see verification.md for their limits and the untested restored-state promotion. Do not substitute a Compose
volume archive for a production CNPG/S3 backup.

## Kubernetes cold application backup and verified restore

The reference procedure pauses application writers while CNPG remains running
for native PostgreSQL logical dumps. Release allocations first, record each
Deployment replica count, then scale those explicitly selected Deployments to
zero. Wait for their pods to disappear and suspend any CronJobs. Do not scale
CNPG database instances to zero.

    python3 scripts/kube_backup.py backup --directory /private/kubernetes-backup --kubeconfig /private/kubeconfig --context install-target --namespace platform-shared --namespace platform-storage --namespace tenant-example --helper-image REGISTRY/BACKEND:VERSION@sha256:DIGEST
    python3 scripts/kube_backup.py verify --directory /private/kubernetes-backup

The command refuses active application/Job/allocation pods, running Deployments
or StatefulSets, and unsuspended CronJobs. It dumps each declared CNPG application
database, archives all non-CNPG PVCs read-only, and saves private namespace
configuration including Secrets. No database/password values are printed.
External S3 requires a coordinated object-store backup while writers stay paused;
the fixture's MinIO PVC was included in the tested backup.

State-copy pods mount only the selected PVC, have no API token, and are deleted
after copying. They need root and narrowly scoped file-read/ownership capabilities
to preserve mixed numeric owners and modes. This operator maintenance exception
is separate from ordinary runtime and sandbox service accounts.

The backup is a same-version cold reference procedure, with full table scans for
database row comparisons. It is not a live or incremental backup, WAL archive,
PITR implementation, large-database performance claim, or CSI snapshot workflow.
For larger installations, select and test an operator-managed backup system
covering the same databases, blobs, metadata, workspace volumes and credentials.

Restore into unused databases and new PVCs using the original compatible CNPG
clusters/owners and recorded image versions:

    python3 scripts/kube_restore.py --directory /private/kubernetes-backup --receipt-directory /private/restore-receipt --kubeconfig /private/kubeconfig --context install-target --suffix recovered

The restorer checks every archive before writes, refuses existing destination
databases/PVCs and empty-destination violations, rejects archive links/traversal,
and preserves numeric ownership and modes. PostgreSQL restore uses one transaction.
It compares database row inventories and every restored file's bytes/ownership;
the receipt stays private. Original databases and PVCs are preserved.

The tested drill restored seven databases and ten PVCs, including both tenant
workspaces and the fixture object store. All row and file comparisons passed.
The original services were then resumed with their recorded replica counts.
This establishes independent data restoration; it does not claim an automated
traffic cutover or cloud disaster-recovery exercise.

To promote restored state, keep writers stopped and select matching database URLs
in the operator-owned Secrets. existingClaims maps restored Aether, MemoryLayer,
provider or session PVCs to their chart workloads. For dynamically allocated
workspace claims, restore/rebind the original claim names before releasing the
provider from maintenance. Select the restored object-store data and matching
storage metadata/signing credentials as one recovery unit. Apply the reviewed
values, resume workloads, and run the saved-file/browser recovery scenario.
A future Helm upgrade must retain these chosen recovery references.


### Consolidated Kubernetes databases

The quiesced Kubernetes backup helper inventories all non-template databases on
each selected CNPG server, including databases added after initialization. It also
saves a private role snapshot. Verification streams row hashes to bound memory.
For dedicated/tainted nodes, pass the same namespace placement JSON to
`kube_backup.py` and `kube_restore.py` using `--placement`. Restore drills require
the original owner roles and deliberately do not replay role/password changes
into a running server. See [customer deployment configuration](deployment.md)
for nightly backups, private task-file retention and production limitations.
