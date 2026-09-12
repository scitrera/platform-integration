# Tenant model catalog

Sahara aliases are durable records in each tenant's Aether global KV. SparkRoute's
native Aether resolver reads those records with a separate read-only service
identity. Central SparkRoute configuration retains shared policies and the
MemoryLayer fixture alias. Central exact model names take precedence, so do not
duplicate a tenant alias in the central routing document.

The selected SparkRoute composition owns schema validation, exact model-key
derivation, caching, provider policy checks and publication. Installation jobs
invoke its catalog publish command with the tenant management credential.
Publication creates absent records, accepts an identical retry, and refuses to
overwrite differing records. An intentional update requires the exact previous
record file through the owner's --expected compare-and-set option. Keep
operator-owned records under configuration control; never put provider keys in
the catalog records. Use allowed credential references instead.

Compose reads one atomic JSON credential bundle per tenant from its private
gateway directory. The Kubernetes gateway reads the three mTLS fields from one
Secret API response, tracks its resourceVersion, and has GET permission only for
the explicitly listed reflected Secrets. Its replica identity is the Pod UID.
Compose uses a single named gateway replica. The tested profile has one gateway
replica; multiple-replica operation is not established by these tests.

The per-tenant bootstrap explicitly names the services allowed to inherit the
platform's historical service permissions. It also configures a read-only KV
service rule for sparkroute-modelcatalog, limited to
sparkroute/modelcatalog/v1/models/*. The component verifies replacement rules
before retiring bootstrap-owned _any_service grants. An unrelated operator
wildcard grant stops reconciliation for review instead of silently removing it.
Changing the replica specifier does not remove the reader restrictions.

For Kubernetes, set modelCatalog.allowedProviderHosts,
modelCatalog.credentialSecretNames, and any required modelCatalog.apiEgress
CIDR/port pairs. The API endpoint's destination port can differ from the
Kubernetes Service port after address translation. Allow only the actual cluster
API endpoints. Tenant namespaces use a stable aether-gateway Service; the
reference certificate server-name template assumes tenant releases use the
tenant slug. Set the template to match the operator's issued certificates.

Local fixtures permit HTTP inference and create records without real provider
credentials. Production should retain HTTPS provider policy and supply scoped
credentials through the selected SparkRoute credential source. External provider
acceptance remains pending.

Run python3 scripts/check_catalog_acl.py --profile compose or --profile kind
to check real reader access. These fixture checks seed a synthetic unrelated key,
verify catalog reads, and reject writes, unrelated KV reads and ACL administration
with both a stable and a changed replica ID.

## Credential rotation

Keep Compose provider credentials under the configured private file roots, with
mode 0600. Mount their containing directory and replace the whole file atomically;
binding only the old file inode will not expose replacement content. Model records
refer to these files without containing the key.

In Kubernetes use k8s://NAMESPACE/SECRET#KEY and include that exact Secret name in
modelCatalog.credentialSecretNames. Update the Secret as one operation. The native
credential source reads its resourceVersion; the installed test observed the new
key without restarting the gateway. Reflected catalog mTLS bundles also read all
three fields from one Secret response.

gatewayFiles is copied into private memory storage by an init container. Changes
to those file inputs require a gateway rollout. Do not infer hot reload of copied
files from the native Secret credential test. The one-replica reference gateway
has no availability guarantee during a rollout.
