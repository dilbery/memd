# Plan 2, piece 2: OIDC resource server

Implements §9b of `the multi-tenant assessment (not published)`. memd becomes an OAuth
2.1 resource server; authentik is the authorisation server; the agent is the
client.

**This is the first and only production caller of `identity.bind_identity`.**
Piece 1 built a substrate where the store follows the authenticated caller and
no request field can move them off it, but nothing could authenticate, so
nothing was ever bound. This closes that.

`MEMD_OIDC_ISSUER` unset means the layer is off and memd authenticates exactly
as before, so taking this code changes no running deployment.

## Configuration

| Variable | Meaning |
|---|---|
| `MEMD_OIDC_ISSUER` | authentik's per-application issuer, `https://auth.example.com/application/o/memd/`. Setting it turns the layer on. |
| `MEMD_OIDC_JWKS_URI` | Optional. Defaults to `<issuer>/jwks/`. Explicit because deriving one URL from another is what breaks quietly on an upgrade. |
| `MEMD_OIDC_SCOPE` | The custom scope that acts as the audience check. Default `memd`. |
| `MEMD_PUBLIC_URL` | Already used; becomes the `resource` identifier and the base of the `WWW-Authenticate` hint. |

## What a request meets, in order

1. **Static token** (`check_bearer`). Checked first: a constant-time compare
   with no network, against a JWT path that may fetch a JWKS. The sync job runs
   every five minutes and must not fetch a JWKS each time. A static token binds
   **no identity**, so it stays subject to the instance lock.
2. **OIDC bearer** (`oidc.bearer_identity`), which binds the caller's store.

Both transports use the same helper in the same order. Two auth implementations
that drift is how a caller ends up with more access over one transport than the
other.

## Validation, in the order an attacker meets it

1. **Signature**, RS256/384/512 against authentik's JWKS. The algorithm the
   token asks for is never used; the allowlist decides, which is what closes
   `alg: none` and the RS256-to-HS256 confusion.
2. **Registered claims**: `iss`, `exp`, `require=["exp","iss"]`, ten seconds of
   clock-skew leeway. Small on purpose: a generous leeway quietly extends the
   life of a revoked session.
3. **Scope**, the practical audience check (§9b item 3). Split on whitespace,
   never substring-matched, so `memdxyz` never satisfies `memd`.
4. **Identity**, the lower-cased `email` claim through
   `stores.store_name`, so an email that is not a usable single path segment is
   refused rather than sanitised into somebody else's store.

Only then is the store bound. A raise at any point leaves nothing bound.

### Why scope and not audience

The spec wants RFC 8707 `resource` honoured as `aud`. authentik's behaviour
there is unverified, and under per-client registration every client has its own
`client_id`, so `aud` cannot identify this resource. A token authentik minted
for another application is signed by the same key and carries the same
issuer; the custom scope is what separates them. Revisit if authentik's
`resource` handling is ever confirmed.

## Discovery

- `GET /.well-known/oauth-protected-resource` (RFC 9728), **unauthenticated by
  necessity**: a client that cannot yet authenticate has to read it. It carries
  no secret, only the issuer URL.
- Every 401 carries `WWW-Authenticate: Bearer resource_metadata="..."` (spec
  MUST). A 401 without it leaves the client with nowhere to go.
- The document **omits `registration_endpoint`**. Claude Code fails validation
  on a null value and attempts DCR on a present one.

## Dynamic client registration: settled by observation

§9b listed "verify authentik's authorisation-server metadata advertises the
`registration_endpoint`" as open. **Checked against a running authentik
server on 2026-09-14: it does not.** `/application/o/<slug>/.well-known/openid-configuration`
has no `registration_endpoint` key at all, and
`.well-known/oauth-authorization-server` returns 404.

Consequences:

- **A reverse-proxy registration broker cannot work on its own.** Injecting a token on
  the registration path does nothing if no client ever discovers the endpoint.
  It would need authentik to advertise it first.
- **Pre-registered public clients work with no further changes**, because a
  client that finds no `registration_endpoint` falls back to its configured
  client id. Codex supports `--oauth-client-id`, omp carries `oauthClientId`,
  Claude Code supports `--client-id`.

So the design is pre-registered public clients, one per agent type, loopback
redirects, PKCE, no secret, and no service token anywhere.

## What is still needed on the authentik side

Not code, and not done here:

1. An application and OAuth2 provider `memd`, issuer
   `https://auth.example.com/application/o/memd/`, with a signing key set so
   access tokens are signed JWTs.
2. A custom scope mapping `memd`, added to the provider's property mappings.
3. Access token lifetime 10 minutes, refresh 30 days (§9b).
4. One public client per agent type with loopback redirect URIs and PKCE.
5. The implicit-consent authorisation flow, so the handshake is silent for a
   signed-in user.

## Still not built

- **Trusted-proxy mode** for Open WebUI (§9b item 5): memd would accept
  `X-OpenWebUI-User-Email` only when the bearer is one specific service token.
  Deliberately deferred: it is the one path where memd takes an identity from a
  header, and it should be built when Open WebUI's memory story is actually
  being wired, not speculatively.
- **Token revocation checking.** Access tokens are short and refresh fails once
  the upstream directory sync deactivates the user, which is §9b's stated model. There is no
  introspection call on the hot path and should not be.

## Deployment trap found on the day: TLS

memd fetches the JWKS with **httpx, which verifies against `certifi`, not the
system trust store.** When the identity provider is served with a certificate from an internal
root CA (for example one issued by step-ca), the container has to be told about it.

Bind-mounting the host bundle at `/etc/ssl/certs/ca-certificates.crt` is
necessary but **not sufficient**, because certifi's own bundle is what httpx
reads. `SSL_CERT_FILE` (and `REQUESTS_CA_BUNDLE` for anything using requests) is
what actually makes verification succeed.

Without it every token is rejected, and it presents as "OIDC does not work"
rather than as a TLS failure. Verified from inside the built image before the
first deploy:

    JWKS unavailable ... CERTIFICATE_VERIFY_FAILED     # bundle mounted only
    JWKS keys: ['<kid>...']                            # with SSL_CERT_FILE

The deployed stack sets both variables and mounts the bundle read-only, a common
pattern for containers that must trust an internal CA.
