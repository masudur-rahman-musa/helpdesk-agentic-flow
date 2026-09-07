"""The agent turn as a LangGraph `StateGraph` (the design).

Deliberately re-exports nothing. `build.py` is the only module that imports
LangGraph, and importing it from here would put the framework behind every
`agent_api.graph` import — including the two pure modules whose whole point
is that they can be tested with no graph at all (the design).
"""
