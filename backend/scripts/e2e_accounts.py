"""The seeded accounts the e2e flows sign in as, re-derived and printed as JSON.

No password string is written into a spec file, a fixture or
`.env.example`: the seed derives every demo password from a
fixed formula and every other user's from one filler constant, so this script
imports `scripts.seed` and asks it, rather than repeating a literal that would
drift the moment the formula changed.

They are seed data and not secrets. The whole corpus is obviously fake, and
the point of printing them is that Playwright's fixtures can read one file
instead of nine specs each knowing how the seed thinks.

Seven named principals, because the flows need people the demo accounts alone
do not cover:

* `employee` - the demo employee, Production index 5. Flows 1, 2 and 6.
* `employee_manager` - that employee's OWN department manager, Production
  index 0. This is also the demo `manager` account, so flow 2 signs in with
  its demo password; it is named by the department rule so the flow follows
  the chain's seat, not the demo pick.
* `gitlab_owner` - the owner of the `gitlab` system, the second seat in flow
  2's chain. The demo `system_owner` owns the production infrastructure
  system (`prod-k8s`) and owns no part of flow 2.
* `sod_employee` - Procurement index 4, one of the seed's `planted_conflicts`:
  it holds `payments.approver` and its department template does NOT contain
  `payments.initiator`, so asking for the other half of SoD-STD section 2.1 is
  a genuine new ask rather than a re-ask of access already held. Flows 3 and 4.
* `hr` - the HR officer, who offboards in flow 4.
* `it_admin` and `platform_admin` - the admin surfaces flows 3, 4, 5 and 6 read
  (`/admin/rules`, `/admin/audit`, `/admin/killswitch`).

The output is one JSON object keyed by those names, written to the path in
`E2E_ACCOUNTS_FILE` when it is set and to stdout otherwise, so `make e2e` can
produce the file before Playwright starts and a person can eyeball it with no
file left behind.
"""

import json
import os
import sys
from pathlib import Path
from typing import Any

from scripts.seed import (
    _FILLER_PASSWORD,
    NORDLICHT,
    TenantSpec,
    UserRow,
    generate_tenant,
)

TENANT = NORDLICHT
"""The e2e tenant. One of the two seeded ones; `cuan` exists so the isolation
tests have a neighbour, and the flows have no use for it."""

SOD_DEPARTMENT = "Procurement"
SOD_USER_INDEX = 4
"""The `planted_conflicts` entry flows 3 and 4 use.

Named here as the pair the seed declares rather than as an email, because the
email is Faker output and moves if the seed's version pin ever changes, while
the department and index are the seed's own coordinates.
"""

GITLAB_SYSTEM = "gitlab"
"""Flow 2's system: `medium` sensitivity, category `other`, so an `admin`
privilege on it is `PRIVILEGED` and its chain is exactly two seats."""


def _password(user: UserRow) -> str:
    """This user's login password: the demo formula, or the filler constant.

    Both come from `backend/scripts/seed.py`, never from a literal here: the seed
    writes the hash and this writes the plaintext, and two copies of one
    formula is exactly the pair that drifts.
    """
    return user.demo_password if user.demo_password is not None else _FILLER_PASSWORD


def _principal(spec: TenantSpec, user: UserRow) -> dict[str, Any]:
    return {
        "email": user.email,
        "password": _password(user),
        "tenantSlug": spec.slug,
        "displayName": user.display_name,
        "department": user.department,
        "userId": str(user.id),
    }


def accounts() -> dict[str, dict[str, Any]]:
    """The seven named principals, resolved out of the generated tenant."""
    rows = generate_tenant(TENANT)
    spec = rows.spec
    by_department = rows.users_by_department

    employee = by_department[spec.departments[1]][5]
    named = {
        "employee": employee,
        "employee_manager": by_department[employee.department][0],
        "gitlab_owner": rows.system_owners[GITLAB_SYSTEM],
        "sod_employee": by_department[SOD_DEPARTMENT][SOD_USER_INDEX],
        "hr": by_department[spec.hr_department][1],
        "it_admin": _first_with(rows.users, "it_admin"),
        "platform_admin": _first_with(rows.users, "platform_admin"),
    }
    return {name: _principal(spec, user) for name, user in named.items()}


def _first_with(users: list[UserRow], principal: str) -> UserRow:
    """The first user holding this principal, or a loud failure.

    Loud rather than a `None` the fixture would trip over three processes
    later: a seed that stopped assigning a principal is a broken run, not an
    account the flows can do without.
    """
    for user in users:
        if principal in user.principals:
            return user
    raise AssertionError(f"the seed assigned no {principal} in {TENANT.slug}")


def main() -> None:
    payload = json.dumps(accounts(), indent=2, sort_keys=True)
    destination = os.environ.get("E2E_ACCOUNTS_FILE")
    if destination:
        Path(destination).write_text(payload + "\n", encoding="utf-8")
    else:
        sys.stdout.write(payload + "\n")


if __name__ == "__main__":
    main()
