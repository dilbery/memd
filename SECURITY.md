# Security policy

memd stores notes and issues access tokens, so security reports are taken seriously.

## Reporting a vulnerability

Please do not open a public issue for a security problem. Use GitHub's private
vulnerability reporting instead: open the repository's **Security** tab and choose
**Report a vulnerability**. Include the version or commit, what you did, and what you
saw. You will get an acknowledgement, and a fix or a mitigation note once the report
is confirmed.

## Scope

In scope: the memd server, its web consoles, the MCP and HTTP interfaces, the client
hooks and bridge in `clients/`, and the onboarding script.

Out of scope: weaknesses that need an already-compromised host, a stolen valid token,
or a deployment that exposes the port without the HTTPS reverse proxy the
documentation calls for.

## Supported versions

Only the latest commit on `main` is supported.
