# Security and Trust

Start with the repository's [security policy](../../SECURITY.md) for disclosure and supported-version guidance. The documents below are **technical references inherited from Odysseus**; specific findings may be historical and should be verified against current Mnerys code and deployment settings.

- [Threat model](THREAT_MODEL.md) — trust boundaries and areas requiring an updated Mnerys review.
- [Native Python containment](native-python-containment.md) — Bubblewrap interpreter paths, read-only mounts and AppArmor test prerequisites.
- [Outbound HTTPS trust](tls-trust.md) — transport and certificate handling notes.
- [SearXNG trust validation](searxng-trust-image.md) — verification of upstream image material.

Never publish private hostnames, tailnet addresses, connection strings, internal topology or real user content in these documents.
