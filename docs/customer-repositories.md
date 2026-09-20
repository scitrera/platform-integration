# Common setup for customer repositories

Customer repositories keep their proprietary code, apps, skills, work profiles,
and policy values. Platform-integration owns the reusable installation mechanisms:

| Responsibility | Shared entry point |
| --- | --- |
| Compose tenant definitions, service generation, browser origins and TLS bootstrap | `scripts/configure.py --tenants FILE` |
| Selected images and source builds | `scripts/build.py` |
| Auth-go tenant creation, domains, provider checks and auto-add | `scripts/auth_config.py check/plan/apply --file FILE` |
| Model YAML validation, routing and updates | `scripts/models.py` |
| Compose lifecycle and readiness with customer extensions | `scripts/dev.py ACTION --overlay FILE` |
| Compose commands consumed by customer Python wrappers | `compose.command(*args, overlays=[Path(...)])` |

A customer launcher can invoke the shared lifecycle command directly:

```sh
python3 /path/to/platform-integration/scripts/dev.py up \
  --overlay /path/to/customer/.local/compose.customer.yaml
```

`--overlay` is repeatable. Overlays apply in supplied order after the generated
base, fixture and optional Modal files, before the Compose command. The same
composed command is used for startup, model route application, shutdown checks,
and readiness. Overlay paths must name existing files. Existing callers with no
overlay retain the base installation behavior.

Python callers can import `scripts/compose.py` from their selected integration
checkout and bind `overlays` with `functools.partial`. They do not need to patch
module globals or reconstruct Docker Compose argument order. Prefer invoking
`dev.py` as a subprocess to keep each selected installation's imports isolated.

JGL uses these shared auth and Compose interfaces. Its launcher still owns JGL
source mounts/custom image selection, its pipeline worker, application setup and
compiled work-profile instructions. Source-version selection is also still in the
customer bootstrap. These are explicit boundaries, not a generic customer plugin
framework. Future extractions should have a concrete second caller and keep
customer policy out of the shared implementation.

### Browser login lifetime

The deployment compiler accepts `auth.sessionTTL` (default `24h`) and renders
`AUTH_PROXY_SESSION_TTL` for local Compose auth and `authSession.ttl` for the
shared Helm chart. Positive integer durations in seconds, minutes or hours are
supported, up to 365 days; use `168h` for seven days. A shared auth operator must
choose one common lifetime across tenants. This fixed sign-in lifetime does not
extend existing sessions when configuration changes. It is distinct from task
authority and operator-dashboard TTLs.
