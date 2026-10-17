"use client";

import type { StepEvent } from "@/features/chat/components/types";

/**
 * The turn's timeline, one row per `step` event. The server sends no tool
 * arguments or payloads here, so nothing private can leak. A refused tool
 * shows its typed code, the part a user can act on, not a bare "failed".
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
