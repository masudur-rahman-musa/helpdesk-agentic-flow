# project-evals (AI layer)

Project evals measure the PRODUCT'S behaviour (model-driven features), unlike
coding evals which judge the coding agent's output. The discipline, distilled
from the portfolio projects:

1. **Conversation cases** (`conversations/cases.jsonl`): 45 chosen multi-turn
   cases with expected tool calls and graded replies. A chat failure found
   later becomes one of them or replaces one; the count stays 45.
2. **Deterministic checks first** (exact tool calls, the turn that says yes);
   **LLM-as-judge only for free text**, and only after calibration against
   human labels, which the companion console keeps.
3. **Baseline**: the companion console's `backend/evals/baseline.json` is the
   only baseline. This repo keeps the cases and holds no baseline file.
   It is never edited to make a run pass.
4. **Failure taxonomy**: every failed case gets a tag (wrong_output, wrong_tool,
   missed_refusal, false_refusal, injection_followed, timeout, judge_low, ADAPT).
   Read 50 failures by hand before changing anything.
5. **Run**: `make eval-chat` plays the cases and the console scores them. It
   needs the real gateway and costs money, so it is a manual run and not part
   of `make verify`.

## Running it

`make eval-chat` needs the stack up (`docker compose up -d`) with the agent-api
started with `EVAL_ROUTES_ENABLED=true`, the real gateway, and the console
checked out beside this repo (`CONSOLE_DIR`, default `../llmops-console`).
It restores the database with `backend/scripts/e2e_reset.py` (which also ingests
version v1 of each tenant's policies from `backend/data/policies`, redacted through
the real gateway) and the seed, then calls the console's `ops-eval conversations` against `$(AGENT_API_URL)`.

The agent-api answers the console on two routes that exist only while
`EVAL_ROUTES_ENABLED` is true (off in production): `POST /admin/test/reset`
(answers 204, clears nothing) and `POST /v1/eval/conversation` (one case in,
one record per turn out). Every step's prompt version is in the
`X-Prompt-Versions` response header.

## Speed numbers

`make agent-speed LABEL=before` (or `after`) runs the load smoke test, then reads the
spans the run filed and saves p50 and p95 for a whole turn, for each model step and for
each tool call in `agent-speed.json`, with the date and commit. The other label is kept.
It needs the real gateway and costs money, and it is meant for a quiet stack.

By default the spans come from Langfuse (`LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`,
`LANGFUSE_HOST`). With `SPANS=local` they come from the agent's own span log
(`SPAN_LOG_FILE`), which needs no Langfuse. The load smoke waits about twelve seconds
between asks, so one session stays under the chat's message limit; the gateway limits
for the tenant must allow the load too.

Saving `after` prints a before and after table when a `before` entry exists. The last
pair, both runs 20 users for 5 minutes on `agent-default`, spans from the local log:

- `before`: 2026-10-31, commit `0c768b3`, 343 turns.
- `after`: 2026-10-31, commit `c08b4f2`, 337 turns.

```
step            p50 before  p50 after  p50 change  p95 before  p95 after  p95 change
turn            5617        5199       -7.4%       9469        9196       -2.9%
model_step      2019        2161       7.1%        3589        3470       -3.3%
search_policy   102         112        9.1%        223         298        34.0%
list_my_access  12          104        803.8%      33          317        849.8%
get_me          15          85         465.7%      20          154        676.2%
```

Times are in milliseconds; a negative change means faster. A whole turn got about 7%
faster at the middle. Each lookup on its own got slower, because the lookups of one step
now run at the same time and share the database and the search store; what counts is
that a step waits for the slowest one instead of all of them in a row.

## The cut-offs

Two numbers decide a turn. Each is a setting, so it can be tuned against the
conversation cases, and the value that is kept is recorded here with its numbers.
Neither has been tuned yet: both are still the starting values, picked by hand.

| Setting | Default | What it decides |
|---|---|---|
| `ROLE_SUGGESTION_FLOOR` | 0.70 | a suggested role must score at least this |
| `POLICY_REFUSAL_FLOOR` | 0.35 | a policy answer below this is refused |

| Setting | Value | Date | Run commit | Decision accuracy | Refusal recall |
|---|---|---|---|---|---|
| `ROLE_SUGGESTION_FLOOR` | 0.70 | not tuned yet | not tuned yet | not tuned yet | not tuned yet |
| `POLICY_REFUSAL_FLOOR` | 0.35 | not tuned yet | not tuned yet | not tuned yet | not tuned yet |

## Conversation cases

`conversations/cases.jsonl` holds 45 multi-turn cases, one JSON line each: the
person's messages (`turns`), the tool calls the whole conversation must make
(`expected.tool_calls`), the turn that says yes (`expected.confirm_turn`, `null`
when nothing may change) and 1 to 3 replies to grade (`graded`, each with a pass
or fail question). There is no larger set. The help desk scores nothing: the
console's eval engine plays each case through `POST /v1/eval/conversation`,
which answers every turn's reply, tool calls and results.

- `principal_user` is `seed:<department lowercased>.<principal>.<index>`, a
  coordinate into the seeded directory rather than an address: department by
  name, index within the department, and the seat must hold the named
  principal.
- A tool call's `args_subset` is symbolic. An access request names its role as
  `<system slug>.<role slug>`, which the route writes back in place of the role id.

Cases run in file order and share one database, so an approval case relies on
the request an earlier case filed. Run the whole file, not one case.
