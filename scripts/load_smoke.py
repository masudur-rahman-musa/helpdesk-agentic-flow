"""The load smoke: 20 concurrent chat sessions for five minutes.

A locustfile, run by `make load-smoke`. It answers one question - what a chat
turn costs under a load one demo machine can actually produce - and it answers
it against the REAL gateway under the `agent-default` alias (decision D). A p95
measured against `scripts/e2e_gateway_stub.py` would measure this repo's own
overhead and nothing a reader would care about, so this target and `make e2e`
deliberately do not share a model.

Manual, and never in CI. It needs the compose stack up, agent-api running and a
gateway key that costs money, and its number belongs in the README beside the
machine it was measured on rather than in a pass/fail gate that would go red on
somebody else's laptop.

locust rather than k6 (decision B). k6 is the better load tool and is a
Homebrew binary, which a project rule turns into an ask on every machine that
ever runs this repo; locust rides the lockfile that is already there.

Every user signs in as a seeded demo account and sends the same short ask
`POST /chat`, not `/chat/stream`: the streaming route's duration is dominated
by how fast the model emits tokens, while the non-streaming one measures the
turn - the graph, the retrieval, the policy engine, the tool calls and the
writes - which is the part this repo owns.

p50 and p95 are printed per chat turn at the end of the run by
`_print_percentiles`, off locust's own statistics rather than a second
measurement of the same requests.
"""

import random
from typing import Any

from locust import HttpUser, between, events, task

from scripts.e2e_accounts import accounts

CHAT_ENDPOINT = "/chat"
LOGIN_ENDPOINT = "/auth/login"

_ACCOUNTS = list(accounts().values())
"""Derived once at import, not per simulated user: `accounts()` generates the
whole tenant, and doing that twenty times is load on this process rather than
on the thing being measured."""

TURN_NAME = "POST /chat (one turn)"
"""The one statistic this script exists to report, named so the percentile
print and the request itself cannot drift apart."""

ASKS = (
    "What does the access policy say about admin rights?",
    "Which systems am I allowed to ask for?",
    "How long does elevated access last?",
    "What happens when my access expires?",
)
"""Read-only asks. A load test that filed a request per turn for five minutes
would leave thousands of rows and an approval queue nobody asked for, and it
would measure the write path rather than the turn."""


class ChatUser(HttpUser):
    """One signed-in person sending one ask at a time."""

    wait_time = between(1, 3)

    def on_start(self) -> None:
        """Sign in once per simulated user and keep the bearer token."""
        principal = random.choice(_ACCOUNTS)
        response = self.client.post(
            LOGIN_ENDPOINT,
            json={
                "tenant_slug": principal["tenantSlug"],
                "email": principal["email"],
                "password": principal["password"],
            },
            name="POST /auth/login",
        )
        response.raise_for_status()
        token = response.json()["access_token"]
        self.client.headers["Authorization"] = f"Bearer {token}"

    @task
    def one_turn(self) -> None:
        self.client.post(
            CHAT_ENDPOINT,
            json={"message": random.choice(ASKS)},
            name=TURN_NAME,
        )


@events.quitting.add_listener
def _print_percentiles(environment: Any, **_kwargs: Any) -> None:
    """p50 and p95 for the chat turn, off locust's own statistics.

    Read from the same entry the requests were recorded under rather than
    measured again: two measurements of one thing is how a report ends up
    disagreeing with the tool that produced it.
    """
    entry = environment.stats.get(TURN_NAME, "POST")
    if entry is None or entry.num_requests == 0:
        print("load smoke: no chat turns completed")
        return
    print(
        f"load smoke: {entry.num_requests} turns, {entry.num_failures} failed, "
        f"p50 {entry.get_response_time_percentile(0.5)} ms, "
        f"p95 {entry.get_response_time_percentile(0.95)} ms"
    )
