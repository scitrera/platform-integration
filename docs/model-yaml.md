# YAML model configuration for Compose

`scripts/models.py` compiles endpoint definitions and alias routes into the
owner's SparkRoute model catalog format. It supports OpenAI-compatible endpoints,
Gemini native endpoints, Modal proxy key/secret headers, bearer authentication,
explicit HTTP opt-in, concurrency and capability declarations.

```yaml
version: 1
models:
  review:
    provider: openai_compatible
    base_url: https://YOUR-ENDPOINT.modal.run/v1
    model: YOUR-SERVED-MODEL-ID
    modal_key_env: MODEL_PROXY_KEY
    modal_secret_env: MODEL_PROXY_SECRET
    # api_key_env: MODEL_API_KEY  # Optional additional bearer authentication.
    max_concurrency: 2
    capabilities: [tools, vision]
routes:
  sahara-default: review
  sahara-text-advanced: review
  sahara-vision-advanced: review
  memorylayer-default: review
```

Supply the named credential environment variables. For Google use `provider:
gemini`, its native `/v1beta` base URL and `api_key_env`; the compiler selects
`x-goog-api-key`. A plain HTTP endpoint requires `allow_http: true`. Model names
are local definition keys; the `model` value is the exact upstream served ID.
Additional tenant aliases are supported. `memorylayer-default` is a special
central route, shared across this installation's tenants.

After configuring the integration installation and selecting images:

```sh
python3 scripts/models.py check --file /private/models.yaml --tenant example
python3 scripts/models.py stage --file /private/models.yaml --tenant example
python3 scripts/dev.py up --fixtures
```

`stage` writes a private snapshot for startup. During `up`, authentication is
initialized first, then models are published before ordinary catalog dependency
jobs and gateway readiness. YAML routes can coexist with synthetic OAuth and
embeddings; fixture setup retains managed routing on later starts. Without the
fixture profile, supply the ordinary external identity, gateway caller and TLS
bootstrap inputs described in the deployment documentation.

For subsequent changes to a running installation:

```sh
python3 scripts/models.py apply --file /private/models.yaml --tenant example
```

Apply stages the file, validates central and tenant documents with the selected
SparkRoute image under `--network none`, and invokes the owner catalog publisher
with the exact prior canonical record as `--expected`. After each successful
publication, its canonical file is atomically replaced. Finally the central
MemoryLayer route and provider host policy are written and the gateway is
recreated. A local installation lock prevents overlapping model operations.
Concurrent remote changes are rejected by Aether compare-and-swap. A batch is
not a transaction: already published aliases remain applied, and retry resumes
safely. Startup also replays the staged plan before the bootstrap jobs, including
recovery when publication succeeded just before a local process crash.

Unmentioned routes are retained; empty `models`/`routes` mappings are a no-op.
Central aliases shadowing requested tenant aliases are rejected. Unrelated
central definitions are retained, and generated central provider/deployment names
are immutable across revisions so updates do not alter another route's targets.
Existing provider host allowances and HTTP permission are preserved; new hosts
are added automatically. Tightening manually managed policy remains an operator
action after checking all active routes.

Credentials are saved privately in `.local/models/<tenant>.json` (0600), cached
by environment variable name. An unset variable reuses the saved credential; a
newly named or explicitly empty variable requires a valid value. Setting a new
value and applying rotates credentials. Generated gateway credential files use
new names when values change, preserving the credentials of old or partially
updated routes. Catalog records contain file references only. Old credential
files and provider objects are retained for recovery, not automatically pruned.
Owner command diagnostics go to `.local/models/last-command.log`; inspect them
privately. The checker verifies YAML/credential availability only; apply also
validates owner schemas, but neither performs inference acceptance.

A customer launcher may import `stage(root, file, tenant)` during configuration
and use `apply_plan(root, tenant, compose_command, expected_plan=plan)` for updates.
The Compose command callback preserves that launcher's overlays and project
isolation. The owning integration `dev.py` handles startup replay. Keep generic
routing logic here; customer repositories supply their own commented YAML and
thin launcher.
