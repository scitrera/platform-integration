# Helm installation

The reference topology uses three independent chart types: platform-shared,
platform-storage, and one platform-tenant release per tenant. It requires
Kubernetes 1.34–1.36, an enforcing CNI, a working ReadWriteOnce storage class,
CloudNativePG, and a Gateway API controller. Charts have no bundled chart
dependencies; operator and CRD versions are separate installation inputs.
No ArgoCD or private administration package is required.

This is a single-instance application baseline. Aether lite, Valkey and several
application services are single points of failure. Increasing databaseInstances
does not make the application highly available. CSI snapshots, managed cloud
services, gVisor, multi-node failover and external identity/provider acceptance
need their own environment-specific verification.

## Prerequisite ownership

The cluster operator installs/upgrades CNI, CSI, CNPG and Gateway API/controller
once. Application operators install namespaced charts, quotas and provider RBAC.
The provider receives a Role limited to its tenant namespace; allocated pods use
a separate service account with no mounted API token. Only the shared gateway's
credential reflector can read the explicitly named tenant catalog Secrets.

The tested disposable versions and downloaded artifact checksums are in
tests/fixtures/prerequisites.lock.json. They record the tested compatibility set,
not an automatic operator update policy. The fixture uses Calico enforcement,
local-path storage, CNPG and Envoy Gateway. Production must supply durable storage,
public DNS/TLS and a certificate renewal process. TLS Secrets are existing inputs;
certificate management is not installed by these charts.

Reserve capacity for databases, applications, migration Jobs and execution pods.
Set storage.className/size, databaseResources, resources, roleResources and tenant
quota.hard for the selected cluster. Database instances are limited to 1–3 by the
schema; only one was tested. The Kind profile used two Linux arm64 nodes.

## Images and configuration

Copy the three examples/helm values files into private operator configuration.
Replace every REPLACE value. Production sets development: false and requires a
registry manifest digest for every image. Local Docker image IDs are not registry
manifest digests. CNPG image references also retain the actual PostgreSQL version
in their tag; MemoryLayer requires its own vector/AGE/pg_textsearch image.

Configure release and namespace references together. Tenant sharedNamespace and
storageNamespace select shared dependencies; shared.tenants maps each tenant ID,
namespace and release. shared.modelCatalog.addressTemplate and serverNameTemplate
must match the Aether service aliases and certificate names. The charts deliberately
provide aether-gateway as the tenant-local alias. Override blobFetchBaseURL,
storageEdgeEndpoint, clusterDomain and dnsResolver when the topology differs.
The storage download endpoint accepts authenticated capability GET/HEAD requests;
the storage control API is not exposed to sandbox pods.

