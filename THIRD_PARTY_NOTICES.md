# Component terms

New integration code is AGPL-3.0-only. Component implementations remain in their
own repositories. Their exact source revisions, local image IDs and source
relationships are recorded in versions.yaml; docs/upstream.md explains ownership.

Platform backend and frontend are primarily AGPL-3.0-only. Backend's
sandbox/components/execd and sandbox/images/sandbox-sidecar2 retain Apache-2.0.
The agent-harness-go and Aether SDK dependencies are Apache-2.0. Their owning
distributions preserve license and notice texts.

Aether and MemoryLayer core are Apache-2.0. MemoryLayer enterprise/data-connectors
and storage gateway/edge are AGPL-3.0-only. Storage libraries retain their existing
Apache-2.0 terms. Auth-go is AGPL-3.0-only.

PostgreSQL, Nginx, Valkey, MinIO, Kubernetes operators, build tools and base
distributions retain their upstream terms and embedded notices. Their source is
not vendored here. The three application charts have no bundled chart dependencies;
operators are installed separately using the prerequisite lock.

The gateway is the AGPL-3.0-only `scitrera/platform-sparkroute` distribution of
public SparkRoute. Its image retains Scitrera LLC and Fox Engine Ltd. copyright
notices, dependency licenses and its matching source archive. Source and registry
publication of the prepared candidate remain separate release steps.
The web serving image likewise includes its matching source archive and notices.
