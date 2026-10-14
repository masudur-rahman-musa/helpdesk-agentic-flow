# Writing a directory adapter

Every read of directory state and every change to it goes through one
interface. Swapping the shipped simulation for Entra ID, Okta, a SCIM endpoint
or LDAP means writing one class, not touching the policy engine, the tools, the
approval flow or the audit chain.

Both the interface and the worked example live in
`backend/helpdesk/shared/services/access/directory.py`.

## The interface

The port is split in two. `DirectoryReader` is what a caller that only reads
may ask; `DirectoryProvider` extends it with everything that changes state, so
a read-only caller cannot be handed mutation methods by accident.

`DirectoryReader`:

- `lookup_user(user_id)` — the user with this id, or `None`.
- `list_membership_role_ids(user_id)` — the role ids of that user's department
  template. An empty set is a valid answer: a person outside every template is
  not an error.
- `list_account_role_ids(user_id, now)` — the role ids the user actually holds
  as of `now`. Live means active, not revoked, and not expired.

`DirectoryProvider` adds:

- `list_department_role_ids(department_id)` — the same template question asked
  by department rather than by user. Onboarding needs it before the joiner
  exists, so there is no user to ask about yet.
- `create_user(...)` — put one person into the directory. The caller has
  already refused a duplicate address and resolved the department; this only
  records the person.
- `apply_grant(...)` — give one user one role, live from `granted_at`. The
  decision to grant was taken before this method was called.
- `revoke_grant(grant, reason, revoked_at)` — take one grant away.

Two properties are worth stating plainly, because an adapter that gets them
wrong passes its own tests and fails in production:

**No method decides anything.** By the time `apply_grant` or `revoke_grant` is
called, the policy engine has classified the request, checked segregation of
duties, and either approved it or routed it to a human. An adapter that
consults its own rules, widens a revocation past the grant it was handed, or
skips a write it judges unnecessary is making policy decisions outside the
audited path.

**`revoke_grant` is singular on purpose.** Which grants a revocation covers is
decided by the caller. A real identity provider removes one assignment at a
time, and an adapter that removed a role's every assignment would take away
access nothing recorded.

## The worked example

`SimulatedDirectory`, in the same file, implements all seven methods against
the seeded tables. It is the v1 implementation and the reference: every method
is a few lines, and the liveness rule that `list_account_role_ids` applies is
written out there rather than described.

Read it before writing your own. In particular, note that it takes a session
whose queries are already filtered to the bound tenant, so a user id from
another tenant reads as absent rather than as a row.

## What a real adapter implements

For Entra ID or Okta, the mapping is direct:

- `lookup_user` — a user lookup by object id, mapped onto the project's `User`.
- `list_membership_role_ids` and `list_department_role_ids` — group or
  entitlement membership, with your provider's group ids mapped to role ids.
- `list_account_role_ids` — the user's current app role assignments, filtered
  to the ones still in effect.
- `create_user` — a user creation call, or a SCIM `POST /Users`.
- `apply_grant` and `revoke_grant` — one app role assignment created or
  removed.

Three things to plan for that the simulation does not have to:

**Latency and failure.** Every method is `async` and every call crosses the
network. Give the client an explicit connect and read timeout; a directory that
hangs must fail the turn, not hold it open.

**Eventual consistency.** A grant applied through a remote provider may not be
visible to the next read for seconds. Return the grant your write produced
rather than re-reading it.

**Identifier mapping.** The project speaks in its own role ids. Keep the
mapping from provider group to role id in one place, and fail loudly on an
unmapped group rather than silently returning a smaller set, which reads as
"this person holds less access than they do" and quietly widens what the
system will grant them.
