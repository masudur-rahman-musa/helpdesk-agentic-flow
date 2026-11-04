# Use it for your company

Five things to change to run this for your own organization. The first four
are settings or data. The fifth is a directory adapter, one class you write.

## Policies

Your policy documents live under one folder, laid out as `{tenant}/{version}/{file}`.
Set `POLICY_DIR` to that folder and ingest it.

```bash
POLICY_DIR=/srv/company-policies make ingest TENANT=your-tenant-slug VERSION=v1
```

You can also put the `POLICY_DIR` line in `backend/.env.demo`. The ingest runs
from `backend/`, so a relative path is read from there. The default is the demo
folder `backend/data/policies/`, which is `POLICY_DIR=data/policies`.

The ingest refuses a folder with no `{tenant}/{version}` inside it, so a typo in
the path fails where you can see it. Each document is redacted through the
model gateway, so the gateway must be running. Running it again over an
unchanged file writes nothing.

## Directory adapter

Out of the box, the directory is a set of seeded tables. To use Entra ID, Okta,
SCIM or LDAP, you write one class. The policy engine, the tools, the approvals
and the audit log stay as they are. The interface and the working example both
live in `backend/helpdesk/shared/access/directory.py`.

### The seven methods

`DirectoryReader` holds the three read methods. `DirectoryProvider` adds the
four that change things. Every method is `async`.

- `lookup_user(user_id)` returns the user, or `None`.
- `list_membership_role_ids(user_id)` returns the roles of the user's department template. An empty set is a valid answer.
- `list_account_role_ids(user_id, now)` returns the roles the user really holds right now.
- `list_department_role_ids(department_id)` asks the same template question by department. Onboarding needs it before the new person exists.
- `create_user(...)` adds one person to the directory.
- `apply_grant(...)` gives one user one role.
- `revoke_grant(grant, reason, revoked_at)` takes one grant away.

No method decides anything. By the time `apply_grant` or `revoke_grant` runs,
the policy engine has already decided and the approvers have said yes. An
adapter that adds its own rules is making decisions outside the audit log.
`revoke_grant` takes one grant on purpose. Removing every grant of a role would
take away access that nothing recorded.

### The working example

`SimulatedDirectory`, in the same file, implements all seven methods against
the seeded tables. Each method is a few lines. Read it before writing your own.

### What to plan for with a real directory

- **Slow or failing calls.** Give the client a connect and read timeout. A directory that hangs must fail the turn, not hold it open.
- **Delayed reads.** A new grant may not show up in the next read for some seconds. Return the grant your write produced instead of reading it back.
- **Mapping groups to roles.** Keep the mapping in one place. Fail loudly on an unknown group. A smaller set of roles than the truth makes the system grant too much.

## Role templates

A role template says which roles a department's members should hold. Onboarding
uses it to set a new person's starting access. Access reviews use it to flag
access no department explains. Templates are rows, not code. Seed them for your
departments, or let your adapter answer `list_department_role_ids`.

## SoD rules

Segregation-of-duties rules name pairs of roles one person may not hold at once.
They are data, stored per tenant. The demo rules are written by
`backend/scripts/seed.py`. Adding a rule changes no code. A request that breaks
one is refused with the rule named.

## Branding

The Tailwind theme is in `web/tailwind.config.ts`. The product name appears in
`web/src/app/layout.tsx` and `web/src/components/layout/app-shell.tsx`. There is
no separate build step for branding.
