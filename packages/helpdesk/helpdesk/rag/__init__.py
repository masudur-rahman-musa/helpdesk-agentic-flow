"""Policy RAG: the corpus store and the structural split (the design).

Deliberately empty of imports. `models/policy.py` reads its column widths
from `rag.naming`, and `rag/store.py` imports the models; re-exporting
`store` here would close that into an import cycle.
"""
