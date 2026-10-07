"use client";

import type { StepEvent } from "@/components/chat/types";

/**
 * The turn's timeline: one row per `step` event, in arrival order.
 *
 * A row carries what the wire carries and nothing else — the node, the round,
 * and one entry per tool with its name and its result status (an earlier change
 * decision C, a design note). No arguments, no payloads, no resolved entities: the
 * `step` frame strips all four on purpose (the design), pinned on the Python
 * side by
 * `test_turn_stream.py::test_a_step_carries_no_arguments_no_payload_and_no_entities`,
 * so there is nothing here to leak and widening the row is a change to that
 * frame first.
 *
 * The status is the typed code when a tool refused, never a bare "failed": a
 * refused `create_access_request` and a successful one must not read alike,
 * and the code is the part a user can act on. Pinned by
 * `tests/chat-page.test.tsx`'s "renders one row per step event, naming each
 * tool and its result status".
 *
 * Exactly one `listitem` per event: the per-tool entries are not list items,
 * so the row count is the event count and a row showing the whole accumulated
 * `tool_results` would be visible as a count, not only as prose.
 */
export function ToolSteps({ steps }: { steps: StepEvent[] }) {
  if (steps.length === 0) {
    return null;
  }

  return (
    <section className="rounded-md bg-muted p-4">
      <h2 className="text-sm font-semibold">Tool steps</h2>
      <ul aria-label="Tool steps" className="mt-2 flex flex-col gap-2">
        {steps.map((step, index) => (
          <li key={index} className="flex flex-col gap-1 text-sm">
            <span className="font-medium">
              {step.node} <span className="text-muted-foreground">round {step.round}</span>
            </span>
            <span className="flex flex-wrap gap-2">
              {step.tools.map((tool) => (
                <span
                  key={tool.tool}
                  className={
                    tool.ok
                      ? "rounded-md bg-background px-2 py-1 text-xs text-foreground"
                      : "rounded-md border border-destructive/30 bg-destructive/10 px-2 py-1 text-xs text-destructive"
                  }
                >
                  {tool.tool}{" "}
                  <span className={tool.ok ? "text-muted-foreground" : "font-medium"}>
                    {tool.ok ? "ok" : (tool.errorCode ?? "failed")}
                  </span>
                </span>
              ))}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}
