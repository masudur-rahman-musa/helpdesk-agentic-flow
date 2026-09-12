"use client";

import { useMutation } from "@tanstack/react-query";
import { useState } from "react";

import { FAILURE_MESSAGES, failureMessage } from "@/lib/chat-errors";
import { type KillSwitchRow, setKillSwitchResultSchema } from "@/lib/killswitch-schema";
import { Button } from "@/components/ui/field";

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

/**
 * The sentence for a toggle whose answer said nothing usable (decision F).
 *
 * Its own string rather than `FAILURE_MESSAGES.default`: on this surface the
 * question an operator has is whether the stop LANDED, and "something went
 * wrong" leaves it open. Pinned verbatim by
 * `tests/killswitch-toggle.test.tsx::pins the failure and the reason-required
 * sentences exactly`.
 */
export const KILL_SWITCH_TOGGLE_MESSAGE =
  "The kill switch could not be changed. Try again in a moment.";

/** The sentence the page says when a control is clicked with no reason typed. */
export const KILL_SWITCH_REASON_REQUIRED_MESSAGE = "Give a reason before changing a kill switch.";

/**
 * The `POLICY_BLOCKED` sentence on this surface, and only on this surface
 * (decision E).
 *
 * `FAILURE_MESSAGES.POLICY_BLOCKED` is about chat rate limiting ("Too many
 * messages just now."), which would tell an admin to wait for a refusal that is
 * a permanent property of a protected tool. 22b's `tests/chat-schema.test.ts`
 * pins every string in that map verbatim, so widening the shared map is not
 * available. Pinned by `::pins the two override sentences exactly and apart
 * from the shared map`.
 */
export const KILL_SWITCH_POLICY_BLOCKED_MESSAGE = "That tool is protected and cannot be stopped.";

/**
 * The `VALIDATION` sentence on this surface, and only on this surface
 * (decision E).
 *
 * The shared one is "That message could not be sent.", which is about a chat
 * message; here the only value a person typed is the reason, so the sentence
 * says which one to shorten. Exactly TWO codes are overridden — every other
 * sentence still comes from the shared map, so no second opinion about
 * `FORBIDDEN` or `TOOL_DISABLED` can appear on this page.
 */
export const KILL_SWITCH_VALIDATION_MESSAGE =
  "That could not be saved. Shorten the reason and try again.";

/** The proxy path, one route carrying both verbs (24cb decision E). */
const TOGGLE_PATH = "/api/admin/killswitch";

/** The two scope literals the tool takes, and the only two this page sends. */
type Scope = "global" | "tenant";

/** One control's identity: which layer it acts on and in which direction. */
interface Act {
  scope: Scope;
  enabled: boolean;
}

/**
 * What a toggle POST resolved to: whether the board should be re-read.
 *
 * `false` is the redirect case and nothing else — the browser is navigating to
 * `/login`, so invalidating a query it is about to leave behind would fire a
 * read under an expired session.
 */
