"use client";

import { useQuery } from "@tanstack/react-query";

import { FAILURE_MESSAGES, failureMessage } from "@/lib/chat-errors";
import { formatSwitchInstant, KILL_SWITCH_READ_MESSAGE } from "@/lib/killswitch-format";
import {
  type KillSwitchRow,
  killSwitchBoardSchema,
  type SwitchState,
} from "@/lib/killswitch-schema";

/**
 * The kill switch board, from `GET /api/admin/killswitch`.
 *
 * A client component doing its own read through TanStack Query (§2, decision
 * A), `AuditSearch`'s and `ReviewQueue`'s shape: the bearer stays an httpOnly
 * cookie the proxy attaches, and an expired session is `response.redirected`
 * rather than a status, because the browser has already followed the proxy's
 * redirect by the time this code runs (22b decision E). Pinned by
 * `tests/killswitch-board.test.tsx::follows the proxy's redirect to /login
 * instead of rendering a board`.
 *
 * The read is fired on paint and takes NO argument of any kind (decision A):
 * which board this caller sees is the token's tenant, decided behind the MCP
 * boundary, so a page naming one would be a project rule written into the browser.
 * It is one Redis `MGET` inside the tool (24ca decision C), which is why it
 * needs no click to start it. Pinned by `::reads the proxy once on paint, as a
 * GET with no query and no body`.
 *
 * Everything here is a READ, so a project rule has nothing to bite on: nothing on
 * this surface writes, and the toggle is an earlier change's. Nothing here decides
 * anything either — which tools are on the board, in what order, whether each
 * is stopped and who may see any of it all come from 24ca's tool. This renders.
 *
 * The rows are painted in the payload's own order, which is the registry's:
 * never sorted, filtered or grouped here, so a tool added by a later plan
 * cannot land somewhere an operator does not look. Pinned over two payload
 * orders by `::renders a payload in $name order exactly as it arrived`.
 *
 * A failed read THROWS rather than answering an empty board, so the alert and
 * the empty state are mutually exclusive by construction: `No tools are
 * registered.` over a broken proxy is the one sentence on this page an operator
 * acts on by concluding the platform is clean. Pinned from both sides by
 * `::renders the named empty state with no table and no alert` and the three
 * `$name renders the read message, no table and no empty state` rows.
 *
 * Accessible names: the region is `Kill switches`, the board is the table `Tool
 * kill switches` and its columns are `Tool`, `Status`, `Platform-wide` and
 * `This tenant`. Both names are `aria-label`s, because neither role takes its
 * name from its contents.
 */

/** The four columns, in the order every case in the test file reads them by. */
const COLUMNS = ["Tool", "Status", "Platform-wide", "This tenant"] as const;

/** What a layer holding no switch shows, rather than an empty cell. */
const NO_SWITCH = "No switch";

/**
 * What the board answered, or `null` while the browser navigates away.
 *
 * The null is the redirect case and nothing else: rendering the empty state for
 * an expired session would be a platform reporting itself clean to somebody who
 * is not even signed in, and rendering the alert would blame a read that was
 * fine. Pinned by `::follows the proxy's redirect to /login instead of
 * rendering a board`, which asserts all three absences.
 */
type BoardAnswer = KillSwitchRow[] | null;

export function KillSwitchBoard() {
  const board = useQuery({
    queryKey: ["admin", "killswitch", "board"],
    queryFn: fetchBoard,
  });

  const refusal = board.isError ? errorMessage(board.error) : null;
  const rows = board.isSuccess ? board.data : null;

  return (
    <section aria-label="Kill switches" className="flex max-w-5xl flex-col gap-4">
      <h1 className="text-lg font-semibold">Kill switches</h1>

      {refusal !== null ? (
        <p role="alert" className="rounded-md border border-border px-3 py-2 text-sm">
          {refusal}
        </p>
      ) : null}

      {rows !== null && rows.length === 0 ? (
        <p className="text-sm text-muted-foreground">No tools are registered.</p>
      ) : null}

      {rows !== null && rows.length > 0 ? <SwitchTable rows={rows} /> : null}
    </section>
  );
}

/**
 * One line per registered tool: its name, whether it is stopped, and both
 * layers side by side.
 *
 * The two layers are their own columns rather than one merged state (decision
 * C, 24ca decision B's own reason): an admin clearing the tenant switch has to
 * be told the global one still holds the tool down, and a merged column could
 * not say so.
 */
