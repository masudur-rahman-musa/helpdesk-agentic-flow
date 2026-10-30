# Customizing this system

Five things to change to run this against your own organization. None of them
means editing the code this project owns. The first four are settings or
data. The fifth is a directory adapter written against a published interface.

## Policies

The corpus lives under one root folder, laid out as `{tenant}/{version}/{file}`.
Set `POLICY_DIR` to your own folder and ingest it:

```bash
POLICY_DIR=/srv/company-policies make ingest TENANT=your-tenant-slug VERSION=v1
```

You can also put the `POLICY_DIR` line in `backend/.env.demo`. The ingest runs
from `backend/`, so a relative path is read from there. The shipped demo corpus
is `backend/data/policies/`, which is `POLICY_DIR=data/policies`. That is also the default.

An explicit `--corpus-root` on the ingest command
(`uv run python -m helpdesk.mcp_server.cli`) wins over the setting. That helps
for a one-off ingest from a scratch folder.

The ingest refuses a root with no `{tenant}/{version}` folder rather than
reporting a successful run over zero documents. So a typo in the path fails
where you can see it.

Each document is redacted through the model gateway, chunked and embedded.
The gateway must be running for this. Running the command again over an
unchanged file reports it unchanged and writes nothing. So adding one policy
costs one document, not the whole corpus.

## Directory adapter

Out of the box, the directory is a set of seeded tables. To read from and
write to a real identity provider, implement the seven-method
`DirectoryProvider` port. See [directory-adapter.md](directory-adapter.md).

## Role templates

A role template says which roles a department's members are expected to hold.
Onboarding reads them to set a joiner's starting access. The periodic review
reads them to flag access that nobody's department explains.

Templates are rows, not code. Seed them for your own departments, or let your
directory adapter answer `list_department_role_ids` from the identity provider
instead. Nothing in the policy engine names a department.

## SoD rules

Segregation-of-duties rules name pairs of roles one person may not hold at
once. They are data too, stored per tenant, and the Rules page lists the ones
in force, read only. The demo rules are written by `backend/scripts/seed.py`.

The engine that checks them is pure and table-driven. Adding a rule changes no
code path. A request that would break one is refused with the rule named in
the decision, not blocked with a generic error.

## Branding

The web app carries no logo or colour that is hard to find. The Tailwind theme
is in `web/tailwind.config.ts`. The product name appears in
`web/src/app/layout.tsx` and `web/src/components/layout/app-shell.tsx`. There
is no white-label build step, because nothing is generated that would need
rebuilding.
