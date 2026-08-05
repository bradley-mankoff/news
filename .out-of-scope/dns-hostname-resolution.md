# Dns Hostname Resolution

Deliberately out of scope for #134: the shipped fix uses a deterministic static alias map (RFC 6761 special-use treatment), and DNS resolution was rejected by design because it would make the comparison blocking and network-dependent. Boundary documented in the `_LOOPBACK_HOST_ALIASES` comment in `news_pipeline/config.py`.

## Prior requests

- #134 — "Normalize host aliases (localhost vs 127.0.0.1) in managed-model endpoint comparison"
