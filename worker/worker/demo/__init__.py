"""DEMO MODE ONLY.

This package fabricates a *synthetic* dataset so the full pipeline (coverage → extraction →
replay → stats → backtest → report → UI) can be exercised end-to-end without network access
or credentials. Everything it writes is tagged with an unmistakable marker:

  * price bars use `source = 'synthetic'` (never mixed with real sources)
  * its reports carry a "SYNTHETIC DEMO DATA — NOT RESEARCH RESULTS" banner

Real conclusions can only come from the real ingestion paths. `worker/demo/guard.py`
enforces the marker when reading.
"""
