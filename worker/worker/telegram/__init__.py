"""Telegram ingestion — READ-ONLY (R7).

This package may read history and receive updates. It must never send, forward, edit,
react, join, or otherwise interact: `worker/tests/test_rules.py` greps the tree for the
Telethon write APIs and fails the build if any appear anywhere.
"""
