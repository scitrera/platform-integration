# Local acceptance commands

Run only against the disposable fixture profiles. These tests use the real
platform services with synthetic identity and inference. Keep raw logs, private
configuration and Playwright diagnostics under .local; they are not CI artifacts.

## Fresh Compose checkout

Supply the reviewed builder's private image manifest. Images must already exist
in the local Docker engine and match their recorded IDs:

    python3 scripts/run_compose_smoke.py --images /private/images.json --directory /tmp/platform-smoke-new --base-port 18100

The directory must not exist. The helper generates independent secrets and a
unique project, installs the whole stack, runs 17 browser scenarios, checks the
actual streamed task's gateway ledger, repeats bootstrap and verifies the saved
file/chat. It retains the named project for review. Release its allocations
through the SDK before dev.py down; named volumes remain.

## Existing fixture installation

Set the configured origins and operator file path:

    export PLATFORM_ORIGIN=http://127.0.0.1:18080
    export FIXTURE_IDP_ORIGIN=http://127.0.0.1:18090
    export AUTH_OPERATOR_ORIGIN=http://127.0.0.1:18082
    export AUTH_OPERATORS_FILE="$PWD/.local/operators.json"
    export GATEWAY_USAGE_RECORD="$PWD/.local/gateway-usage.json"
    npx playwright test

The operator tests temporarily enroll only denied@example.test, verify that
membership alone cannot authorize Admin, then remove membership and revoke its
sessions. Another test revokes Alice's fixture sessions. Use these dedicated
fixture identities; do not point the tests at real users.

For Kind, use PLATFORM_PROFILE=kind,
PLATFORM_ORIGIN=https://platform.example.test:18443,
FIXTURE_IDP_ORIGIN=http://127.0.0.1:18091 and
AUTH_OPERATOR_ORIGIN=http://127.0.0.1:18083. See helm.md for the explicit
port-forwards. Tests map the synthetic hostname to loopback.

Run the installed catalog and provider-key drill:

    python3 scripts/check_gateway.py --profile compose --project platform-smoke-EXACT_ID --usage-record .local/gateway-usage.json

Compose requires the exact generated platform-smoke-* project. For Kind:

    python3 scripts/check_gateway.py --profile kind --kubeconfig .local/cluster/kubeconfig --context kind-platform-integration --usage-record .local/gateway-usage.json

The drill creates a unique expiring fixture alias, tests concurrent native
publisher processes, rotates a random provider credential and checks usage.
Cleanup removes the test alias, credential and temporary Kubernetes resources.
A failed cleanup exits nonzero; inspect its private evidence directory.
Add --usage-only to read the recorded browser turn without publication or rotation.

## Controlled expiry and worker interruption

Run the expiry controller with the configured origins already exported:

    python3 tests/integration/session_expiry.py --profile compose --origin "$PLATFORM_ORIGIN" --expect-project platform-smoke-EXACT_ID

For Kind substitute --profile kind and supply
--kubeconfig .local/cluster/kubeconfig --context kind-platform-integration.
The controller sets a real three-second server lifetime, runs the browser case,
and restores baseline configuration in finally. Restart the auth operator
port-forward after the Kind rollout.

Select an existing alpha/Alice allocation by its full UUID for the worker drill:

    WORKER_SANDBOX_ID=FULL_UUID WORKER_PROJECT=platform-smoke-EXACT_ID npx playwright test --grep 'unavailable worker'

For Kind, export WORKER_KUBECONFIG, WORKER_CONTEXT=kind-platform-integration and
WORKER_POD as well as WORKER_SANDBOX_ID and PLATFORM_PROFILE=kind. The helper
verifies the pod's tenant, owner-thread and full allocation labels. It signals
only the Sahara executable and resumes it in finally.

These drills temporarily interrupt a disposable session or process. They do not
prove crashed-worker replacement, multi-replica failover or external-provider
behavior. Recovery/retention procedures and their limits are in operations.md.
