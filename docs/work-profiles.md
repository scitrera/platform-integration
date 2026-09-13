# Work profiles

Work profiles share one Sahara worker per tenant and task type, selected when each conversation is created. Personal conversations remain available.

For Compose, copy examples/work-profiles.json to an operator-owned file and set WORK_PROFILES_FILE to its absolute path in .local/compose.env. The default compose/config/work-profiles.json is empty. platform-server, platform-bridge and sandbox-provider mount the same read-only registry. Restart those services to load changes; drain and release existing profile workers before changing their runtime settings.

For Helm, set these tenant-chart values:

```yaml
workProfiles:
  profiles:
    document-review:
      instructions: Review the caller's documents using authorized application tools.
      max_concurrent_turns: 4
      idle_timeout_seconds: 3600
  allowedProfiles:
    - document-review
```

The chart creates a tenant-specific ConfigMap mounted into all three routing services. Profile changes roll those deployments; allocated workers must still be drained and released to pick up runtime changes.

Users still need their normal workspace write permission and access to the application tools used by the profile. Allowing a profile does not grant workspace access or an administrator role.

The web **New Thread** controls show profile selection only when the tenant UI config sets `enableWorkProfileSelection: true`. Its general default is `false`, even when profiles are configured. It uses the same tenant UI config as `enableThreads`, `workspaceAsThread` and `workspaceHomedThreads`. The integration smoke's `--work-profiles` option explicitly enables the chooser for tenant alpha.

Configure it with the existing tenant interface: `await ti.set_ui_config_variables(enableWorkProfileSelection=True)` (or `False` to hide it), then refresh the browser. Hiding the chooser does not disable profile routing or change saved conversation bindings. New conversations created through the generic UI without a chooser omit an explicit profile selection, allowing configured creation defaults to apply. A custom application uses platform-bridge thread.create with work_profile, then chat.send with the returned thread_id. The profile remains bound to that conversation. agent.synthesize also accepts work_profile for one-shot requests.

Shared workers use delegated tools and per-turn remote history. Unrestricted local code/filesystem tools, personal notes/bootstrap, subagents and autonomous goals are disabled for this pilot. GPU work can be delegated to Modal by an application tool; a Modal adapter is not included here.

External OAuth, model-provider and Modal acceptance are pending. Local fixture acceptance is recorded separately.

On development hosts that exhaust Docker's automatic subnet pool, sandbox-provider supports SANDBOX_DOCKER_SUBNET_POOL, an operator-selected unused IPv4 /16 through /24. It allocates /28 worker networks and retries bounded collisions; unset retains Docker defaults. Select a pool that does not overlap LAN, VPN or Kubernetes routes. Compose service networks can independently use standard explicit IPAM configuration. The local acceptance run uses this option without removing any existing networks.

Run the disposable two-user browser acceptance with `python3 scripts/run_compose_smoke.py --images .local/images.json --directory /tmp/profile-acceptance --work-profiles`. It checks shared allocation, private history after reload, immutable conversation selection, cross-user cancellation denial, the originating user's tool approval, and per-user gateway accounting. The second fixture user receives only temporary workspace/tool-service grants; cleanup removes those grants and tenant membership.

See [local validation results](work-profile-verification.md) for the tested scope and pending external checks.
