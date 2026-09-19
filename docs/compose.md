# Compose installation

The supported development profile runs on Linux with a Docker daemon. Acceptance
used Linux arm64, Docker 29.1.3 and Compose 5.0.1, Python 3.12 and Node 22.
Reserve 24 GiB RAM and 40 GiB free disk for prebuilt artifacts and runtime state;
source builds need additional compiler caches and disk. Resource minima have
not been load tested. The fixture inference server downloads no model weights.

Install Python dependencies from requirements-dev.txt. Source builds additionally
need Git, OpenSSL, Node/npm and Docker. Run all commands at the repository root.

## Select artifacts

scripts/build.py consumes explicit component paths supplied in a private JSON
object. Keys include platform-backend, platform-frontend, auth-go, aether,
memorylayer-enterprise and memorylayer-storage. Paths can be anywhere the
operator can read; this repository does not assume sibling checkouts.

Check out each component's exact `revision` in `versions.yaml` and use a clean
working tree. The compatibility manifest now selects committed source in the
owning repositories; no patch application is required. See
[upstream ownership](upstream.md) for review and source availability status.

The builder snapshots tracked files and records the content hash and local image
ID. Its hash must match the selected source set before relying on that selection.
Local development with uncommitted changes remains available through explicit
`--allow-dirty` and `--include-untracked` options. Supply an already built image
with `--use-image KEY=TAG` to record its ID without claiming source provenance.

Auth uses the published **auth-go v0.1.3** image, pinned by its multi-architecture
registry digest in `versions.yaml`. Select that release without a local auth build:

```sh
auth_image_ref="$(python3 -c 'import yaml; print(yaml.safe_load(open("versions.yaml"))["images"]["AUTH_IMAGE"]["tag"])')"
docker pull "$auth_image_ref"
python3 scripts/build.py --use-image "AUTH_IMAGE=$auth_image_ref"
```

