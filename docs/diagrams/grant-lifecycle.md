# Grant lifecycle

Every grant is time-bound. There is no state in which access simply persists.

```mermaid
stateDiagram-v2
    [*] --> active: execute_grant, expiry set at grant time
    active --> warned: expiry inside the warning horizon
    warned --> expired: the scheduler sweep passes the expiry
    active --> expired: the scheduler sweep passes the expiry
    active --> revoked: revoke_access, or the leaver sweep
    warned --> revoked: revoke_access, or the leaver sweep
    active --> rolled_back: rollback_request undoes the grant
    expired --> [*]
    revoked --> [*]
    rolled_back --> [*]

    note right of active
        The class cap bounds the expiry.
        A request for longer is capped,
        and the capping is a cited reason.
    end note

    note right of expired
        Expiry is a sweep, not a trigger:
        a lapsed grant is expired by the
        scheduler and notifies its holder
        and the department manager.
    end note
```

`warned` is not a separate status in the database; it is an `active` grant
that has had its warning notification sent. It is drawn because operationally
it is the state a holder notices.
