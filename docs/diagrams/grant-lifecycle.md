# Grant lifecycle

Every grant has an end date. There is no state in which access simply stays.

```mermaid
stateDiagram-v2
    [*] --> active: execute_grant, expiry set at grant time
    active --> warned: expiry within 14 days
    warned --> expired: the expiry sweep passes the end date
    active --> expired: the expiry sweep passes the end date
    active --> revoked: revoke_access, offboarding, a review, or rollback_request
    warned --> revoked: revoke_access, offboarding, a review, or rollback_request
    expired --> [*]
    revoked --> [*]

    note right of active
        The class cap bounds the expiry.
        A request for longer is capped,
        and the cap is a cited reason.
    end note

    note right of expired
        Expiry is a sweep, not a trigger.
        The scheduler ends a lapsed grant
        every five minutes and tells the
        holder and their manager.
    end note
```

A grant row has three statuses, `active`, `expired` and `revoked`. `warned` is
not one of them. It is an `active` grant whose holder has been sent the
expiry warning. It is drawn because it is the state a holder notices.

Expiry is not a revocation. Nobody decided it, so the row keeps `revoked_at`
empty and only its status moves. A rollback by an IT admin, allowed within 24
hours of the grant, revokes the grant and marks the request `rolled_back`.