function SwitchTable({ rows }: { rows: KillSwitchRow[] }) {
  return (
    <div className="overflow-x-auto">
      <table aria-label="Tool kill switches" className="w-full text-left text-sm">
        <thead>
          <tr>
            {COLUMNS.map((column) => (
              <th key={column} scope="col">
                {column}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.tool} className="border-t border-border align-top">
              <td>
                {row.tool}
                {/* `protected` reaches the browser as a field of the row so
                    24db's disabled toggle reads it here rather than from a
                    second browser-side copy of `PROTECTED_TOOLS`. */}
                {row.protected ? (
                  <span className="ml-2 text-xs text-muted-foreground">Protected</span>
                ) : null}
                {/* The only surface a bad key can be seen from (24ca decision
                    D): the tool nulls both layer states on such a row, so
                    without this marker the line reads as a legitimately
                    stopped tool and nobody goes looking for the key. */}
                {row.unreadable ? (
                  <span className="ml-2 text-xs text-muted-foreground">
                    Switch value unreadable
                  </span>
                ) : null}
              </td>
              {/* Read off `stopped` and NEVER recomputed from the two layer
                  states (decision C): `stopped` is true when either layer holds
                  the tool down OR the stored value will not parse, and only the
                  tool knows that precedence. A board deriving it would render
                  an unreadable row as running while every call to that tool is
                  denied (§5's first risk), which
                  `tests/killswitch-board.test.tsx::reads $status for stopped
                  true with both states null and unreadable false` pins. */}
              <td>{row.stopped ? "Stopped" : "Running"}</td>
              <td>
                <LayerCell state={row.global_state} />
              </td>
              <td>
                <LayerCell state={row.tenant_state} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/**
 * One layer's three facts — why, who and when — or the absence of a switch.
 *
 * A null layer says `No switch` rather than drawing nothing: on the one screen
 * whose job is saying WHICH layer holds a tool down, an operator cannot tell an
 * empty cell from a cell that failed to draw.
 */
function LayerCell({ state }: { state: SwitchState | null }) {
  if (state === null) {
    return <span className="text-muted-foreground">{NO_SWITCH}</span>;
  }
  return (
    <div className="flex flex-col gap-0.5">
      <span>{state.reason}</span>
      <span className="text-xs text-muted-foreground">{state.actor}</span>
      {/* Never the raw wire string: decision B is what turns
          `2026-09-12T08:00:00+00:00` into a date and a minute an operator
          reads, and the format itself is pinned by
          `tests/killswitch-format.test.ts`. */}
      <span className="text-xs text-muted-foreground">{formatSwitchInstant(state.at)}</span>
    </div>
  );
}

/**
 * A read the wire refused, carrying the typed code and nothing else.
 *
 * The code is what the design puts on the wire; the sentence is this app's, so
 * no free text crosses the boundary and two surfaces cannot say two different
 * things about one refusal.
 */
class BoardRefusedError extends Error {
  readonly code: string;

  constructor(code: string) {
    super("the kill switch board was refused");
    this.code = code;
  }
}

/**
 * Whether a body's `code` is one of the design's eight, which is what separates
 * this page's two unhappy paths (decision E, a project rule): a typed refusal
 * renders that code's shared sentence, and everything else — a payload the
 * parser refuses, a non-JSON body, or a code outside that vocabulary such as
 * the proxy's own `UPSTREAM_UNAVAILABLE` — is a failed read. `default` is the
 * shared map's fallback key and never a wire value.
 */
function isTypedCode(code: unknown): code is string {
  return typeof code === "string" && code !== "default" && code in FAILURE_MESSAGES;
}

/**
 * The sentence for whatever went wrong, one alert either way.
 *
 * No override on this surface, unlike `AuditSearch`'s `VALIDATION`: the two
 * codes reachable here are `FORBIDDEN` and `TOOL_DISABLED`, and both shared
 * sentences are true of this read.
 */
function errorMessage(error: unknown): string {
  if (error instanceof BoardRefusedError) {
    return failureMessage(error.code);
  }
  return KILL_SWITCH_READ_MESSAGE;
}

/**
 * The whole board, in the registry's order, or `null` while the browser follows
 * the proxy's redirect to `/login`.
 */
async function fetchBoard(): Promise<BoardAnswer> {
  const response = await fetch("/api/admin/killswitch", {
    headers: { accept: "application/json" },
  });

  if (response.redirected) {
    window.location.assign(response.url);
    return null;
  }
  if (!response.ok) {
    // The status is read BEFORE the body: the proxy's own `502
    // UPSTREAM_UNAVAILABLE` is valid JSON, so a reader that skipped it would
    // turn an outage into "no tools are registered".
    const code = await typedCodeOf(response);
    if (code !== null) {
      throw new BoardRefusedError(code);
    }
    throw new Error(`the kill switches could not be read (${response.status})`);
  }

  const parsed = killSwitchBoardSchema.safeParse(await readJson(response));
  if (!parsed.success) {
    throw new Error("the kill switch payload did not match what this page reads");
  }
  return parsed.data.switches;
}

/**
 * The typed code a refused response carries, or null when it carries none — a
 * body that is not JSON at all included, which is why `json()` is guarded
 * rather than awaited into an unhandled rejection.
 */
async function typedCodeOf(response: Response): Promise<string | null> {
  const body = await readJson(response);
  if (typeof body !== "object" || body === null) {
    return null;
  }
  const code = (body as { code?: unknown }).code;
  return isTypedCode(code) ? code : null;
}

/** The body as JSON, or `undefined` when it is not JSON at all. */
async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return undefined;
  }
}
