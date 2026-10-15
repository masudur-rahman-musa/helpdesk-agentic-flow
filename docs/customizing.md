# Customizing this system

Five things an adopter changes to run this against their own organization.
Nothing below requires editing code that this project owns; the first four are
configuration or data, and the fifth is a directory adapter written against a
published interface.

## Policies

The corpus lives under one root, laid out as `{tenant}/{version}/{file}`. Set
`POLICY_DIR` to your own folder and re-ingest:

```
POLICY_DIR=/srv/company-policies
make ingest TENANT=your-tenant-slug VERSION=v1
```

The default is `policies/`, the demo corpus shipped with the repository. An
explicit `--corpus-root` on the ingest CLI (`python -m helpdesk.mcp_server.cli`) still wins over the variable,
which is what you want for a one-off ingest from a scratch directory.

Ingestion refuses a root with no `{tenant}/{version}` directory rather than
reporting a successful run over zero documents, so a typo in the path fails
where you can see it.

Each document is chunked, redacted through the gateway and embedded. Re-running
the command over an unchanged file reports it unchanged and writes nothing, so
a re-ingest after adding one policy costs one document, not the corpus.

## Directory adapter

Out of the box the directory is a set of seeded tables. To read from and write
to a real identity provider, implement the seven-method `DirectoryProvider`
port. See [docs/directory-adapter.md](docs/directory-adapter.md).

## Role templates

A role template says which roles a department's members are expected to hold.
The onboarding flow reads them to decide a joiner's starting access, and the
periodic review reads them to flag access nobody's department explains.

Templates are rows, not code: seed them for your own departments, or let your
directory adapter answer `list_department_role_ids` from the identity provider
instead. Nothing in the policy engine names a department.

## SoD rules

Segregation-of-duties rules name pairs of roles one person may not hold at
once. They are data as well, editable per tenant, and the admin console lists
the ones in force.

The engine that evaluates them is pure and table-driven: adding a rule changes
no code path, and a request that would violate one is refused with the rule
cited in the decision rather than blocked with a generic error.

## Branding

The web app carries no logo, product name or color that is hard to find. The
Tailwind theme and the shell layout live under `web/`, and the visible product
name is a single string. There is no white-label build step, because there is
nothing generated to rebuild.
