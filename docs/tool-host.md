# Tool-host boundary

Both profiles expose exactly /TENANT/tools/v1/connect. This endpoint accepts
Aether user API credentials. Auth-go browser cookies are a separate credential
system. Office, local-agent and MCP clients remain experimental; deploying this
listener does not establish those clients as supported.

The tool host verifies the credential through the tenant's private Aether Lite
/auth/verify endpoint, then opens two mTLS connections: an anonymous-certificate
user session authenticated with the supplied token, and a named
tools-wss-client service session. It exchanges session-bound delegated authority,
registers the tool catalog through Platform Bridge and uses checked RPCs.

The selected profile sets TOOLS_WSS_AUTH_WORKSPACE and the registry workspace to
default. Verification uses that exact workspace; caller-provided workspace
headers cannot select it. The exchanged grant has the same workspace scope,
and RPC workspace selection is checked before delivery. User-owned Office
thread storage remains the owning component's user-scoped contract.

Service permission is explicit. Production serviceGrants defaults to an empty
list. Operators provision intended users through the public ACL bootstrap input;
there is no wildcard user grant in the baseline. The local fixture overlay
selects only the named synthetic user in each tenant:

- tools-wss-client may exchange authority for that exact active user session.
- It may send RPCs to Platform Bridge at READWRITE.
- It receives no additional MemoryLayer, Data Connectors, KV or admin grants.

Aether's canonical user identifier is the raw user ID, and user session identity
strings omit workspace. Its narrow capability identifier therefore uses
capability/exchange_authority_grants/_no_workspace/USER_ID. The verifier and
delegated grant enforce the configured workspace separately.

The public proxy forwards credentials and WebSocket protocol headers and strips
caller identity headers. Aether's verifier has no public route. tools-wss's
private host policy and allowed-origin list remain enabled.

The common local probe tests real credential validation, public TLS on Kind,
tool registration, a checked thread RPC, and refusal of invalid credentials,
another tenant, an unapproved browser origin and another selected workspace.
Tokens are generated through the public Aether admin API, expire after one hour,
and remain restricted to the default workspace. Their values are never printed.
