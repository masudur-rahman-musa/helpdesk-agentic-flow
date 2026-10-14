"""Shared domain logic grouped by the thing it is about.

Each subpackage is one area the apps lean on: `access` (requests, grants,
the directory port, lifecycle and reviews), `audit` (the hash-chained log's
chain, writer, verify and search halves), `killswitch` (the trip switches),
and `policy_docs` (the policy-corpus naming rules). Pure logic over the
tenant-scoped repository; the apps call in, nothing here imports an app.
"""
