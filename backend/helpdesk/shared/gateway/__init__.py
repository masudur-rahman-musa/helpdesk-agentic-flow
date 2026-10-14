"""The client side of the Ops Console gateway.

Everything this repo sends to the gateway's chat endpoint is built here, and
nothing else in the repo opens that socket. `helpdesk.rag.gateway` remains the
redaction client on the same host: a different endpoint, a different data
class and a different failure story, sharing only the per-tenant key lookup
this package owns in `keys.py`.
"""
