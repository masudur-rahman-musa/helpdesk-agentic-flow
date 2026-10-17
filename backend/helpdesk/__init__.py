"""The one backend package: the shared library plus three apps.

`helpdesk.agent_api`, `helpdesk.mcp_server` and `helpdesk.scheduler` import
only `helpdesk.shared`, never each other (backend/.importlinter checks this).
"""
