# Platform SparkRoute distribution

The gateway, PostgreSQL migration job and native Aether catalog publisher use
`scitrera/platform-sparkroute`, an AGPL-3.0-only Scitrera distribution of upstream
`sparksq/sparkroute`. General PostgreSQL adapters are owned by upstream; tenant
caller policy, Aether credentials/catalog and command composition are owned by
the platform distribution. Integration owns installation and concrete values.

Build from a single explicit checkout using:

```sh
python3 scripts/build_sparkroute.py --source /path/to/platform-sparkroute
```

The distribution pins and vendors the public upstream code and dependency source.
The builder does not combine an internal tree with an OSS sibling. It verifies
release inputs and records the distribution revision/content hash, upstream
revision and local image ID. Neither this command nor tests publish anything.

Compose and Helm retain their existing bearer caller mapping, native Aether
catalog, file/Kubernetes credential policy, `migrate --components ledger,runtime`
and `catalog publish` contracts. Keep the gateway and all its jobs on the same
image. The distribution includes its matching source at `/source.tar.gz` and a
source offer at `/source`; preserve those paths through any external proxy.

File configuration still requires a rollout after replacement. Catalog refresh
and caller/provider credential rotation have independent semantics. The reference
uses a single gateway replica; publishing a new distribution does not establish HA.

## Local candidate verified on 2026-09-13

| Input / artifact | Identity |
| --- | --- |
| Distribution revision | `a3feca9b13abb011924ce86ae6f391a3bfab9a7d` |
| Upstream base revision | `50d267f5f39909bdc6820a0bc908346ada2f7cf4` |
| Upstream Go module version | `v0.0.3-0.20260913184231-50d267f5f399` |
| Local Linux arm64 image | `platform-integration/platform-sparkroute:d6466c1919246869` |
| Local Docker image ID | `sha256:7749b80617623dfb29aca59605abe4cafbaea090cca3dbd1e6a646a85693670b` |
| Matching source archive SHA-256 | `38a33106115786780867f8f08a5968c0d80646e040c120e759e8c778f26f10b3` |

The local upstream commit must be published before a fresh public Go module proxy
can resolve it. Generated vendoring already supports an independent source build.
Neither repository nor this image was published by the cutover. A local image ID
is not a registry manifest digest; `versions.yaml` preserves that distinction.

The following checks passed against this candidate:

- Upstream full Go suite, plus real PostgreSQL 17.11 integration tests for all five
  promoted adapters: ledger, runtime, config, client credentials and saved traces.
  Each adapter suite used a fresh disposable database.
- Distribution race tests and vet, including authenticated tenant separation,
  streaming through the upstream data plane, attribution and credential rotation.
- Tests and a Linux amd64 cross-build from exported tracked source with an empty
  module cache and module network resolution disabled. The Linux arm64 Docker
  build also compiled with network access disabled and no sibling source input.
- Release-input checks, vendored source hashes and third-party notice hashes.
  The installed Compose gateway served the exact source archive embedded in its image.
- Integration static/lifecycle checks, 24 unit tests and fresh Compose rendering.
- Installed Compose gateway migration, concurrent catalog create/retry/CAS and
  stale-update refusal, provider credential rotation, and PostgreSQL attribution
  and usage accounting. Three browser scenarios passed: Sahara streaming/reload,
  cancellation followed by another turn, and recovery after a provider failure.
  The browser stream's attribution and token totals were checked in PostgreSQL.
- Installed Helm upgrades of shared services and both tenant releases, including
  the real gateway migration and Aether catalog publisher jobs on the new image.
  Kind acceptance passed concurrent create/retry/CAS and stale-update refusal,
  provider credential rotation, authenticated requests and PostgreSQL accounting.

These tests used synthetic tenants and deterministic model inference with real
platform services. They do not qualify external model providers, OAuth, production
rollouts, multi-replica behavior or a Linux amd64 container execution path. Historical
enterprise-image results elsewhere are separate from this replacement's evidence.

## Existing installation cutover

Keep gateway serving, migration jobs and tenant catalog publisher jobs on the same
image. Run database migrations before replacing the gateway; the promoted SQL
migration files retain their original bytes. Existing records and caller/catalog
configuration formats are preserved. Back up persistent databases before a real
installation upgrade and follow its normal change process.

For Helm, update the shared and tenant `images.sparkroute` values and use a new
`bootstrapRevision` so immutable migration/bootstrap/publisher Jobs receive new
names. When upgrading an older saved values set with a newer chart, merge the new
chart defaults underneath the saved values (`--reset-then-reuse-values` with the
tested Helm version), or supply a complete reviewed values file. Plain
`--reuse-values` failed rendering on the older fixture's missing work-profile
defaults; the defaults-aware retry succeeded. The first Kind acceptance attempt
also needed the existing backend helper image loaded into the local cluster;
acceptance passed after that test prerequisite was supplied.