This records the selected image in the installation manifest. Existing
installations retain their image selection until this command is run; changing
`versions.yaml` alone does not restart services or replace `.local/images.json`.
The release index includes Linux amd64 and arm64. See the
[auth release verification](verification.md#auth-go-v013--15-september-2026).

Build `AETHER_IMAGE` before `SIDECAR_IMAGE` and `CODE_BASE_IMAGE` before
`CODE_IMAGE`. One checkout per component now supplies all its images. The sidecar
uses the same Aether revision as the tenant gateway; dependency image IDs are
recorded explicitly in `.local/images.json`. Web packaging includes its matching
source archive.

Build the gateway from the AGPL `scitrera/platform-sparkroute` checkout:

```sh
python3 scripts/build_sparkroute.py --source /path/to/platform-sparkroute
```

The distribution vendors its pinned public dependencies and builds without a
sibling checkout or private source input. The builder checks source/dependency
integrity and records the distribution revision, content hash, upstream pin and
image ID. Alternatively, record a compatible platform-sparkroute image as
`SPARKROUTE_IMAGE`. The local candidate is not yet a published registry artifact.
The native catalog contracts are described in [model-catalog.md](model-catalog.md).

## Configure and start

Generate new development material and explicitly select local fixtures:

    python3 scripts/configure.py
    python3 scripts/fixtures.py
    python3 scripts/gateway_configure.py --fixtures
    python3 scripts/dev.py up --fixtures

To configure customer tenants instead of the two examples, supply a JSON list on
first configuration:

```json
[{"slug": "customer", "name": "Customer", "email": "alice@example.test", "workspace": "default"}]
```

    python3 scripts/configure.py --tenants /path/to/tenants.json

The definition is recorded in `.local/tenants.json` and reused by subsequent
configuration, authentication enrollment, and gateway setup. Compose services,
networks, storage buckets, certificates, administrator grants, and fixture
services are generated from the maintained example templates. Generated Compose
files remain private in `.local/`; the checked-in examples are unchanged.
Tenant slugs accept lowercase letters, digits, and hyphens, starting with a letter.
The fixture identity provider continues to offer its synthetic Alice/Bob identities;
use a matching email for fixture login.

Use a fresh integration directory to change tenant definitions. Existing tenant
state and credentials are never renamed by configuration. Offline backups include
the recorded definitions, generated Compose files, and each tenant's credentials.
The default acceptance scripts still target the alpha/beta example installation;
customer smoke tests must select their configured tenant.

Image selection must precede dev.py. Its first run invokes auth-go's own
bootstrap CLI for the operator credential file, starts/migrates auth, enrolls
synthetic tenants through the public operator API, and starts the dependency
graph with bounded readiness checks. Failed setup exits nonzero. Repeating the
command preserves existing secrets, memberships and matching model policies.

Open http://127.0.0.1:18080. The signed fixture login selects the synthetic
administrator. Alpha and beta have independent tenant CAs, databases, storage
namespaces, gateway callers and allocation providers. Auth operators use the
separate loopback dashboard at http://127.0.0.1:18082/admin/; the generated
credential stays in .local/operators.json.

A second independent installation can select distinct ports before its first
run:

    python3 scripts/configure.py --project platform-integration-clean --web-port 18084 --admin-port 18086 --fixture-idp-port 18094

Use a separate checkout/directory for that installation. Changing arguments does
not replace an existing .local/compose.env or rotate existing credentials.
Fixture policy refuses conflicting provider settings.

## LAN browser access

For a fresh installation, bind the browser-facing services and set their public
origin together:

    python3 scripts/configure.py --bind-address 0.0.0.0 --public-origin http://dev-host:18080

The fixture identity provider uses the same hostname on the configured fixture
port (18090 by default); override `--fixture-public-origin` when needed. Both web
and fixture ports must be reachable by the browser. OAuth callbacks, post-login
redirects, WebSocket origin checks, and public upload/download URLs use this
origin. The operator dashboard remains bound to loopback. Existing installations
retain their addresses; choose a fresh directory to change their browser origins.

## Verification and daily operation

    python3 scripts/compose.py ps
    python3 scripts/check_catalog_acl.py --profile compose
    npm ci
    npx playwright install chromium
    FIXTURE_IDP_ORIGIN=http://127.0.0.1:18090 AUTH_OPERATOR_ORIGIN=http://127.0.0.1:18082 AUTH_OPERATORS_FILE="$PWD/.local/operators.json" npx playwright test

PLATFORM_ORIGIN selects a different configured web origin; FIXTURE_IDP_ORIGIN selects the disposable issuer used by the identity tests. Raw runtime logs and
test failures can contain synthetic credentials or private request data; keep
.local private and publish only reviewed summaries.

    python3 scripts/dev.py stop
    python3 scripts/dev.py start

Release active sandbox allocations before stop or down; the wrapper refuses to
leave them running after their tenant services disappear. These commands retain
volumes and configuration. down removes Compose containers
and networks while retaining named volumes; dynamic provider allocations have
their own lifecycle and must be released through scripts/sandbox_release.py with
the expected owner before complete project cleanup. Do not use global Docker
prune commands. Cold backup/restore and retained-data cleanup have been exercised; consult
operations.md and verification.md for their limits.

Only allocation providers receive the Docker socket. That grants host-level
Docker authority and is suitable for this trusted development installation.
Production Kubernetes uses namespace-scoped service accounts instead.

## External configuration

External Google/Entra and real model-provider acceptance remains pending by
operator choice. Keep OAuth client material in .local/oauth.env and provider
credentials in private credential files. Use auth-go's supported provider
configuration and operator API; do not write its tables directly. Supply explicit
model provider host allowlists and embedding endpoints. The local fixture overlay
must be disabled for an external-provider run. Certificate generation is for
disposable development only and has a 30-day lifetime.

Customer overlays can set `TENANT_SETUP_MINIMAL=true` on their `catalog-<tenant>` job to provision reserved workspaces, administrative infrastructure and agents without registering or globally installing the standard application catalog. The default remains the full catalog. Customer provisioning can then register its own applications. Existing app assignments are not removed by this bootstrap setting; reconcile them through the application APIs when migrating an existing tenant.


### Workspace landing preferences

Tenant definitions accept optional `default_workspace`: a workspace ID, or `null`
for no configured landing workspace. If omitted, new tenants use `workspace` as
before. The `workspace` field remains bootstrap configuration; changing the
landing preference does not delete workspace data. `select_tenants` permits
updating only this preference in an existing installation while retaining its
identity guard for other fields. `scripts/auth_setup.py` applies explicit
preferences to existing tenants through the operator API, preserving their name,
enabled state and other editable metadata.

To show a picker instead of selecting the first accessible workspace, also set
tenant `uiConfig.autoSelectWorkspace=false` using platform provisioning. The
frontend defaults this flag to true for existing deployments. Direct workspace
URLs remain explicit selections.

## AetherLite memory budget

The pinned Aether revision includes on-demand Badger replay/KV reads and a
default 1 GiB Go runtime memory budget. This is a soft garbage-collector budget,
not an RSS or container limit. Transient live Go allocations can exceed the
budget, and mapped database files, native allocations and other non-Go memory
need additional headroom. Measure representative replay
and ingestion before choosing a VM or imposing a hard container limit.

To override the runtime budget, set `GOMEMLIMIT` on the tenant's Aether service
in a Compose overlay (for example `768MiB` or `2GiB`). In Helm, add it to the
Aether environment Secret. An explicit value is preserved by AetherLite;
`off` disables the budget. The effective value is logged at startup. The
standalone gateway is unaffected. Image/source pins are in `versions.yaml`;
local image IDs are engine-specific and do not establish registry publication.

The private Aether administration listener uses HTTPS with the same tenant
server certificate as its gateway. Existing generated `.local/<tenant>/aether.yaml`
files are preserved by configuration: when upgrading, add `admin.tls_cert_file`
(`/etc/aether/tls/tls.crt`) and `admin.tls_key_file`
(`/etc/aether/tls/tls.key`) and recreate the Aether service. Without TLS the new
server refuses to start an administration listener that has an API key. The
gRPC gateway and operations health/metrics listener are independent. Keep
the administration listener private and validate its tenant CA when connecting.

The tenant Helm chart gives Aether a separate `roleResources.aether` default
(512 MiB request, 8 GiB container limit) to leave headroom above its 1 GiB Go
budget. This is a starting configuration, not a qualified maximum for every
data/replay workload. Include startup/reconnection and kernel lifetime peaks,
not just steady-state samples, when qualifying a lower limit. Also exercise
repeated task creation/recovery under the intended CPU limit: brief allocation
bursts can exceed both the steady footprint and the soft Go budget. Override it using
measured total container demand; lowering
`GOMEMLIMIT` does not bound mapped files. Compose leaves the hard limit to the
operator's overlay.
