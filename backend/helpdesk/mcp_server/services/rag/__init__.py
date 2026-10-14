"""Policy RAG ingest and retrieval, used only by the MCP server.

Deliberately empty of imports, like the old `shared.rag`: `vectors`, `store`,
`chunking` and the rest import each other and `helpdesk.shared.rag.naming`, so
re-exporting them here would make an import cycle. Import the modules directly.
"""
