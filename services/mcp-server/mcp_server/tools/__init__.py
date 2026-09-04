"""The MCP tools themselves.

A tool is a plain async function over a bound session, the caller's
`Identity` and a typed input. What keeps it that way is the signature the
registry requires (`ToolSpec.handler`): a tool that wanted a token, a
session factory, a permission or an audit writer could not be registered,
because there is nowhere in the signature to pass one. Everything a tool
does not do is the pipeline's, so the guarantees are properties of one seam
rather than of each tool.
"""
