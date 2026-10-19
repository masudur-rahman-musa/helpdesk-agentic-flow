import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  type OpenTicket,
  openTicketsSchema,
  type SolveTicketResult,
  solveTicketResultSchema,
} from "@/features/admin/tickets/schema";
import { API } from "@/lib/api-paths";
import { queryKeys } from "@/lib/query-keys";
// `readWire` only does GETs, so the solve POST reuses just its code check.
import { isTypedCode, readWire } from "@/lib/wire-read";

/** `null` only while the browser follows the redirect to `/login`. */
export type OpenTicketsAnswer = OpenTicket[] | null;

/** Every open ticket in the company, oldest first. */
export function useOpenTickets() {
  return useQuery({ queryKey: queryKeys.openTickets, queryFn: fetchOpenTickets });
}

async function fetchOpenTickets(): Promise<OpenTicketsAnswer> {
  const parsed = await readWire(API.openTickets, openTicketsSchema);
  return parsed === null ? null : parsed.tickets;
}

/**
 * Solve one ticket, then re-read the open list. The row is never removed here:
 * the server decides what is still open.
 */
export function useSolveTicket(ticketId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => postSolve(ticketId),
    onSuccess: (outcome: SolveOutcome) => {
      if (outcome !== null) {
        void queryClient.invalidateQueries({ queryKey: queryKeys.openTickets });
      }
    },
  });
}

/** The parsed answer, or `null` while redirecting to `/login`. */
export type SolveOutcome = SolveTicketResult | null;

/** Carries only the typed code; the page picks the sentence. */
export class SolveRefusedError extends Error {
  readonly code: string;

  constructor(code: string) {
    super("the solve was refused");
    this.code = code;
  }
}

/** A fresh idempotency key per click: a reused key would replay the old answer. */
async function postSolve(ticketId: string): Promise<SolveOutcome> {
  const response = await fetch(API.solveTicket, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ ticket_id: ticketId, idempotency_key: crypto.randomUUID() }),
  });

  if (response.redirected) {
    window.location.assign(response.url);
    return null;
  }
  if (!response.ok) {
    // Check the status first: the proxy's own 502 body is valid JSON too.
    const body = await readJson(response);
    const code =
      typeof body === "object" && body !== null ? (body as { code?: unknown }).code : undefined;
    if (isTypedCode(code)) {
      throw new SolveRefusedError(code);
    }
    throw new Error(`the solve could not be completed (${response.status})`);
  }

  const parsed = solveTicketResultSchema.safeParse(await readJson(response));
  if (!parsed.success) {
    throw new Error("the solve answer did not match what this page reads");
  }
  return parsed.data;
}

async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return undefined;
  }
}
