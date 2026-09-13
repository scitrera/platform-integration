# Work-profile validation — 2026-09-12

Validated with local source builds on Linux/arm64 and synthetic OAuth/model-provider fixtures. External provider acceptance is pending; these results do not establish real provider or production Kubernetes acceptance.

| Check | Result |
| --- | --- |
| Backend routing, bridge, websocket and work-profile tests | 342 passed |
| Frontend typecheck and chat-state hooks | Passed; 6 hook tests |
| Go race checks | Shared history/catalog, runtime routing/steering, controls, per-turn attribution, allocation policy and sidecar configuration passed |
| Compose/Helm configuration | 9 unit tests; all chart phases, nonempty profile rendering and digest refusal passed |
| Two-user shared-worker browser scenario | Passed: one worker, separate saved histories, immutable selection, cross-user cancellation refused, approval on the originating browser, delegated tool execution |
| Shared-worker gateway ledger | 4 tasks and 5 model calls attributed to their respective users and conversations |
| Existing personal browser flows | 17 passed; 3 preexisting opt-in/conditional scenarios skipped |
| Repeated Compose startup and persistence | Passed |
| LAN browser flows | Login, native socket, personal chat, authenticated file access, logout passed; profile selection, reload and cancellation passed |

The shared-worker scenario uses one admin fixture and one ordinary member with temporary WRITE access to the test workspace and the relevant tool services. It does not grant the member an administrator role. It confirms a shared Sahara allocation has two containers, without a code sidecar.

Runtime acceptance found and resolved three integration gaps: canonical SDK principal types in task-control checks, task lookups blocking the receive callback, and missing per-turn user attribution on shared model requests. A registry-removal regression also verifies that previously bound conversations cannot silently fall back to personal workers.

The pilot disables unrestricted local filesystem/code tools, personal notes/bootstrap, subagents and autonomous goals on shared workers. Application tools can delegate GPU work; no Modal adapter is included. Real OAuth, real model providers, Modal and dynamic Kubernetes work-profile acceptance remain pending.

The tenant UI option `enableWorkProfileSelection` defaults off. Additional browser acceptance verifies disabled and enabled settings, and application-created profile bindings while the chooser is hidden. The integration example explicitly enables it. Frontend typechecking and 8 focused tests, including removal and tenant switching of UI overrides, pass.


## Component commit cutover

The integration now consumes clean component commits listed in versions.yaml.
Nineteen images built successfully, including Sahara with normal public Go
module downloads and no dependency patches or local replacements. The frontend
typecheck and all 129 unit tests passed.

Acceptance was repeated with the new images on the disposable Compose stack:
the two-user shared worker, per-user model accounting, all 17 personal browser
scenarios (3 conditional skips), repeated bootstrap, saved file/chat recovery and
chooser visibility passed. Dynamic Sahara, sidecar and code containers were
checked against the selected image IDs. The LAN preview was not redeployed by
this source cutover.

The four upstream review links and component ownership are in
[upstream.md](upstream.md). New Kubernetes runtime acceptance, external OAuth,
real model-provider and Modal tests remain pending.
