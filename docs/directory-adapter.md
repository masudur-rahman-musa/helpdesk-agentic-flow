# Writing a directory adapter

Every read of directory state and every change to it goes through one
interface. Swapping the shipped simulation for Entra ID, Okta, a SCIM endpoint
or LDAP means writing one class. The policy engine, the tools, the approval
flow and the audit chain stay as they are.

Both the interface and the worked example live in
`backend/helpdesk/shared/access/directory.py`.

## The interface

The port is split in two. `DirectoryReader` is what a caller that only reads
may ask. `DirectoryProvider` extends it with everything that changes state, so
a read-only caller cannot be handed write methods by accident. Every method is
`async`.

`DirectoryReader` has three methods.

- `lookup_user(user_id)` returns the user with this id, or `None`.
- `list_membership_role_ids(user_id)` returns the role ids of that user's department template. An empty set is a valid answer. A person outside every template is not an error.
- `list_account_role_ids(user_id, now)` returns the role ids the user really holds as of `now`. Live means active, not revoked and not expired.

`DirectoryProvider` adds four more.

- `list_department_role_ids(department_id)` asks the same template question by department rather than by user. Onboarding needs it before the joiner exists, so there is no user to ask about yet.
- `create_user(...)` puts one person into the directory. The caller has already refused a duplicate address and found the department. This method only records the person.
- `apply_grant(...)` gives one user one role, live from `granted_at`. The decision to grant was taken before this method was called.
- `revoke_grant(grant, reason, revoked_at)` takes one grant away.

Two properties matter, because an adapter that gets them wrong passes its own
tests and fails in production.

**No method decides anything.** By the time `apply_grant` or `revoke_grant` is
called, the policy engine has classified the request, checked segregation of
duties, and either approved it or sent it to a human. An adapter that applies
its own rules, widens a revocation past the grant it was handed, or skips a
write it thinks is not needed, is making policy decisions outside the audited
path.

**`revoke_grant` is singular on purpose.** The caller decides which grants a
revocation covers. A real identity provider removes one assignment at a time.
An adapter that removed every assignment of a role would take away access that
nothing recorded.

## The worked example

`SimulatedDirectory`, in the same file, implements all seven methods against
the seeded tables. It is the shipped implementation and the reference. Every
method is a few lines, and the liveness rule that `list_account_role_ids`
applies is written out there rather than described.

Read it before writing your own. Note that it takes a session whose queries
are already filtered to the bound tenant, so a user id from another tenant
reads as absent rather than as a row.

## What a real adapter implements

For Entra ID or Okta, the mapping is direct.

- `lookup_user` becomes a user lookup by object id, mapped onto the project's `User`.
- `list_membership_role_ids` and `list_department_role_ids` become group or entitlement membership, with your provider's group ids mapped to role ids.
- `list_account_role_ids` becomes the user's current app role assignments, filtered to the ones still in effect.
- `create_user` becomes a user creation call, or a SCIM `POST /Users`.
- `apply_grant` and `revoke_grant` each create or remove one app role assignment.

Three things to plan for that the simulation does not have to handle.

**Latency and failure.** Every call crosses the network. Give the client an
explicit connect and read timeout. A directory that hangs must fail the turn,
not hold it open.

**Eventual consistency.** A grant applied through a remote provider may not
show up in the next read for some seconds. Return the grant your write
produced rather than reading it back.

**Id mapping.** The project speaks in its own role ids. Keep the mapping from
provider group to role id in one place. Fail loudly on an unmapped group rather
than quietly returning a smaller set. A smaller set reads as "this person
holds less access than they do", which quietly widens what the system will
grant them.
