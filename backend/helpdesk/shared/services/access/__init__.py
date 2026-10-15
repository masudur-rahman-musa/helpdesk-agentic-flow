"""Access domain: the directory port, the snapshot builder, and execution.

This package carries no re-exports: import each name from the module that
defines it. The halves in `helpdesk.shared.access` and these modules import
each other, so an `__init__` that loaded them all made an import cycle.
"""
