# Approval lifecycle

What happens to a request the policy engine sends for approval. The chat turn
does not wait. It ends with a reply that says who must approve, and the people
involved hear about each step through in-app notifications.

```mermaid
sequenceDiagram
    actor Employee
    participant Turn as Chat turn
    participant Tools as MCP tools
    participant DB as PostgreSQL
    actor Approver

    Employee->>Turn: says yes to the proposed request
    Turn->>Tools: create_access_request
    Tools->>DB: request row with its approver seats, audit event
    Tools->>DB: notify each approver
    Tools-->>Turn: needs approval, and from whom
    Turn-->>Employee: reply naming the approvers

    Note over DB,Approver: hours or days pass

    Approver->>Tools: approve_request or reject_request, from the Approvals page
    Tools->>DB: lock the request row, fill this approver's seat
    alt the last seat is approved
        Tools->>DB: execute_grant in the same transaction
        Tools->>DB: audit event, notify the employee
    else a seat rejects
        Tools->>DB: mark the request rejected, audit event, notify the employee
    else seats are still open
        Tools->>DB: audit event, notify about the progress
    end
    Tools-->>Approver: who is still left, or the outcome
```

Three rules hold for every chain.

The grant is made by the approval tool, in the same transaction as the last
filled seat. No chat turn runs it, so a lost conversation cannot cost an
approved request its access, or grant it twice.

Nobody fills a seat on a request they made or are the target of. One person
fills at most one seat per chain, even when they hold two of the principals
the chain asks for.

Both decisions lock the request row first. Two approvers acting on the last
seat at the same moment cannot both grant.
