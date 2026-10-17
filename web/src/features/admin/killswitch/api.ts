import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  type KillSwitchRow,
  killSwitchBoardSchema,
  setKillSwitchResultSchema,
} from "@/features/admin/killswitch/schema";
import { API } from "@/lib/api-paths";
import { queryKeys } from "@/lib/query-keys";
// The vocabulary check alone: `readWire` is a GET and cannot express the toggle
// POST, so its fetch stays as it is while the design's eight are checked in the
// one place that holds them.
import { isTypedCode, readWire } from "@/lib/wire-read";

/**
 * What the board answered, or `null` while the browser navigates away.
 *
 * The null is the redirect case and nothing else: rendering the empty state for
 * an expired session would be a platform reporting itself clean to somebody who
 * is not even signed in, and rendering the alert would blame a read that was
 * fine. Pinned by `::follows the proxy's redirect to /login instead of
 * rendering a board`, which asserts all three absences.
 */
export type BoardAnswer = KillSwitchRow[] | null;

/** The kill switch board, read through TanStack Query. */
export function useKillSwitchBoard() {
  return useQuery({ queryKey: queryKeys.killSwitchBoard, queryFn: fetchBoard });
}

/**
 * Re-read the board. Invalidation and never an optimistic rewrite: the tool
 * decides what the row becomes, and a row that changed on click would show a
 * stop that did not land as done.
 */
export function useRefreshKillSwitchBoard(): () => void {
  const queryClient = useQueryClient();
  return () => {
    void queryClient.invalidateQueries({ queryKey: queryKeys.killSwitchBoard });
  };
}

/**
 * One row's toggle. `onChanged` runs only when the change landed, never on the
 * redirect to `/login`, so nothing re-reads under an expired session.
 */
export function useKillSwitchToggle(tool: string, onChanged: () => void) {
  return useMutation({
    mutationFn: ({ act, reason }: { act: ToggleAct; reason: string }) =>
      postToggle(tool, act, reason),
    onSuccess: (changed: ToggleOutcome) => {
      if (changed) {
        onChanged();
      }
    },
  });
}

/**
 * The whole board, in the registry's order, or `null` while the browser follows
 * the proxy's redirect to `/login`.
 */
async function fetchBoard(): Promise<BoardAnswer> {
  const payload = await readWire(API.killSwitch, killSwitchBoardSchema);
  return payload === null ? null : payload.switches;
}

/** The two scope literals the tool takes, and the only two this page sends. */
type Scope = "global" | "tenant";

/** One control's identity: which layer it acts on and in which direction. */
export interface ToggleAct {
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
export type ToggleOutcome = boolean;

/**
 * A toggle the wire refused, carrying the typed code and nothing else.
 *
 * The code is what the design puts on the wire; the sentence is this app's, so
 * no free text crosses the boundary and two surfaces cannot say two different
 * things about one refusal.
 */
export class ToggleRefusedError extends Error {
  readonly code: string;

  constructor(code: string) {
    super("the kill switch change was refused");
    this.code = code;
  }
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
async function postToggle(tool: string, act: ToggleAct, reason: string): Promise<ToggleOutcome> {
  const response = await fetch(API.killSwitch, {
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
