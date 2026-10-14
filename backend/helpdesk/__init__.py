"""The one backend package: shared library plus the three apps.

`helpdesk.shared` is the shared library; `helpdesk.agent_api`,
`helpdesk.mcp_server` and `helpdesk.scheduler` are the three apps of the design.
The apps import only `helpdesk.shared`, never each other (backend/.importlinter).
"""
