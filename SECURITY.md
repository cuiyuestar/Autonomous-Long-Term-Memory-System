# Security Policy

## Supported Versions

Security fixes are applied to the latest released minor version.

## Reporting a Vulnerability

Do not open a public issue for an unpatched vulnerability. Use GitHub's private
security advisory flow for this repository and include:

- affected version or commit;
- reproduction steps;
- impact and data exposure;
- suggested mitigation, when available.

## Security Boundaries

- The default MCP `runtime` profile is intended for trusted local stdio use.
- The `admin` profile exposes governance, rollback, and maintenance operations
  and must not be exposed to untrusted callers.
- API keys are read from environment variables and must never be committed.
- Memory content may contain sensitive user data. Deployments must define
  retention, deletion, backup, and access-control policies before serving
  multiple users.
- Semantic model failures are fail-closed for destructive governance actions.

Remote HTTP deployment is not considered secure until authentication,
authorization, TLS termination, and request limits are configured.