Keep provider credentials in Kubernetes Secrets referenced by the selected
SparkRoute distribution (k8s://NAMESPACE/NAME#KEY). Include each exact Secret
name in modelCatalog.credentialSecretNames; no list or wildcard read is needed. Set allowedProviderHosts and its network destinations explicitly;
the production model policy defaults to HTTPS. Populate all three Sahara aliases
through modelCatalog.records using the native publisher contract in model-catalog.md.
The operator-supplied SparkRoute composition must include that contract; a local
private image is not evidence of public distribution availability.

## Existing Secrets

Create the namespaces and operator-owned Secrets before installing. Never put
real values in these examples or commit generated .local files. Secret names are
configurable in each chart's secrets map. Required contents are:

| Release | Secret input | Required keys / contract |
| --- | --- | --- |
| Shared | authDatabase, gatewayDatabase | kubernetes.io/basic-auth; username and password match each CNPG database owner |
| Shared | auth | SCITRERA_MT_DB_URL, AUTH_PROXY_TOKEN_HMAC_KEY |
| Shared | oauth | Auth-go's supported Google/Entra provider and session configuration |
| Shared | operators | operators.json created by auth-go bootstrap CLI |
| Shared | gateway | SPARKROUTE_POSTGRES_URL, SPARKROUTE_RUNTIME_POSTGRES_URL |
| Shared | gatewayFiles | config.json, callers.json and referenced credential files; see gateway configuration contract |
| Shared | tlsSecret | kubernetes.io/tls certificate/key for hostname |
| Shared | modelCatalog.credentialSecretNames | Reader Secrets: tls.crt, tls.key, ca.crt; provider Secrets: the key named by their credential reference |
| Storage | database | basic-auth username=storage and password |
| Storage | storage | BLOBGW_DATABASE_URL, BLOBGW_EDGE_DATABASE_URL, EDGE_SIGNING_SEED, S3_ENDPOINT, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION |
| Storage | files | tenants.json and credentials.json storage registration |
| Tenant | mlDatabase, dcDatabase | basic-auth username=memorylayer / dataconnectors and distinct passwords |
| Tenant | aether | AETHER_TOKEN_HMAC_KEY, AETHER_ADMIN_API_KEY, TOOL_CATALOG_CURSOR_KEY |
| Tenant | memorylayer | MEMORYLAYER_POSTGRESQL_URL and explicit embedding/model configuration |
| Tenant | connectors | DC_POSTGRESQL_URL, DC_BLOBGW_UPLOAD_PUBLIC_URL, DC_BLOBGW_EDGE_PUBLIC_URL |
| Tenant | gatewayToken | token bound to this tenant's gateway principal |
| Tenant | platform, provider | explicit environment overrides; empty Opaque inputs are allowed |
| Tenant | secrets.tls.* | tls.crt, tls.key, ca.crt with the role-specific Aether identity |

CA/service identity issuance follows Aether's component contract. Management,
orchestration, catalog readers and anonymous user-verification identities are
different credentials. Do not reuse an administrator key for an application.
serviceGrants defaults to empty; add only explicitly required service operations.
toolsWorkspace is one concrete workspace. The fixture grants in helm_local.py
are synthetic user-specific setup, not production grants.

Auth-go operator enrollment uses its supported dashboard/API. The public web
tenant Admin API is separate. Bootstrap does not enroll arbitrary new users or
turn them into tenant administrators in production (seedDevelopmentAdmin: false).

## Phase graph and ordinary Helm

Always select kubeconfig and context explicitly. Set private values paths and
existing namespaces. For each release, plan first:

    python3 scripts/helm_apply.py plan --chart shared --release shared --namespace platform-shared --values /private/shared.yaml --kubeconfig /private/kubeconfig --context install-target

The helper validates chart schemas, prerequisite references and existing Secret
names without displaying values. Apply shared, storage, then each tenant:

    python3 scripts/helm_apply.py apply --chart shared --release shared --namespace platform-shared --values /private/shared.yaml --kubeconfig /private/kubeconfig --context install-target
    python3 scripts/helm_apply.py apply --chart storage --release storage --namespace platform-storage --values /private/storage.yaml --kubeconfig /private/kubeconfig --context install-target
    python3 scripts/helm_apply.py apply --chart tenant --release example --namespace tenant-example --values /private/example.yaml --kubeconfig /private/kubeconfig --context install-target

| Chart | Ordered phases |
| --- | --- |
| Shared | 0: databases/sessions; 1: auth/gateway migrations; 2: auth/gateway/web/ingress |
| Storage | 0: database; 1: blob gateway/edge/restricted download |
| Tenant | 0: Aether/databases; 1: ACL and migrations; 2: MemoryLayer/connectors; 3: workspaces/catalog/models; 4: platform/bridge/provider/tools/skills |

The helper invokes ordinary helm upgrade --install with --wait --wait-for-jobs,
then explicitly waits for CNPG Ready conditions, which Helm itself does not wait
for. A fresh install visits phases in order. A resumed install starts at its live
phase; it never lowers a phase and removes established workloads.

Equivalent manual commands use helm upgrade --install RELEASE charts/CHART
with explicit kubeconfig/context, namespace, values and --set phase=N. After each
command, wait for the named CNPG clusters with kubectl wait --for=condition=Ready.
Advance only after required Jobs complete. Installing a fresh tenant straight at
phase 4 can race migrations against APIs and is not the documented installation.

## Upgrade and failed setup

Back up the complete recovery unit first; see operations.md. Select compatible
component versions and a new bootstrapRevision when migration/catalog inputs
change. Job names include that revision because Job templates are immutable.
Changing images without changing the revision can fail an immutable Job update.

Rerun the same apply command. Existing Secrets are not chart-generated or rotated.
Model publishing refuses conflicting existing policy. MemoryLayer migrations
hold a PostgreSQL advisory lock across the component initializer.

A database image update can temporarily refuse migration connections. This was
observed during the disposable upgrade; Helm failed clearly instead of declaring
success. Wait for the CNPG cluster to be Ready, inspect the specific failed Job
privately, and correct the cause. For an unchanged retry, delete only that verified
failed, release-owned Job and rerun the same Helm command. Completed Jobs and
persistent resources stay intact. Never delete all Jobs or lower the phase as a
retry shortcut. For schema changes incompatible with running clients, schedule
maintenance and quiesce those clients before applying the upgrade.

Charts contain no teardown Jobs. Uninstall retains CNPG Cluster resources and
application PVCs; operator Secrets remain. Release allocations first, retain the
namespace, and reinstall the same release name with the original values and
Secrets to recover retained state. Namespace deletion defeats retention.
A Helm rollback cannot undo schema migrations.

## Disposable acceptance cluster

After building/selecting the images in compose.md and generating fixture input:

    python3 scripts/kind_setup.py download
    python3 scripts/kind_setup.py create
    python3 scripts/kind_setup.py prerequisites
    python3 scripts/kind_setup.py load-images
    python3 scripts/helm_local.py --kubeconfig .local/cluster/kubeconfig --context kind-platform-integration

The creation command refuses an existing named cluster. Prerequisite application
is restricted to the expected two-node disposable cluster. It installs pinned
Calico, CNPG and Envoy artifacts and a GatewayClass. helm_local.py creates
synthetic credentials and values, preserves existing Secret bytes and release
phases, and refuses conflicting inputs. --check applies no cluster resources.

Apply shared/storage/alpha/beta with helm_apply.py and the corresponding
.local/helm values paths, using the explicit Kind kubeconfig/context. Before tenant
installation, port-forward shared-auth:8082 to loopback18083 and enroll users:

    python3 scripts/auth_setup.py --origin http://127.0.0.1:18083

For browser tests, port-forward the Envoy HTTPS service to loopback18443 and idp:8080
to loopback18091, then run:

    PLATFORM_PROFILE=kind PLATFORM_ORIGIN=https://platform.example.test:18443 FIXTURE_IDP_ORIGIN=http://127.0.0.1:18091 AUTH_OPERATOR_ORIGIN=http://127.0.0.1:18083 AUTH_OPERATORS_FILE="$PWD/.local/operators.json" npm run test:browser

The fixture browser maps platform.example.test to loopback. Real production TLS
validation is not replaced by that browser fixture setting. The tool-host test
uses the generated CA explicitly. To test beta, add PLATFORM_TENANT=beta and
FIXTURE_IDP_ORIGIN=http://127.0.0.1:18091. Port-forwards must be restarted if their
selected pod is replaced.

## Health and troubleshooting

Use helm status, kubectl get jobs/pods/clusters, pod events and rollout status
before inspecting private logs. Job active deadlines and installer waits bound
startup failures. Check missing Secrets, rejected catalog policy, CNPG extensions,
tenant CA identity and allocated pod events when readiness fails. Failed dynamic
allocations must be released through the public SDK with their expected owner.

Auth checkz, native CONNECTION_READY and a scoped RPC jointly verify browser
readiness; HTTP101 alone does not. Gateway health/list-model checks do not prove
provider inference. Keep logs local: identity projections, signed storage URLs
and delegated credentials can appear in application errors. Configure your own
log/metric collection; no hosted telemetry destination is enabled by default.
