# Platform integration

Docker Compose development installation and shared, storage and tenant Helm
charts for the Scitrera platform.

- [Compose installation](docs/compose.md)
- [Tenant auth configuration](docs/tenant-auth.md)
- [Customer repository setup](docs/customer-repositories.md)
- [Helm prerequisites, configuration and phase commands](docs/helm.md)
- [State, backup and recovery](docs/operations.md)
- [Customer deployment configuration](docs/deployment.md)
- [Actual verification results and remaining gates](docs/verification.md)
- [Local acceptance commands](docs/acceptance.md)
- [Native model catalog contract](docs/model-catalog.md)
- [Authenticated tool hosts](docs/tool-host.md)

This candidate provides locally tested Compose and Helm reference profiles.
External OAuth and real model-provider tests are pending by operator choice.
Local fixture tests use the real platform services and allocation providers.

The installation consumes independent component artifacts. Local source builds
accept explicit paths and record input hashes. The gateway uses the AGPL
`scitrera/platform-sparkroute` distribution, with its pinned public SparkRoute
base and vendored dependencies. Its local candidate builds from one repository;
registry publication is separate from local verification. See the
[gateway distribution and verification record](docs/gateway-distribution.md).
No original monorepo, private administration application, hosted fleet state or
ArgoCD is an installation prerequisite.

## Embeddings and OCR

See [document services](docs/document-services.md) for the portable GPU Compose/Helm appliance, private Modal adapter, and independent configuration renderer. Existing synthetic fixtures remain unchanged; the real 1920-dimensional profile requires a prepared storage target and qualified component images.
