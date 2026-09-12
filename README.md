# Platform integration

Docker Compose development installation and shared, storage and tenant Helm
charts for the Scitrera platform.

- [Compose installation](docs/compose.md)
- [Helm prerequisites, configuration and phase commands](docs/helm.md)
- [State, backup and recovery](docs/operations.md)
- [Actual verification results and remaining gates](docs/verification.md)
- [Local acceptance commands](docs/acceptance.md)
- [Native model catalog contract](docs/model-catalog.md)
- [Authenticated tool hosts](docs/tool-host.md)

This candidate provides locally tested Compose and Helm reference profiles.
External OAuth and real model-provider tests are pending by operator choice.
Local fixture tests use the real platform services and allocation providers.

The installation consumes independent component artifacts. Local source builds
accept explicit paths and record input hashes. SparkRoute enterprise is a
separately permitted operator input; local verification does not establish public
artifact availability. No original monorepo, private administration application,
hosted fleet state or ArgoCD is an installation prerequisite.
