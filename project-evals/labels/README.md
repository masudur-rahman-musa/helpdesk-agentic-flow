# Judge calibration labels
Human-graded outputs (1 to 5 against the rubric) used to check the LLM judge
before trusting it. Minimum 40 items. Store as `*.jsonl`: {"id", "case_id",
"output", "human_score", "note"}. `case_id` names the golden case the output
answers, so the judge is given the same rubric the human was. Report Cohen's
kappa in every eval run.
