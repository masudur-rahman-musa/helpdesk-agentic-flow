"""The MCP tools themselves.

A tool is a plain async function over a bound session, the caller's
`Identity` and a typed input. The registry's handler signature leaves no
place to pass a token, a permission or an audit writer, so those checks
stay in one shared pipeline instead of in each tool.
"""
