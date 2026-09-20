# Customer deployment configuration

`scripts/deployment.py` resolves a customer deployment description and its referenced
component files once, then renders Compose or Helm inputs. Tenant applications,
auth admission, model aliases, document services, embedding dimensions, work
profiles and review settings remain in the customer repository. Operator bindings
supply image digests, namespaces, placement, service addresses, storage and Secret
references. Rendering is offline and does not expand credentials or change services.

```sh
python3 scripts/deployment.py check --config /customer/config/deployment.yaml \
  --profile production --bindings /operator/bindings.yaml
python3 scripts/deployment.py render --config /customer/config/deployment.yaml \
  --profile production --bindings /operator/bindings.yaml \
  --runtime helm --output .local/customer-production
```

Use `--runtime compose` for the same customer policy on Docker Compose. Keep the
LAN profile's existing local auth and storage settings; switching database or
object-storage layouts requires data migration. A rendered file is not an installer
or evidence that an existing stack has been upgraded. Compose still consumes the
base installer's private credentials, certificates and volume identities.

Production rendering requires registry manifest digests, HTTPS, host-wide auth,
and an external object store. Kubernetes additionally requires the actual cluster
DNS IP and explicit API endpoint CIDR/port rules in `bindings.kubernetes`. Refresh
those rules when the control plane endpoints change. `ingressEnabled: false`
produces a staged configuration without public HTTPRoutes while retaining the
required host-auth policy for subsequent activation.

## State and credentials

`database.mode: consolidated` renders one PostgreSQL instance with independent
application databases and roles. It is an installation choice, not an automatic
merge of existing databases. Shared auth, OpenMeter and usage reporting may use
separate PostgreSQL releases via `charts/platform-postgres`. That chart's physical
backup resources target CNPG 1.30 and Barman Cloud plugin 0.15. The existing
shared/storage/tenant charts retain their Kubernetes 1.34 compatibility gate
until newer runtime acceptance is complete. Offline schema validation against
Kubernetes 1.36 does not replace that acceptance.

`scripts/deployment_credentials.py` creates private Secret manifests for a new
consolidated tenant or imports an existing Compose installation's identities.
Choose `--fresh` only for an empty installation, or `--from-compose` with the
existing installer's state directory. Its `--state` directory retains generated
identities so repeat runs do not rotate them. Generated files have mode 0600.
Model/document keys are read literally from the customer's referenced env files;
values are never interpreted as shell commands. The tool does not apply Secrets
and does not yet provision shared auth or shared metering identities. Do not
replace existing cluster Secret values without checking them against retained
state. CA and leaf certificate expiration/renewal remain operational requirements.

Customer source is distributed independently from base images. Package it with
`scripts/customer_bundle.py`; `scripts/customer_stage.py` verifies its hash and
installs an immutable revision into an owned source PVC using an explicitly
selected cluster context. It refuses an unowned existing claim and mismatched
revision content. Runtime source mounts are read-only; task state uses a separate
volume. Models, skills and review settings participate in the configuration digest.

## Authenticated object access

The serving chart protects the entire customer host through Envoy's external auth
policy, including browser storage, uploads and source-page routes. The auth gate
uses a fixed tenant/workspace, returns a login redirect for HTML navigation and
401 for unauthenticated API calls, and fails closed on auth-service errors. This
host gate complements document/workspace ACLs; it does not replace them.

For AWS storage use `objectStorage.credentialMode: aws-sts` with a narrowly scoped
role ARN and service-account annotations. Static credentials remain an alternative
for other S3 installations. The operator must prevent public presigned-S3 bypass,
for example through a tenant bucket policy requiring the reviewed VPC endpoint.
The storage gateway alone uses its private S3 upstream; browser traffic uses the
customer's authenticated origin. The separate backup bucket must permit the
reviewed off-node recovery identity.

## Backups and retention

Compose can run the bounded `images/postgres-backup` helper on a nightly schedule.
It saves role definitions and custom dumps of every configured database, validates
the archives, uploads an encrypted Restic snapshot and verifies its manifest before
recording success or applying retention. Configure the ignored backup env file,
keep the encryption password separately and test recovery from another machine.
Repository pruning is an explicit maintenance operation. A local filesystem
restore test does not qualify S3 or off-node recovery.

For Kubernetes, nightly CNPG physical backups and WAL archiving cover the whole
PostgreSQL cluster. Full application recovery also needs matching credentials,
Aether/application volumes, customer source revision and object-store state.
`kube_backup.py` is an explicit quiesced maintenance backup: it inventories **all
non-template databases**, including databases created after initdb, saves a private
role snapshot and verifies each custom archive. Unsupported/disconnected databases
fail the operation rather than being silently omitted. Table verification streams
sorted row hashes instead of building a table-sized PostgreSQL string.

Both Kubernetes backup and restore accept `--placement /operator/placement.json`,
a mapping of namespace to `nodeSelector` and `tolerations`, so helper Pods can mount
volumes on a dedicated tainted node. Restore drills create new databases/PVCs and
refuse existing destinations. Original owner roles must already exist; the drill
never replays role/password snapshots into a live server. Use a reviewed role
restoration procedure when recovering a lost cluster. Preserve the backup's
private manifest and matching Secrets.

Sahara's private source cache, scratch and archived context have eight-hour scope
expiry, startup/five-minute cleanup and a default aggregate 3 GiB budget per
instance. Admission reclaims expired scopes, then refuses writes if unexpired
state fills the budget. It does not evict active notes or extend source authority.
Kubernetes mounts a separate private 4 GiB claim into Sahara only. Stopped harnesses
clean expired contents when restarted. Retired owner claims need explicit operator
cleanup after checking allocations/tasks; published outputs and customer worker
checkpoints follow separate retention rules. A volume capacity limit is not an
archival policy.

## Usage and draft pricing

`scripts/metering.py` renders the same bounded reporting services for Compose or
Kubernetes. ClickHouse has a 2 GiB container limit, a 1.25 GiB tracked-memory budget,
256 MiB per-query limit and two query threads. Kafka, Redis, PostgreSQL and the
OpenMeter API/sink have independent limits. Reporting uses its own durable
PostgreSQL journal, authenticated tenant/source-bound producers, stable event IDs
and replayable delivery to OpenMeter.

The tenant collector reads canonical SparkRoute attempts, with an overlapping
replay window and read-only database sessions. Provision a SELECT-only database
role for it in production. Do not enable the legacy LLM metrics bridge alongside
this collector. Current source durability is **unreconciled**: upstream asynchronous
usage persistence can lose events before they reach the attempt database. OCR,
embedding, compute and storage producers also require integration. Reports must
show these coverage gaps; recording events is not proof of complete billing.

The backend report CLI supports versioned provider-cost and customer-price cards,
exact decimal rates, reproducible draft IDs and explicit unknown/missing coverage.
An incomplete draft has no final total. It does not issue invoices or take payments.
See the backend's `docs/usage-reporting.md` for its event and rate-card contracts.
