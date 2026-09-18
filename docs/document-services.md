# Embeddings and OCR deployment

Document services have their own versioned configuration, independent of `models.yaml` and chat routes. The `qwen-colmodern-unlimited-l4-v1` profile serves Qwen3-VL text/image vectors at **1920 dimensions** (prefix truncation, no renormalization), ColModernVBERT multivectors, and Unlimited-OCR with grounded layout metadata. It requires the companion MemoryLayer client/server changes and a qualified image from memorylayer-enterprise `memorylayer-embed-server-enterprise/deploy/l4`.

## Consumer configuration

Example for Modal; replace the endpoint and digest with qualified artifacts. Credentials are references, never values:

```yaml
version: 1
profile: qwen-colmodern-unlimited-l4-v1
transport: modal
endpoint: https://your-embed-app.modal.run
proxy:
  image: registry.example/embed-proxy@sha256:REPLACE_WITH_MANIFEST_DIGEST
  secret_name: embed-modal
  key_env: EMBED_MODAL_KEY
  secret_env: EMBED_MODAL_SECRET
```

For a private self-hosted server use `transport: http`, its root URL (e.g. `http://embed-server:61051`), and omit `proxy`.

```sh
python scripts/document_services.py check --config /private/document-services.yaml
python scripts/document_services.py check --config /private/document-services.yaml --check-secrets
python scripts/document_services.py render --config /private/document-services.yaml --tenant alpha --storage-dimensions 1920 --output /tmp/document-services-rendered
```

`check` never contacts providers or modifies services. `render` creates a new directory containing `compose.yaml`, `helm.yaml`, and a model-identity manifest. It requires an explicit prepared 1920-dimensional target. These files are **staged only**; the command never runs Compose, Helm, SQL, reindexing or document reprocessing. Apply them through the existing customer overlay/Helm deployment workflow after reviewing storage readiness and image versions.

The Compose overlay updates both MemoryLayer and its migration job. It adds a private tenant-network adapter without published ports. The Helm values set the same dimensions on MemoryLayer and bootstrap/migration and, for Modal, add a loopback sidecar. Create the referenced Kubernetes Secret separately with `MODAL_KEY`/`MODAL_SECRET` keys. These are Modal **proxy tokens**, not deployment-account tokens. MemoryLayer itself never receives them.

For a local Compose installation before registry publication, `--allow-local-image` accepts an immutable `sha256:...` proxy image ID already present in that Docker engine. This mode emits Compose and the identity manifest only; it omits Helm because Kubernetes cannot pull engine-local image IDs. Registry manifest digests remain required by default.

