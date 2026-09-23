"""Portable HTTP client for the reports-fetcher v1.0.3 API.

Depends only on the Python standard library. The same code works against the
local mock (``REPORTS_API_BASE_URL=http://127.0.0.1:18765``) and against a real
deployment reachable through an SSH tunnel or a configured gateway.
"""
