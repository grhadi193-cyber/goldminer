"""Database access layer. All reads that feed research code must go through
`worker.research.asof` so the anti-leakage rule cannot be bypassed by accident."""
