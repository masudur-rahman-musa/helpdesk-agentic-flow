# project-evals (AI layer)

Project evals measure the PRODUCT'S behaviour (model-driven features), unlike
coding evals which judge the coding agent's output. The discipline, distilled
from the portfolio projects:

1. **Conversation cases** (`conversations/cases.jsonl`): 33 chosen multi-turn
   cases with expected tool calls and graded replies. A chat failure found
   later becomes one of them or replaces one; the count stays 33.
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
checked out beside this repo (`CONSOLE_DIR`, default `../ai-operations-console`).
It restores the database with `dev/scripts/e2e_reset.py` and the seed, then
calls the console's `ops-eval conversations` against `$(AGENT_API_URL)`.

The agent-api answers the console on two routes that exist only while
`EVAL_ROUTES_ENABLED` is true (off in production): `POST /admin/test/reset`
(answers 204, clears nothing) and `POST /v1/eval/conversation` (one case in,
one record per turn out). Every step's prompt version is in the
`X-Prompt-Versions` response header.

## The cut-offs

Three numbers decide a turn. Each is a setting, so it can be tuned against the
conversation cases, and the value that is kept is recorded here with its numbers.

| Setting | Default | What it decides |
|---|---|---|
| `INTENT_CONFIDENCE_MIN` | 0.7 | below this the agent asks instead of acting |
| `ROLE_SUGGESTION_FLOOR` | 0.70 | a suggested role must score at least this |
| `POLICY_REFUSAL_FLOOR` | 0.35 | a policy answer below this is refused |

| Setting | Value | Date | Run commit | Decision accuracy | Refusal recall |
|---|---|---|---|---|---|
| `INTENT_CONFIDENCE_MIN` | 0.7 | not tuned yet | not tuned yet | not tuned yet | not tuned yet |
| `ROLE_SUGGESTION_FLOOR` | 0.70 | not tuned yet | not tuned yet | not tuned yet | not tuned yet |
| `POLICY_REFUSAL_FLOOR` | 0.35 | not tuned yet | not tuned yet | not tuned yet | not tuned yet |

## Conversation cases

`conversations/cases.jsonl` holds 33 multi-turn cases, one JSON line each: the
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
