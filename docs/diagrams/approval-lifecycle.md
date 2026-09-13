# Approval lifecycle

What happens to a request the policy engine sends for approval, and to the
conversation turn waiting on it.

```mermaid
sequenceDiagram
    actor Employee
    participant Turn as Agent turn
    participant Tools as MCP tools
    participant DB as PostgreSQL
    actor Approver

    Employee->>Turn: asks for access
    Turn->>Tools: create_access_request
    Tools->>DB: request row plus approver chain
    Tools-->>Turn: needs approval, chain of seats
    Turn->>DB: checkpoint the paused turn
    Turn-->>Employee: a person is looking at it

    Note over DB,Approver: hours or days pass

    Approver->>Tools: approve_request
    Tools->>DB: fill the seat
    alt every seat approved
        Tools->>DB: execute the grant in the same transaction
        Tools->>DB: append granted to the audit chain
    else a seat rejects
        Tools->>DB: mark the request rejected
    end
    Tools-->>Turn: resume with the recorded decision
    Turn-->>Employee: reply composed from the decision on the row
```

Two properties the sequence is drawn to show.

The grant is made by the approval tool, in the same transaction as the last
filled seat. The resumed turn makes no tool call at all: it composes from the
decision recorded on the row, so a lost checkpoint cannot cost an approved
request its access.

A seat is filled by a person the segregation-of-duties rules allow to fill it.
One person cannot occupy two seats in the same chain.
