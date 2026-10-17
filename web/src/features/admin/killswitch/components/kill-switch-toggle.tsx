"use client";

import { useState } from "react";

import { type ToggleAct, useKillSwitchToggle } from "@/features/admin/killswitch/api";
import {
  KILL_SWITCH_REASON_REQUIRED_MESSAGE,
  toggleRefusal,
} from "@/features/admin/killswitch/format";
import type { KillSwitchRow } from "@/features/admin/killswitch/schema";
import { Button } from "@/components/ui/button";
import { Alert } from "@/components/ui/alert";
import { TextArea } from "@/components/ui/field";

/**
 * Stop or release one tool at either layer, with a reason. Each row has its own
 * pending state, so stopping one tool never greys out another row mid-incident.
 * No `maxLength` on the reason: the server checks it, and a browser cap would
 * quietly cut the only record of why a tool was stopped.
 */

export function KillSwitchToggle({
  row,
  onChanged,
}: {
  row: KillSwitchRow;
  onChanged: () => void;
}) {
  const [reason, setReason] = useState("");
  const [armed, setArmed] = useState(false);
  const [blankReason, setBlankReason] = useState(false);

  const toggle = useKillSwitchToggle(row.tool, () => {
    // So the next stop cannot reuse this reason.
    setReason("");
    setArmed(false);
    onChanged();
  });

  // From the row, never a browser copy of the protected list, which would go stale.
  const disabled = row.protected || toggle.isPending;
  const message = blankReason ? KILL_SWITCH_REASON_REQUIRED_MESSAGE : toggleRefusal(toggle.error);

  /** Trimmed, so a reason of three spaces does not pass. */
  function run(act: ToggleAct): boolean {
    if (reason.trim() === "") {
      setBlankReason(true);
      return false;
    }
    setBlankReason(false);
    toggle.mutate({ act, reason });
    return true;
  }

  /**
   * The only click that stops a tool for every tenant, so it takes two presses.
   * Release is never armed: bringing things back must stay fast.
   */
  function onStopGlobal(): void {
    if (reason.trim() === "") {
      setBlankReason(true);
      return;
    }
    setBlankReason(false);
    if (!armed) {
      setArmed(true);
      return;
    }
    setArmed(false);
    toggle.mutate({ act: { scope: "global", enabled: true }, reason });
  }

  /** Any other click disarms, so a stale arm never becomes a one-press stop. */
  function onOther(act: ToggleAct): void {
    setArmed(false);
    run(act);
  }

  return (
    <div className="flex flex-col gap-2">
      {message !== null ? <Alert>{message}</Alert> : null}

      <TextArea
        aria-label={`Reason for ${row.tool}`}
        rows={2}
        value={reason}
        onChange={(event) => setReason(event.target.value)}
      />

      <div className="flex flex-wrap gap-2">
        {holdsGlobal(row) ? (
          <Button
            type="button"
            aria-label={`Release platform-wide for ${row.tool}`}
            disabled={disabled}
            onClick={() => onOther({ scope: "global", enabled: false })}
          >
            Release platform-wide
          </Button>
        ) : (
          <Button
            type="button"
            aria-label={
              armed
                ? `Confirm stop platform-wide for ${row.tool}`
                : `Stop platform-wide for ${row.tool}`
            }
            disabled={disabled}
            onClick={onStopGlobal}
          >
            {armed ? "Confirm stop platform-wide" : "Stop platform-wide"}
          </Button>
        )}

        {holdsTenant(row) ? (
          <Button
            type="button"
            aria-label={`Release for this tenant for ${row.tool}`}
            disabled={disabled}
            onClick={() => onOther({ scope: "tenant", enabled: false })}
          >
            Release for this tenant
          </Button>
        ) : (
          <Button
            type="button"
            aria-label={`Stop for this tenant for ${row.tool}`}
            disabled={disabled}
            onClick={() => onOther({ scope: "tenant", enabled: true })}
          >
            Stop for this tenant
          </Button>
        )}
      </div>
    </div>
  );
}

/**
 * Read off this layer, not `stopped`, which merges both. An unreadable row
 * offers Release, since deleting the bad value is the only fix.
 */
function holdsGlobal(row: KillSwitchRow): boolean {
  return row.unreadable || row.global_state !== null;
}

function holdsTenant(row: KillSwitchRow): boolean {
  return row.unreadable || row.tenant_state !== null;
}
