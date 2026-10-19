"use client";

import { SettingsRefusedError, useSetToolStepsVisible } from "@/features/admin/settings/api";
import { useMe } from "@/features/auth/api";
import { Alert } from "@/components/ui/alert";
import { failureMessage } from "@/lib/failure-messages";
import { cn } from "@/lib/utils";

const CHANGE_FAILED_MESSAGE = "The setting could not be changed. Try again in a moment.";

/**
 * The one switch: show the tool steps box in this tenant's chat. It shows what
 * `/api/me` says and changes only after the server has answered, so a refused
 * change leaves it as it was.
 */
export function ToolStepsSwitch() {
  const me = useMe();
  const change = useSetToolStepsVisible();
  const on = me?.show_tool_steps === true;

  const message =
    change.error === null
      ? null
      : change.error instanceof SettingsRefusedError
        ? failureMessage(change.error.code)
        : CHANGE_FAILED_MESSAGE;

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center justify-between gap-4 rounded-md bg-muted p-4">
        <div className="space-y-1">
          <p className="text-sm font-medium">Show tool steps in chat</p>
          <p className="text-sm text-muted-foreground">
            Off by default. When on, everyone in this tenant sees the steps beside the chat.
          </p>
        </div>
        <button
          type="button"
          role="switch"
          aria-checked={on}
          aria-label="Show tool steps in chat"
          disabled={me === undefined || change.isPending}
          onClick={() => change.mutate(!on)}
          className={cn(
            "relative h-6 w-11 shrink-0 rounded-full transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50",
            on ? "bg-primary" : "bg-border",
          )}
        >
          <span
            aria-hidden="true"
            className={cn(
              "absolute left-0.5 top-0.5 h-5 w-5 rounded-full bg-background transition-transform",
              on ? "translate-x-5" : "translate-x-0",
            )}
          />
        </button>
      </div>
      {message !== null ? <Alert>{message}</Alert> : null}
    </div>
  );
}
