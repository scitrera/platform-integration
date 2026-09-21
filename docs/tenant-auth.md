# Configure an auth-go tenant

`scripts/auth_config.py` reconciles one tenant using auth-go's supported private
operator API. It can create the tenant, associate email domains, and set its
provider/claim checks and automatic-enrollment policy in one invocation. It does
not configure OAuth clients, contact an identity provider, enroll administrators,
or provision tenant Aether/storage/application services.

Start with [`examples/compose/auth-tenant.json`](../examples/compose/auth-tenant.json).
The `tenant` and `auth` objects use auth-go's native request fields; `domains` is
an exact set of email-domain associations. The example organization ID is a
placeholder. Keep operator credentials separate from this file.

```sh
# Offline validation; no credentials or running stack needed.
python3 scripts/auth_config.py check --file /path/to/customer-auth.json

# Preview against the selected installation without configuration writes.
python3 scripts/auth_config.py plan --file /path/to/customer-auth.json

# Create if missing, apply settings, and verify the result.
python3 scripts/auth_config.py apply --file /path/to/customer-auth.json
```

By default, the command uses the installation's loopback admin listener from
`.local/compose.env` and its `.local/operators.json` bootstrap token file. For a
separate deployment or Kubernetes port-forward, pass `--origin`, `--operators`
and optionally `--operator NAME`. The listener must already be running. Remote
operator access requires HTTPS; HTTP is restricted to loopback. Origin must
match auth-go's configured admin origin. Redirects are refused.

For an HTTPS-configured admin origin reached through a local tunnel, keep the
configured origin and use `--connect-to` for the transport endpoint:

```sh
python3 scripts/auth_config.py apply --file /path/to/customer-auth.json \
  --origin https://auth.example.com --connect-to http://127.0.0.1:18482 \
  --operators /path/to/private/operators.json
```

The tunnel must target the admin listener, not the public login listener. This
preserves Host, Origin, Secure session cookies and CSRF checks while sending the
request through the explicitly selected loopback port. Remote plaintext tunnels,
redirects and environment proxies are not allowed in this mode.

The token file has auth-go's bootstrap format, `{"operators":{"NAME":"TOKEN"}}`.
Use a private file, not a token argument or committed configuration. The client
opens an operator session, sends its CSRF token and revision preconditions, and
closes the session afterward. Provider policies may be prepared before OAuth
clients exist; this operation does not establish working end-user sign-in.

## Update behavior

- Reapplying matching configuration is a no-op, including the registry revision.
- A missing tenant is created disabled and enabled only after all settings
  succeed, if `tenant.enabled` is true.
- `domains` is authoritative: omitted existing associations are removed, and new
  ones are added. Users and memberships are never deleted. A domain owned by
  another tenant produces a conflict rather than being reassigned.
- `auth.auto_add` and the provider allowlist are authoritative. The tool requires
  a nonempty allowlist because auth-go interprets an empty one as unrestricted.
  Each supplied provider's checks replace that provider's checks. Omitted
  providers' check maps remain stored; `{}` or null removes an explicitly named
  map. Other auth settings are unchanged.
- Only supplied tenant metadata keys are managed. Null clears a key; omitted
  keys survive. `name` and `enabled` are managed, so setting enabled true also
  enables an existing disabled tenant.
- Auto-add is off while domain or policy changes are applied, then restored to
  the requested value. Existing users remain subject to provider/claim checks.

One invocation uses several API transactions. If an update fails midway, earlier
writes remain and enrollment may remain disabled. Inspect `plan`, resolve the
cause, and rerun `apply`. Concurrent configuration edits return a conflict; the
command never refreshes a stale revision and blindly retries. Plans are previews;
apply reads a new snapshot and carries its revision through every write.

The command refuses to apply against this installation's fixture listener when
`.local/fixtures.enabled` exists. Fixture enrollment remains handled by
`scripts/auth_setup.py` only when fixtures are enabled; ordinary startup ensures
tenant records without enrolling the example administrator. An explicit different
operator origin can target the actual deployment. No fixture identity is created by this configurator.

## Validation

Unit tests cover creation/update/no-op planning, interrupted runs, domain
replacement, stale-read/write conflicts, fixture protection and validation.
The API test uses owned, disposable auth-go and PostgreSQL containers on a
dedicated Docker network; it never connects to an existing deployment:

```sh
python3 tests/integration/auth_config.py \
  --auth-image YOUR_LOCAL_AUTH_IMAGE \
  --postgres-image YOUR_LOCAL_POSTGRES_IMAGE \
  --file examples/compose/auth-tenant.json
```

Images must already be available locally. The test removes only its own containers
and network. This verifies operator configuration, not live OAuth login.

## Explicit member claim overrides

Auth-go can attach selected claim overrides to an existing user–tenant
membership. An operator must create the user and membership first; this helper
never creates either, enables accounts, or changes tenant enrollment policy.
The provider must still be allowed by the tenant, and all unmodified tenant
claims remain required. Removing the membership removes the overrides; a later
auto-add starts with ordinary tenant checks.

Save an operator-managed file such as:

```json
{"version":1,"tenant":"example","email":"support@partner.example",
 "checks":{"azure":{"tid":["22222222-2222-4222-8222-222222222222"]}}}
```

Then run `scripts/auth_membership.py check`, `plan`, or `apply` with `--file`.
The origin, operator token file and optional loopback tunnel arguments are the
same as `auth_config.py`. Reapplying identical checks is a no-op; stale revisions
fail without retrying the edit. `checks: {}` removes all overrides and restores
inherited tenant checks. Provider maps are replaced in full. This requires the
auth-go membership override API and schema migration 002; older servers reject it.