type ToggleOutcome = boolean;

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

  const toggle = useMutation({
    mutationFn: (act: Act) => postToggle(row.tool, act, reason),
    onSuccess: (changed: ToggleOutcome) => {
      if (!changed) {
        return;
      }
      // Cleared so the NEXT tool stopped from this screen cannot be filed with
      // the previous tool's reason, and the arming cleared with it (§1).
      setReason("");
      setArmed(false);
      onChanged();
    },
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
  function run(act: Act): boolean {
    if (reason.trim() === "") {
      setBlankReason(true);
      return false;
    }
    setBlankReason(false);
    toggle.mutate(act);
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
    toggle.mutate({ scope: "global", enabled: true });
  }

  /**
   * Any other control in this row, which also disarms the platform-wide stop
   * (§1): an arming that survived an unrelated click would turn the admin's
   * next platform-wide click into a one-press stop for every tenant.
   */
  function onOther(act: Act): void {
    setArmed(false);
    run(act);
  }

  return (
    <div className="flex flex-col gap-2">
      {message !== null ? (
        <p role="alert" className="rounded-md border border-border px-3 py-2 text-sm">
          {message}
        </p>
      ) : null}

      <textarea
        aria-label={`Reason for ${row.tool}`}
        rows={2}
        value={reason}
        onChange={(event) => setReason(event.target.value)}
        className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
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

/**
 * A toggle the wire refused, carrying the typed code and nothing else.
 *
 * The code is what the design puts on the wire; the sentence is this app's, so
 * no free text crosses the boundary and two surfaces cannot say two different
 * things about one refusal.
 */
class ToggleRefusedError extends Error {
  readonly code: string;

  constructor(code: string) {
    super("the kill switch change was refused");
    this.code = code;
  }
}

/**
 * Whether a body's `code` is one of the design's eight, which is what separates
 * this surface's two unhappy paths (decision E, a project rule): a typed
 * refusal renders that code's sentence, and everything else — a payload the
 * parser refuses, a body that is not JSON, or a code outside that vocabulary
 * such as the proxy's own `UPSTREAM_UNAVAILABLE` — is a failed toggle.
 * `default` is the shared map's fallback key and never a wire value.
 *
 * The third copy of this predicate on the admin surface, beside
 * `kill-switch-board.tsx`'s and `audit-search.tsx`'s. 24da decision F refused
 * the shared-fetcher refactor on purpose and this plan's §3 repeats the
 * refusal; the move is owed to the plan that takes that surface on.
 */
function isTypedCode(code: unknown): code is string {
  return typeof code === "string" && code !== "default" && code in FAILURE_MESSAGES;
}

/** The sentence for a refused toggle, or `null` while nothing is refused. */
function toggleRefusal(error: unknown): string | null {
  if (error === null || error === undefined) {
    return null;
  }
  if (!(error instanceof ToggleRefusedError)) {
    return KILL_SWITCH_TOGGLE_MESSAGE;
  }
  if (error.code === "POLICY_BLOCKED") {
    return KILL_SWITCH_POLICY_BLOCKED_MESSAGE;
  }
  if (error.code === "VALIDATION") {
    return KILL_SWITCH_VALIDATION_MESSAGE;
  }
  return failureMessage(error.code);
}

/**
 * One change, posted to the proxy for this tool, this layer and this direction.
 *
 * `enabled` describes the SWITCH and `stopped` the TOOL (13c decision B), so a
 * Stop sends `enabled: true` at either scope. Inverting it would release a tool
 * an admin just stopped, silently, with a 200.
 *
 * Answers `false` when the browser is navigating to `/login`, so the caller
 * files nothing and re-reads nothing.
 */
async function postToggle(tool: string, act: Act, reason: string): Promise<ToggleOutcome> {
  const response = await fetch(TOGGLE_PATH, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      tool_name: tool,
      enabled: act.enabled,
      scope: act.scope,
      reason,
    }),
  });

  if (response.redirected) {
    window.location.assign(response.url);
    return false;
  }
  if (!response.ok) {
    // The status is read BEFORE the body: the proxy's own `502
    // UPSTREAM_UNAVAILABLE` is valid JSON carrying a code outside the design,
    // and a reader that skipped the status would answer a refusal sentence for
    // an outage.
    const body = await readJson(response);
    const code =
      typeof body === "object" && body !== null ? (body as { code?: unknown }).code : undefined;
    if (isTypedCode(code)) {
      throw new ToggleRefusedError(code);
    }
    throw new Error(`the kill switch could not be changed (${response.status})`);
  }

  // Parsed and its fields UNRENDERED (decision F): the invalidated board is
  // what an admin reads, and the parse is what turns an answer that is not a
  // set result into a failure sentence rather than a confident refetch.
  const parsed = setKillSwitchResultSchema.safeParse(await readJson(response));
  if (!parsed.success) {
    throw new Error("the kill switch answer did not match what this page reads");
  }
  return true;
}

/** The body as JSON, or `undefined` when it is not JSON at all. */
async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return undefined;
  }
}
