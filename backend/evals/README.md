# project-evals (AI layer)

Project evals measure the PRODUCT'S behaviour (model-driven features), unlike
coding evals which judge the coding agent's output. The discipline, distilled
from the portfolio projects:

1. **Conversation cases** (`conversations/cases.jsonl`): 45 chosen multi-turn
   cases, each graded by where the world ended up and by a few checks of the
   replies. A chat failure found later becomes one of them or replaces one;
   the count stays 45.
2. **Deterministic checks first** (the world after the conversation, read
   from the database); **LLM-as-judge only for the reply checks**, and only
   after calibration against human labels, which the companion console keeps.
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
(answers 204, clears nothing) and `POST /v1/eval/conversation` (one turn in,
the reply and the world before and after out). Every step's prompt version is in the
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

`conversations/cases.jsonl` holds 45 multi-turn cases, `case-001` to
`case-045`, one JSON line each. The help desk scores nothing: the console plays
a simulated person through `POST /v1/eval/conversation` and grades the result.

Each case has these fields:

- `id`, `purpose` (one sentence), `owner` (`access`, `accounts`, `policy` or
  `safety`) and `tags`: one or more `job:<chat tool name>` and one or more
  kinds of variety (`everyday_words`, `typos`, `short_answers`, `follow_ups`,
  `changes_of_mind`, `for_others`, `other_languages`, `off_topic`, `attacks`,
  `live_failures`, `tricky_phrasing`).
- `scripted`: true only where the exact words matter (attacks, other
  languages, tricky phrasing), so the simulated person sends them as written.
- `person`: `seat`, `role`, `tenant` and `language`. The seat is
  `seed:<department lowercased>.<principal>.<index>`, a place in the seeded
  directory: department by name, index within the department, and the seat
  must hold the named principal.
- `world`: the facts the right answer depends on, all true on a fresh seed.
- `goal`, `opening` (the first message), `person_knows` (what the simulated
  person may answer when asked) and `confirms_if` (when it says yes).
- `right_end_states`: one or more lists of facts; the world after must match
  one of them. `never`: rules only this case has.
- `reply_checks`: each `{check, applies_when}`, the check one of
  `asks_before_change`, `reports_from_tool`, `cites_policy`,
  `asks_only_what_is_missing`, `refuses_safely`, `same_language`,
  `stays_on_topic`, `honest_about_failure`.
- `limits`: `max_turns` (1 to 10) and `max_tool_calls` (1 to 20).

A fact is one key: `holds`, `lacks`, `open_request` or `no_open_request` (a
role, `<system>.<role>`), `open_ticket` or `no_open_ticket` (a ticket kind),
`in_department`; an end state may also use `is_leaver`, `user_exists` (an
email), `reset_asked`, `unlocked`, `open_review` (a department) and
`nothing_changed`. A fact may add `person`, `<department lowercased>.<index>`,
to speak of someone other than the case's person. The seed plants a few
pending requests so "already asked" is true on a fresh seed.

Where the right answer depends on the person, sibling cases share the opening
with a different world: does not hold the role, already holds it, already
asked for it.

The route plays one turn per call. The first call sends `chat_id` null; the
answer gives the chat id to send with every later turn, and a chat the seat
did not start is refused. Each answer carries the reply, the turn's tool calls
with their results (a role id written back as `<system>.<role>`), and the case
tenant's requests, grants, tickets, people and reviews before and after the
turn, with people named as in the facts.

Cases share one database, so restore it before a run.
