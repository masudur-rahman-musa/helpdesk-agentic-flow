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
 * What one row of the kill switch board can DO: stop or release the tool, at
 * either layer, with a reason.
 *
 * One instance per row, holding its own reason text, its own arming flag and
 * its own in-flight mutation (§2), `DecideControls`' shape: one
 * in-flight flag for the whole board would grey out every control while a
 * single tool is being stopped, which during an incident greys out the
 * releases an operator needs next. Pinned by
 * `tests/killswitch-toggle.test.tsx::disables only the acting row's controls
 * while its toggle is in flight`.
 *
 * a project rule holds without an exception: the mutation is `set_kill_switch`, a
 * typed MCP tool behind RBAC and the protected-tool guard, reached through
 * 24cb's proxy under the caller's own bearer. The LLM is not in the path at all
 * — the tool is deliberately absent from the agent's catalogue (13c and 24ca
 * decision F) — so the only caller is a person clicking a button. Nothing here
 * decides anything: whether the change is allowed, whether the tool may be
 * stopped, which tenant the switch is written for and what the audit event says
 * are all decided behind the MCP boundary.
 *
 * The body is exactly four keys (§1). No tenant id — the router substitutes the
 * token's own (24cb decision C), and a browser naming one would be a project rule
 * written into the page — and no idempotency key, which 13c decision C says
 * nothing derives here. Pinned as an EXACT object over all four combinations by
 * `::$name posts its exact four-key body`.
 *
 * The reason field declares no `maxLength` (§1, 23c decision E's precedent):
 * `REASON_MAX_LENGTH` is the tool's own and applies at the route, so a browser
 * cap would silently truncate the only record of why a tool was stopped.
 * Pinned by `::sends a 500 character reason whole, with no maxLength on the
 * field`. The one refusal the page answers for itself is a blank reason, a
 * floor and never a second cap (`::$name makes no call at all and says so in
 * the row`).
 *
 * An expired session at the toggle is `response.redirected` rather than a
 * status, because the browser has already followed the proxy's redirect by the
 * time this code runs (22b decision E). A board sits open on a screen for
 * minutes, so a session expiring between the paint and the click is the common
 * case, which `::follows the proxy's redirect to /login, with no alert and no
 * re-read` pins.
 *
 * Accessible names: the two controls carry the scope AND the tool (`Stop
 * platform-wide for {tool}`, `Stop for this tenant for {tool}`, and their
 * Release forms), because two rows otherwise carry four controls with the same
 * two names; the armed control reads `Confirm stop platform-wide for {tool}`
 * and the field is `Reason for {tool}`.
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
    // Cleared so the NEXT tool stopped from this screen cannot be filed with
    // the previous tool's reason, and the arming cleared with it (§1).
    setReason("");
    setArmed(false);
    onChanged();
  });

  // Read off the row's own `protected` and never off a browser-side copy of
  // `PROTECTED_TOOLS` (§1): a second copy of a security-relevant list goes
  // stale the day the server's changes.
  const disabled = row.protected || toggle.isPending;
  const message = blankReason ? KILL_SWITCH_REASON_REQUIRED_MESSAGE : toggleRefusal(toggle.error);

  /**
   * The floor every click passes first: a reason the tool would refuse costs a
   * round trip to be told. Trimmed, because `reason === ""` lets three spaces
   * past and files a switch nobody can explain later.
   */
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
   * The platform-wide STOP, and the only click in the product that stops a tool
   * for every tenant: armed first, posted second (decision C). A Release is not
   * armed at either scope — making an admin click twice to bring a platform
   * back up is a cost paid during an incident.
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

  /**
   * Any other control in this row, which also disarms the platform-wide stop
   * (§1): an arming that survived an unrelated click would turn the admin's
   * next platform-wide click into a one-press stop for every tenant.
   */
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
 * Whether the platform layer holds this tool down, read off THAT layer and
 * never off `stopped`, which merges the two (§1).
 *
 * A row whose global layer is held and whose tenant layer is null offers
 * `Release platform-wide` beside `Stop for this tenant`; a toggle deriving both
 * from `stopped` would offer a Release for a tenant switch that holds nothing,
 * and the admin's click would clear nothing while the platform kept the tool
 * down. Pinned over all four layer combinations by
 * `tests/killswitch-toggle.test.tsx::derives each verb from its own layer with
 * $name`, each fixture carrying a `stopped` that contradicts the layer it would
 * be mistaken for.
 *
 * An `unreadable` row is the one exception (decision D): both stored states are
 * null because the tool nulled them (24ca decision D), so the derivation would
 * offer a Stop for a tool that is already denied on every call, when the only
 * act that clears a corrupt value is the delete a Release posts. Releasing a
 * layer that holds nothing is a no-op, so the wrong half of the guess costs
 * nothing. Pinned by `::renders Release on both layers for an unreadable row`.
 */
function holdsGlobal(row: KillSwitchRow): boolean {
  return row.unreadable || row.global_state !== null;
}

/** The tenant layer's half of the same derivation. */
function holdsTenant(row: KillSwitchRow): boolean {
  return row.unreadable || row.tenant_state !== null;
}