The adapter image is built from `services/embed-proxy/`; its Dockerfile supports ARM64 and AMD64. Pin a published manifest digest for production. The adapter injects credentials, follows only same-origin HTTPS 303 continuations using GET without the POST body, bounds redirects/response sizes/total time, and never retries inference POSTs. It rejects foreign origins and 307/308 replay requests. Its `/health/live` is adapter liveness; the forwarded `/health/ready` checks remote models. See [Modal continuation behavior](https://modal.com/docs/guide/webhook-timeouts).

MemoryLayer uses HTTP transport, the `embed_server` embedding and transcription services, timeout 1860s, and one-image batches. It retains original page indexes across split requests. The profile disables neural NER, document chat and visual tokenizer, and does not use Sparkroute for OCR. Ordinary regex extraction remains available. The serving envelope accepts one image up to 2048 pixels on each edge and 4M decoded pixels, eight text inputs within 6144 UTF-8 bytes (3072 for multivector), and 4096 OCR output tokens. Ordinary PDF pages at 150 DPI fit; larger scans require preprocessing.

## Modal application lifecycle and scaling

Use the enterprise `deploy/l4/modal_app.py` wrapper for both ephemeral (`modal serve`, for development) and persistent (`modal deploy`, for integration) web endpoints. Independently select `MEMORYLAYER_MODAL_MODE=scale-to-zero` (min 0/max 1) or `always-warm` (min 1/max 1). The production integration should reference a **persistent** endpoint; persistent does not imply an always-running GPU. Both policies use the same proxy authentication and continuation handling. All Modal functions retain default CPU/RAM requests.

The synthetic L4 startup measured about 6½ minutes, so always-warm is the recommended interactive production policy if continuous GPU usage is acceptable. Scale-to-zero is useful for occasional workloads that tolerate startup latency. Warm mode cannot eliminate infrastructure restarts. Prepare the model volume before either mode; the enterprise runbook has the full command matrix and measured qualification. No persistent deployment is created by the build/qualification workflow.

## GPU Docker Compose

This standalone file serves the generic appliance; it contains no tenant or customer services. Build the image using the enterprise build-context script first. Set `EMBED_SERVER_IMAGE` to that image (an immutable digest for shared deployments).

```sh
docker compose -f compose/profiles/embed-gpu.yaml --profile prepare run --rm embed-prepare
docker compose -f compose/profiles/embed-gpu.yaml up -d embed-server
```

Preparation mounts the same model volume and runs no inference engine. Serving uses one visible GPU, one container for all three engines, 4 GiB shared memory, and a loopback-published API. No Docker socket is mounted. For remote consumers use a private network/authenticated ingress rather than exposing the raw GPU service publicly.

## GPU Kubernetes Helm

The NVIDIA device plugin, an L4 node and a suitable StorageClass/PVC must already exist. No three-pod GPU subdivision is used.

```sh
helm upgrade --install embed charts/memorylayer-embed -n embed --create-namespace --set image=REGISTRY_IMAGE_AT_DIGEST --set phase=prepare --set cache.storageClassName=YOUR_CLASS
kubectl -n embed wait --for=condition=complete job/embed-prepare --timeout=1800s
helm upgrade embed charts/memorylayer-embed -n embed --reuse-values --set phase=serve
```

Preparation is a CPU-only Job scheduled on the selected storage/GPU node class; serving requests **one** `nvidia.com/gpu` for the entire pod. Recreate strategy and two explicit phases accommodate an RWO cache. Existing claims can be supplied via `cache.existingClaim`. The PVC has keep semantics. The service is ClusterIP and the ingress policy permits MemoryLayer pods; do not add public ingress without authentication. Adapt node selectors to the cluster's actual GPU labels. If a failed preparation Job remains, inspect it and explicitly replace that Job before retrying; never delete the cache to resolve a Job status problem.

## Storage and release gates

A model or preprocessing change requires re-embedding even if its dimensions stay the same. This profile pins native vLLM 0.25.0 ColModernVBERT image processing with image splitting disabled and pool factor 1. It is not index-compatible by assumption with tiled Sentence Transformers/ColPali processing; qualify representative visual retrieval before switching. The existing synthetic fixtures remain 1536-dimensional. No active configuration, database or fixture default is changed merely by adding this profile. Plan migration or a new isolated 1920-dimensional database, retain existing data, and validate retrieval/citations before switching traffic.

Publish/review the component source changes and image manifests, then update the consumer lock and image pins together. The serving artifact manifest records actual wheel hashes because unpublished local edits are not represented by an old Git commit. A candidate image is not a production-qualified image. Run the enterprise L4 synthetic qualification, proxy tests, `scripts/check.py`, and a disposable tenant ingest/retrieval acceptance before customer rollout. Source URLs, filenames, tables and figure citations must survive; a successful HTTP status alone is insufficient.

## Split v2 services (opt-in)

V1 remains supported. V2 runs the embedding and transcription profiles independently
and keeps **one proxy and one consumer URL**. Nothing is applied by rendering.
Do not change existing JGL configuration as part of qualifying this distribution.

```yaml
version: 2
profile: qwen-colmodern-dual-ocr-l4-v2
proxy:
  image: registry.example/embed-proxy@sha256:REPLACE_WITH_64_HEX_DIGEST
services:
  embedding:
    transport: modal
    endpoint: https://EMBEDDING-APP.modal.run
    capabilities: [single_vector, multi_vector, score, ner]
    auth:
      secret_name: embedding-modal
      key_env: EMBED_MODAL_KEY
      secret_env: EMBED_MODAL_SECRET
  transcription:
    transport: modal
    endpoint: https://TRANSCRIPTION-APP.modal.run
    providers: [unlimited_ocr, deepseek_ocr]
    policy: fallback
    auth:
      secret_name: transcription-modal
      key_env: OCR_MODAL_KEY
      secret_env: OCR_MODAL_SECRET
```

Render with `scripts/document_services.py render` as for v1. The Compose output
expects `EMBED_MODAL_KEY_FILE`, `EMBED_MODAL_SECRET_FILE`, `OCR_MODAL_KEY_FILE` and
`OCR_MODAL_SECRET_FILE` to name private host files. They may refer to the same
workspace credential when appropriately scoped. Values are mounted read-only and
are never included in generated manifests. Helm references existing Secrets, each
with `MODAL_KEY` and `MODAL_SECRET` keys. It renders one loopback proxy sidecar.
For self-hosted services, use `transport: http` and an internal service origin;
omit that role's `auth` block. Mixed HTTP/Modal transport is supported.

The proxy's local `/health/live` never wakes a GPU. Operator checks
`/health/embedding/ready` and `/health/transcription/ready` inspect only that role.
`/health/ready` explicitly probes both and may wake both. An unhealthy remote role
does not withdraw the healthy role or fail the proxy's local readiness probe.
Continuations stay on the selected Modal origin, including when the other role's
origin is also configured. There are no inference POST retries in the proxy.
V2 permits 32 embedding and 16 transcription forwards, with separate bounded
queues of 64 and 32. Large responses spill to a private `/tmp` volume above 1 MiB;
admission remains held until delivery or disconnect. The split Helm sidecar keeps
its 128 MiB RAM request and allows a 2 GiB limit, plus a 12 GiB disk-backed spool.
Compose uses a private named spool volume and a 2 GiB RAM limit. Neither is a GPU
reservation; monitor ephemeral disk usage when clients read slowly.

The renderer keeps 1920 dimensions, emits a separate transcription identity, and
records `compatible_embedding_cache_identity` for a v1-to-v2 migration that retains
the exact Qwen/ColModern models. A customer staging script must deliberately preserve
that compatible cache alias; do not blindly replace existing cache namespaces or
reset stored vectors. NER availability does not select a tenant extraction provider.
The current enterprise synchronous NER caller needs async offloading before it is
enabled in an async ingestion path; v2 rendering leaves extraction selection alone.

V2 enables bounded image fan-out for consumers built from the new client source.
Optional `consumer: {embedding_concurrency: 16, transcription_concurrency: 8}` sets
separate process-wide role budgets, using `MEMORYLAYER_EMBED_IMAGE_CONCURRENCY`,
`MEMORYLAYER_EMBED_TEXT_CONCURRENCY`, and `MEMORYLAYER_EMBED_TRANSCRIPTION_CONCURRENCY`.
The default remains two image/OCR requests and serial text batches. Fan-out preserves
page indexes and failed-page results. Updated clients retry POSTs only on pre-send
connection failures; an ambiguous read/protocol failure is surfaced without replay.
Older consumers ignore these new settings and must be upgraded for this behavior.

`services.transcription.providers` accepts either `[unlimited_ocr, deepseek_ocr]` or
`[deepseek_ocr, unlimited_ocr]`. The renderer passes the order through
`MEMORYLAYER_EMBED_TRANSCRIPTION_PROVIDERS`; the consumer selects each exact remote
provider, advancing only confirmed failed pages. The order affects transcription
identity only. This allows customers to override the remote default without deploying
model servers. Both-failed pages stay failed; transport or malformed-response failures
are not silently retried against another provider.

### Serving on Compose or Helm

`compose/profiles/embed-gpu-v2.yaml` provides two preparation jobs, two GPU services,
separate caches and one CPU proxy. Set immutable `EMBEDDING_SERVER_IMAGE`,
`TRANSCRIPTION_SERVER_IMAGE`, `EMBED_PROXY_IMAGE` plus **different physical GPU UUIDs**
for `EMBEDDING_GPU_ID` and `TRANSCRIPTION_GPU_ID`. Run each preparation job first;
then start embedding, transcription and proxy. The default public binding is
loopback `127.0.0.1:61051`, and both model services stay internal. Do not use the same
GPU UUID for both roles or claim two-L4 capacity with GPU time slicing.

For Kubernetes, install `charts/memorylayer-embed` twice, as releases `embedding-v2`
and `transcription-v2`, with their respective immutable profile images. Each release
has a separate PVC and preparation Job; switch its `phase` to `serve` after that Job
succeeds. Each serving deployment requests one GPU. Use the resulting internal
origins (for example `http://embedding-v2:61051`) in the v2 document-services config
and render the tenant proxy overlay. Both releases can be installed/rolled back
independently. Modal consumption needs only the proxy, with no local GPU releases.

Compose/Helm rendering is tested separately from Modal GPU qualification. Live GPU
execution on a particular Docker/Kubernetes installation still requires acceptance
on that installation. Registry image publication is a separate release step.
