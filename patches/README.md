# Selected component source changes

These patches reproduce the source snapshots used for the images in versions.yaml.
They are alternatives against each source set's exact base revision. Do not stack
patches for different snapshots of the same component.

Use a separate clean component checkout for each selected source set:

    git checkout --detach BASE_REVISION
    git apply --check /path/to/platform-integration/patches/SELECTED.patch
    git apply /path/to/platform-integration/patches/SELECTED.patch

The builder requires an explicit sources JSON path map, --allow-dirty, and an
--include-untracked JSON mapping the component name to the source set's
include_untracked list. Build the image named by that source set. The builder's
tracked_content_sha256 must equal versions.yaml before relying on that source
selection. File paths are sorted; the hash covers each path, NUL, and the SHA-256
of its content. File modes are preserved by the patches but are not in this hash.

The sidecar was built with SIDECAR_AETHER_BUILD_BASE. Select that artifact as the
builder's AETHER_IMAGE input while rebuilding the sidecar, then restore the
separate tenant AETHER_IMAGE selection. The code image similarly consumes the
recorded CODE_BASE_IMAGE. These dependency image IDs are listed explicitly.

Auth-go's selected source is its base revision without a patch. Web's image
contains the matching source archive; its checksum is recorded in versions.yaml.
SparkRoute enterprise source is excluded. Supply a separately permitted image or
use the documented private source build contract.

Source availability and image registry availability remain separate pending
checks. Patches preserve the owning component's licenses; see
../THIRD_PARTY_NOTICES.md. They include source code and are not relicensed under
the integration repository's default.
