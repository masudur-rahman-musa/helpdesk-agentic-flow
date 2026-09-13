# Request lifecycle

One conversation turn, from the employee's sentence to the reply they read.

```mermaid
flowchart TD
    msg([" Employee message "]) --> intent["Classify intent and entities"]
    intent --> retrieve["Hybrid retrieval over the policy corpus"]
    retrieve --> enough{"Best dense cosine<br/>at or above the threshold?"}
    enough -- no --> refuse["Refuse and offer a ticket"]
    enough -- yes --> plan["Plan tool calls"]
    plan --> guard{"RBAC, kill switch<br/>and idempotency key"}
    guard -- denied --> refuse
    guard -- allowed --> create["create_access_request"]
    create --> decide["Policy engine decides"]
    decide -- blocked --> blocked["Blocked, with the rule cited"]
    decide -- "needs approval" --> pause["Pause the turn, notify approvers"]
    decide -- allowed --> grant["execute_grant with an expiry"]
    grant --> audit["Append to the audit chain"]
    blocked --> audit
    pause --> audit
    audit --> compose["Compose the reply from tool results"]
    compose --> check{"Decision word matches<br/>what the tools returned?"}
    check -- no --> regen["Regenerate once, then fall back"]
    check -- yes --> reply([" Reply, streamed with its steps "])
    regen --> reply
    refuse --> reply
```

The decision diamond is the policy engine, not the model. The model reads the
sentence at the top and writes the words at the bottom; everything between the
two is deterministic, authorized and recorded.
