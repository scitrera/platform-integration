# Upstream ownership

Component fixes are committed in their owning repositories. The integration
selects immutable component revisions and owns composition and acceptance tests.
It does not distribute or apply implementation patches.

| Component | Selected commit | Review |
| --- | --- | --- |
| sahara-oss | `6ed204e9bb81f6f028e95fd10830659986c0adee` | [Draft PR](https://github.com/scitrera/agent-harness-go/pull/1) |
| aether | `bbc397f6a08521fcecfbf7fcb567456076f4e3ce` | [Draft PR](https://github.com/scitrera/aether/pull/7) |
| memorylayer-enterprise | `5e5c5e80a04f4091282bcb3aee951dbff15f9ec0` | [Draft PR](https://github.com/scitrera/memorylayer-enterprise/pull/2) |
| memorylayer-storage | `71d41d728572dfeffc36441b96d6954b607eb35e` | [Draft PR](https://github.com/scitrera/memorylayer-storage/pull/1) |
| platform-backend | `aba850337184b34d5d38a77c5fa46c991f5bcc6f` | Local component commit; remote not configured |
| platform-frontend | `10ce64e2426398b497ae93c8056c0a552861cb1b` | Local component commit; remote not configured |

Draft PRs are open; these commits have not been merged or tagged as releases.
The four upstream source archives were retrieved anonymously. Backend and
frontend remain local component candidates: supply those exact commits from
the prepared local repositories until remotes are configured. An image ID in
the compatibility manifest establishes local engine content, not registry availability.

Sahara consumes the committed harness and Aether SDK through go.mod/go.sum.
Its Dockerfile uses normal checksum-verified module downloads without local
replacements or git-apply steps. Once upstream merges or releases are selected,
advance those pins and rerun the component and installation acceptance tests.

## Validation scope

The component regression suites include shared-worker isolation, task query
correlation, subject/window authorization, proxy URL handling and concurrent
PostgreSQL migration. The frontend typecheck and 129 unit tests passed.
The installed image selection and current Compose acceptance are recorded in
versions.yaml. Helm render checks do not establish a new Kubernetes deployment.
External OAuth, real model-provider and Modal acceptance remain pending.
