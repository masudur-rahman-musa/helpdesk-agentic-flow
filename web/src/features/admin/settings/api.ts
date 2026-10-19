import { useMutation, useQueryClient } from "@tanstack/react-query";

import { API } from "@/lib/api-paths";
import { queryKeys } from "@/lib/query-keys";
import { isTypedCode } from "@/lib/wire-read";

/** Carries only the typed code; the switch picks the sentence. */
export class SettingsRefusedError extends Error {
  readonly code: string;

  constructor(code: string) {
    super("the setting was refused");
    this.code = code;
  }
}

/**
 * Set the switch, then read `me` again: the server decides what the switch
 * shows, so there is no optimistic flip to undo.
 */
export function useSetToolStepsVisible() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: postToolSteps,
    onSuccess: async (changed: boolean) => {
      if (changed) {
        await queryClient.invalidateQueries({ queryKey: queryKeys.me });
      }
    },
  });
}

/** Whether to re-read `me`; `false` on a redirect to `/login`. */
async function postToolSteps(show: boolean): Promise<boolean> {
  const response = await fetch(API.settings, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ show_tool_steps: show }),
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
      throw new SettingsRefusedError(code);
    }
    throw new Error(`the setting could not be changed (${response.status})`);
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
