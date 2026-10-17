import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  type KillSwitchRow,
  killSwitchBoardSchema,
  setKillSwitchResultSchema,
} from "@/features/admin/killswitch/schema";
import { API } from "@/lib/api-paths";
import { queryKeys } from "@/lib/query-keys";
// `readWire` only does GETs, so the toggle POST reuses just its code check.
import { isTypedCode, readWire } from "@/lib/wire-read";

/** `null` only while the browser follows the redirect to `/login`. */
export type BoardAnswer = KillSwitchRow[] | null;

export function useKillSwitchBoard() {
  return useQuery({ queryKey: queryKeys.killSwitchBoard, queryFn: fetchBoard });
}

/** Re-read, never an optimistic update: the server decides what the row becomes. */
export function useRefreshKillSwitchBoard(): () => void {
  const queryClient = useQueryClient();
  return () => {
    void queryClient.invalidateQueries({ queryKey: queryKeys.killSwitchBoard });
  };
}

/** `onChanged` runs only when the change landed, never on a redirect. */
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

async function fetchBoard(): Promise<BoardAnswer> {
  const payload = await readWire(API.killSwitch, killSwitchBoardSchema);
  return payload === null ? null : payload.switches;
}

type Scope = "global" | "tenant";

export interface ToggleAct {
  scope: Scope;
  enabled: boolean;
}

/** Whether to re-read the board; `false` on a redirect to `/login`. */
export type ToggleOutcome = boolean;

/** Carries only the typed code; the page picks the sentence. */
export class ToggleRefusedError extends Error {
  readonly code: string;

  constructor(code: string) {
    super("the kill switch change was refused");
    this.code = code;
  }
}

/**
 * `enabled` is about the switch, not the tool: Stop sends `enabled: true`.
 * Getting it backwards would quietly release a tool an admin just stopped.
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
    // Check the status first: the proxy's own 502 body is valid JSON too.
    const body = await readJson(response);
    const code =
      typeof body === "object" && body !== null ? (body as { code?: unknown }).code : undefined;
    if (isTypedCode(code)) {
      throw new ToggleRefusedError(code);
    }
    throw new Error(`the kill switch could not be changed (${response.status})`);
  }

  // Parsed only to catch a wrong answer; the re-read board is what shows.
  const parsed = setKillSwitchResultSchema.safeParse(await readJson(response));
  if (!parsed.success) {
    throw new Error("the kill switch answer did not match what this page reads");
  }
  return true;
}

async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return undefined;
  }
}
