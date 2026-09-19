# Browser source pages through blobgw-edge

An authenticated application asks MemoryLayer for a browser page descriptor after
checking current workspace access and the reviewed source revision. Image bytes
use the existing `/storage/{tenant}/blob/...` browser route; Aether carries only
metadata and the capability. The browser gateway strips caller identity headers
and stamps the current auth-go tenant/user. Do not expose edge capability minting
or the internal blob gateway directly to browsers.

The MemoryLayer browser endpoint requires canonical tenant-scoped blobgw storage
with HTTP tenant routing enabled by `-tenant-config`, and an edge revision that
enforces GET/HEAD `content_hash`. It returns 120-second,
user-bound links with no-store responses. The frontend loads only nearby images.

## Configuration and migration

Compose sets the URL/domain defaults but preserves local storage unless
`MEMORYLAYER_BLOB_STORAGE_SERVICE_<TENANT>=blobgw` is selected in the installation
environment (an explicit service overlay can also select it). Helm uses
`documentBlobStorage.provider: blobgw`; its default remains `default` (local PVC).
MemoryLayer-to-edge TCP 8090 already supports document fetching. The additional
storage policy admits only MemoryLayer pods from labeled tenant namespaces to
blobgw TCP 8080 for canonical blob I/O; it grants no web-to-MemoryLayer access.

Before changing an existing installation, stop ingestion, copy and verify all
local canonical blobs using MemoryLayer enterprise's
`scripts/migrate_document_blobs.py`, keeping their original base path. Retain the
local volume. Do not change the provider until every destination object has been
read back and hash-verified in the intended tenant domain (verified by
`X-Blobgw-Domain`, including a read-only preflight before any writes). See the enterprise `docs/source-page-delivery.md`
for the command and rollback procedure. Existing source-file descriptors used by
agent task-local materialization remain supported.

In Compose, blobgw's S3 backend is the local MinIO `objects` service, persisted
in a Docker volume; its index is in PostgreSQL. A production S3 endpoint can be
configured separately. Changing the document blob provider does not itself move
data off the deployment machine or change the tenant's backing S3 credentials.
