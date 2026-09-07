"""Policy RAG: the corpus store, the structural split, and the chunk pipeline
(the design).

Deliberately empty of imports. `models/policy.py` reads its column widths
from `rag.naming`, and `rag/store.py` imports the models; re-exporting
`store` here would close that into an import cycle. `gateway`, `sentences`,
`embedding` and `chunking` are left out for the same reason rather than a
different one: a package whose `__init__` imports half its modules makes the
cycle depend on which half.

Import the modules directly: `from helpdesk.rag.chunking import chunk_document`.
"""
