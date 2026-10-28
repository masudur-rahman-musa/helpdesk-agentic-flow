import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { KillSwitchBoard } from "@/features/admin/killswitch/components/kill-switch-board";
import {
  KILL_SWITCH_POLICY_BLOCKED_MESSAGE,
  KILL_SWITCH_REASON_REQUIRED_MESSAGE,
  KILL_SWITCH_TOGGLE_MESSAGE,
  KILL_SWITCH_VALIDATION_MESSAGE,
} from "@/features/admin/killswitch/format";
import { QueryProvider } from "@/components/layout/query-provider";
import { FAILURE_MESSAGES } from "@/lib/failure-messages";

/**
 * `killswitch-toggle.test.tsx`: the per-row kill switch toggle —
 * two controls, a reason, one POST — against a stubbed `fetch`. jsdom, no
 * agent-api, no backing service. (The neighbours are `killswitch-board.test.tsx`,
 * `killswitch-format.test.ts`, `killswitch-schema.test.ts` and
 * `killswitch-proxy-routes.test.ts`.)
 *
 * The toggle is NOT rendered standalone: it holds `useMutation` and is a child
 * of `KillSwitchBoard`, so every case here renders `<KillSwitchBoard />` inside
 * `QueryProvider` against a stub that answers the board
 * on `GET /api/admin/killswitch` and the toggle on `POST /api/admin/killswitch`
 * — ONE path, two verbs, which is why every call assertion in this file
 * discriminates on the method rather than on the URL.
 *
 * Shapes this file fixes:
 * - the component is `KillSwitchToggle` from
 *   `web/src/features/admin/killswitch/components/kill-switch-toggle.tsx`, taking
 *   `{ row: KillSwitchRow; onChanged: () => void }`. No case
 *   imports it: the board is the only renderer, so the component's name is
 *   pinned by nothing here and the four SENTENCES are what this file imports.
 * - the four override / failure constants are exported from that same module as
 *   `KILL_SWITCH_TOGGLE_MESSAGE`, `KILL_SWITCH_REASON_REQUIRED_MESSAGE`,
 *   `KILL_SWITCH_POLICY_BLOCKED_MESSAGE` and `KILL_SWITCH_VALIDATION_MESSAGE`,
 *   the `DECISION_VALIDATION_MESSAGE` precedent, so this file imports
 *   them rather than repeating three of the four.
 * - a POST is read off the stub's call list as `[input, init]`, with
 *   `JSON.parse(init.body)` asserted as a WHOLE object (`toEqual`), so a fifth
 *   key fails the case. The relative-versus-absolute form of the URL is not
 *   fixed; the pathname is.
 * - the row a case acts in is found by its `Tool` cell, `killswitch-board.test
 *   .tsx`'s own helper, and every control is located `within(row)`.
 * - a refusal's sentence is a `role="alert"` INSIDE the acting row (the board's
 *   read failure is the region-level alert, and the two are located
 *   differently on purpose).
 * - an expired session at the POST is `response.redirected` with `url` on the
 *   INSTANCE and `location` stubbed through `vi.stubGlobal`.
 *
 * Every board fixture parses under `web/src/features/admin/killswitch/schema.ts`,
 * and every toggle success answers `setKillSwitchResultSchema`'s four
 * fields. Both parsers are pinned by `tests/killswitch-schema.test.ts` and only
 * CONSUMED here. Obviously fake actors and reasons throughout.
 */

const ORIGIN = "http://localhost:3000";
const BOARD_PATH = "/api/admin/killswitch";
const LOGIN_URL = "http://localhost:3000/login";

/** The tool's own cap, applied at the route. The page adds no second. */
const REASON_MAX_LENGTH = 500;

const TOOL = "request_access";
const OTHER_TOOL = "grant_access";

/** The reason every case that expects a call types into the row's field. */
const REASON = "Obviously fake reason.";

/** The four board columns, plus the fifth for the controls. */
const TOOL_CELL = 0;
const ACTIONS_CELL = 4;

const GLOBAL_STATE: Record<string, unknown> = {
  reason: "Fake platform-wide stop.",
  actor: "fake-admin-one",
  at: "2026-09-12T08:00:00+00:00",
};

const TENANT_STATE: Record<string, unknown> = {
  reason: "Fake tenant-only stop.",
  actor: "fake-admin-two",
  at: "2026-09-11T17:30:45+00:00",
};

/** The four control names, per tool. */
function stopGlobal(tool: string): string {
  return `Stop platform-wide for ${tool}`;
}
function releaseGlobal(tool: string): string {
  return `Release platform-wide for ${tool}`;
}
function stopTenant(tool: string): string {
  return `Stop for this tenant for ${tool}`;
}
function releaseTenant(tool: string): string {
  return `Release for this tenant for ${tool}`;
}
function confirmGlobal(tool: string): string {
  return `Confirm stop platform-wide for ${tool}`;
}
function reasonField(tool: string): string {
  return `Reason for ${tool}`;
}

function row(overrides: Record<string, unknown>): Record<string, unknown> {
  return {
    tool: TOOL,
    protected: false,
    stopped: false,
    global_state: null,
    tenant_state: null,
    unreadable: false,
    ...overrides,
  };
}

/**
 * Neither layer set, so both controls read `Stop`. `stopped` is deliberately
 * TRUE while both layers are null: a toggle deriving its verbs from `stopped`
 * renders `Release` here, which is the merge the board must not make.
 */
const ROW_NEITHER = row({ tool: TOOL, stopped: true });

/**
 * Both layers held, so both controls read `Release`, with `stopped` FALSE for
 * the mirror reason.
 */
const ROW_BOTH = row({
  tool: TOOL,
  stopped: false,
  global_state: GLOBAL_STATE,
  tenant_state: TENANT_STATE,
});

/** A second tool, so "no OTHER row's controls" is assertable. */
const ROW_OTHER = row({ tool: OTHER_TOOL, stopped: true });

const fetchStub = vi.fn<typeof fetch>();
const assign = vi.fn<(url: string) => void>();

type Answer = () => Response | Promise<Response>;

let boardAnswer: Answer = () => board([ROW_NEITHER]);
let toggleAnswer: Answer = () => setResult();

function jsonResponse(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/** The board in `killSwitchBoardSchema`'s envelope. */
function board(switches: Record<string, unknown>[]): Response {
  return jsonResponse({ switches }, 200);
}

/** A toggle answer in `setKillSwitchResultSchema`'s shape: all four fields. */
function setResult(overrides: Record<string, unknown> = {}): Response {
  return jsonResponse(
    {
      tool_name: TOOL,
      stopped: true,
      scope: "global",
      changed_at: "2026-09-13T09:00:00+00:00",
      ...overrides,
    },
    200,
  );
}

/**
 * The response a browser is handed after FOLLOWING the proxy's redirect to
 * `/login`: a 200 carrying the login page, with `redirected` true and `url`
 * pointing at where it ended up. `Response` has no constructor option for
 * either, so both are defined on the instance.
 */
function redirectedResponse(): Response {
  const response = new Response("<html>login</html>", {
    status: 200,
    headers: { "content-type": "text/html" },
  });
  Object.defineProperty(response, "redirected", { value: true, configurable: true });
  Object.defineProperty(response, "url", { value: LOGIN_URL, configurable: true });
  return response;
}

function urlOf(input: RequestInfo | URL): string {
  if (typeof input === "string") {
    return input;
  }
  if (input instanceof URL) {
    return input.toString();
  }
  return input.url;
}

function methodOf(init: RequestInit | undefined): string {
  return (init?.method ?? "GET").toUpperCase();
}

interface Call {
  url: string;
  init: RequestInit;
}

/** Every call the page made to the proxy with this verb, in order. */
function callsWith(method: string): Call[] {
  return fetchStub.mock.calls
    .map(([input, init]) => ({ url: urlOf(input), init: init ?? {} }))
    .filter((call) => call.url.includes(BOARD_PATH) && methodOf(call.init) === method);
}

function boardCalls(): Call[] {
  return callsWith("GET");
}

function toggleCalls(): Call[] {
  return callsWith("POST");
}

function only(calls: Call[]): Call {
  const call = calls[0];
  if (calls.length !== 1 || call === undefined) {
    throw new Error(`expected exactly one call, found ${calls.length}`);
  }
  return call;
}

function bodyOf(init: RequestInit): Record<string, unknown> {
  const body = init.body;
  if (typeof body !== "string") {
    throw new Error(`expected a serialised JSON body, found ${typeof body}`);
  }
  return JSON.parse(body) as Record<string, unknown>;
}

function at<T>(values: readonly T[], index: number): T {
  const value = values[index];
  if (value === undefined) {
    throw new Error(`expected an entry at index ${index}, found ${values.length}`);
  }
  return value;
}

function renderBoard(): void {
  render(
    <QueryProvider>
      <KillSwitchBoard />
    </QueryProvider>,
  );
}

async function table(): Promise<HTMLElement> {
  const region = await screen.findByRole("region", { name: "Kill switches" });
  return await within(region).findByRole("table", { name: "Tool kill switches" });
}

/** The table's DATA rows: the ones holding cells rather than column headers. */
async function switchRows(): Promise<HTMLElement[]> {
  return within(await table())
    .getAllByRole("row")
    .filter((candidate) => within(candidate).queryAllByRole("cell").length > 0);
}

/** One row's cell at a column index, with its text collapsed. */
function cellText(rowElement: HTMLElement, column: number): string {
  const cells = within(rowElement).getAllByRole("cell");
  return (at(cells, column).textContent ?? "").trim();
}

/** The one data row whose `Tool` cell names this tool. */
async function rowFor(tool: string): Promise<HTMLElement> {
  const rows = await switchRows();
  const match = rows.filter((rowElement) => cellText(rowElement, TOOL_CELL).includes(tool));
  if (match.length !== 1) {
    throw new Error(`expected exactly one row for ${tool}, found ${match.length}`);
  }
  return at(match, 0);
}

/** Type a reason into the row's own field, the way every posting case must. */
async function typeReason(
  user: ReturnType<typeof userEvent.setup>,
  tool: string,
  text: string,
): Promise<void> {
  const target = await rowFor(tool);
  await user.type(within(target).getByRole("textbox", { name: reasonField(tool) }), text);
}

/** Click a control by its accessible name, re-found in its row every time. */
async function clickControl(
  user: ReturnType<typeof userEvent.setup>,
  tool: string,
  name: string,
): Promise<void> {
  const target = await rowFor(tool);
  await user.click(within(target).getByRole("button", { name }));
}

beforeEach(() => {
  boardAnswer = () => board([ROW_NEITHER]);
  toggleAnswer = () => setResult();
  assign.mockReset();
  fetchStub.mockReset();
  fetchStub.mockImplementation(async (input, init) => {
    const url = urlOf(input);
    if (!url.includes(BOARD_PATH)) {
      throw new Error(`unexpected fetch to ${url}`);
    }
    return methodOf(init) === "POST" ? await toggleAnswer() : await boardAnswer();
  });
  vi.stubGlobal("fetch", fetchStub);
  vi.stubGlobal("location", {
    ...window.location,
    href: `${ORIGIN}/admin/killswitch`,
    assign,
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("KillSwitchToggle — what a row offers", () => {
  it("puts exactly two controls and one reason field in the fifth cell", async () => {
    // Counted inside the row and inside the new cell.
    // Kills a toggle that renders all FOUR verbs per row — a Stop and a Release
    // side by side on one layer, where an admin clicking the wrong one releases
    // a tool the platform meant to hold down — and kills one that renders a
    // single Stop/Release pair for a merged state, which is the whole
    // point (there would then be no way to say which scope a click meant). The
    // field count kills a shared reason box hoisted to the board, where one
    // admin's typed reason would be filed against whichever tool they clicked
    // next. Scoped to the ACTIONS cell as well as to the row, so a control
    // smuggled into a layer cell (a rejected design, which makes
    // the positional assertions read "No switch Stop platform-wide") is red
    // here rather than in the neighbouring file.
    boardAnswer = () => board([ROW_NEITHER]);
    renderBoard();

    const target = await rowFor(TOOL);
    expect(within(target).getAllByRole("button")).toHaveLength(2);
    expect(within(target).getAllByRole("textbox")).toHaveLength(1);

    const cells = within(target).getAllByRole("cell");
    const actions = at(cells, ACTIONS_CELL);
    expect(within(actions).getAllByRole("button")).toHaveLength(2);
    expect(within(actions).getAllByRole("textbox")).toHaveLength(1);
  });

  const VERBS: ReadonlyArray<{
    name: string;
    entry: Record<string, unknown>;
    present: readonly string[];
    absent: readonly string[];
  }> = [
    {
      name: "the platform layer held and the tenant layer null",
      // `stopped` true, so a toggle reading it renders Release on BOTH layers.
      entry: row({ tool: TOOL, stopped: true, global_state: GLOBAL_STATE }),
      present: [releaseGlobal(TOOL), stopTenant(TOOL)],
      absent: [stopGlobal(TOOL), releaseTenant(TOOL)],
    },
    {
      name: "the tenant layer held and the platform layer null",
      // `stopped` false, so a toggle reading it renders Stop on BOTH layers.
      entry: row({ tool: TOOL, stopped: false, tenant_state: TENANT_STATE }),
      present: [stopGlobal(TOOL), releaseTenant(TOOL)],
      absent: [releaseGlobal(TOOL), stopTenant(TOOL)],
    },
    {
      name: "both layers held",
      entry: ROW_BOTH,
      present: [releaseGlobal(TOOL), releaseTenant(TOOL)],
      absent: [stopGlobal(TOOL), stopTenant(TOOL)],
    },
    {
      name: "neither layer set",
      entry: ROW_NEITHER,
      present: [stopGlobal(TOOL), stopTenant(TOOL)],
      absent: [releaseGlobal(TOOL), releaseTenant(TOOL)],
    },
  ];

  it.each(VERBS)(
    "derives each verb from its own layer with $name",
    async ({ entry, present, absent }) => {
      // Parametrised over all four layer combinations, and
      // arranged so ONLY the per-layer derivation passes every row. Each fixture
      // carries a `stopped` that CONTRADICTS the layer it would be mistaken for:
      // the first row is stopped with a null tenant layer, so a toggle reading `stopped`
      // offers `Release for this tenant` for a tenant switch that holds nothing,
      // and the admin's click clears nothing while the platform keeps the tool
      // down; the second row is not stopped with a tenant layer present, so the same
      // toggle offers `Stop for this tenant` on a layer already held and the row
      // never changes. The third and fourth rows pin the two unambiguous ends, so a toggle
      // that renders one fixed pair of verbs regardless is red too. The ABSENCE
      // of the opposite name is asserted in the same row because the presence
      // assertion alone is green for a toggle rendering all four verbs — which
      // the count case above is the other half of.
      boardAnswer = () => board([entry]);
      renderBoard();

      const target = await rowFor(TOOL);
      for (const name of present) {
        expect(within(target).getByRole("button", { name })).toBeInTheDocument();
      }
      for (const name of absent) {
        expect(within(target).queryByRole("button", { name })).toBeNull();
      }
    },
  );

  it("renders Release on both layers for an unreadable row", async () => {
    // The fixture is what the tool
    // actually produces — both stored states null, `stopped` true, `unreadable`
    // true — so the NORMAL derivation would read "no switch on either layer"
    // and offer two Stops. That is the wrong offer in the one situation an
    // admin is looking at this row for: the tool is already denied on every
    // call and the only act that clears a corrupt value is a delete, which is
    // what a Release posts. Kills the toggle that treats an unreadable row like
    // any other null-layer row, and it cannot be satisfied by a `stopped`-based
    // derivation either, because the verb-derivation rows above forbid that.
    boardAnswer = () => board([row({ tool: TOOL, stopped: true, unreadable: true })]);
    renderBoard();

    const target = await rowFor(TOOL);
    expect(within(target).getByRole("button", { name: releaseGlobal(TOOL) })).toBeInTheDocument();
    expect(within(target).getByRole("button", { name: releaseTenant(TOOL) })).toBeInTheDocument();
    expect(within(target).queryByRole("button", { name: stopGlobal(TOOL) })).toBeNull();
    expect(within(target).queryByRole("button", { name: stopTenant(TOOL) })).toBeNull();
  });

  const PROTECTION: ReadonlyArray<{
    name: string;
    entry: Record<string, unknown>;
    enabled: boolean;
  }> = [
    {
      // `stopped` false, so nothing but `protected` can disable this row.
      name: "a protected row",
      entry: row({ tool: TOOL, protected: true, stopped: false }),
      enabled: false,
    },
    {
      // `stopped` true, so a toggle disabling stopped rows is red here.
      name: "an unprotected row",
      entry: row({ tool: TOOL, protected: false, stopped: true }),
      enabled: true,
    },
  ];

  it.each(PROTECTION)(
    "renders both controls disabled=$enabled for $name",
    async ({ entry, enabled }) => {
      // Both directions, one case each because the
      // disabled half alone is green for a page that disables every control it
      // renders. Read off the ROW's own `protected` and never off a browser-side
      // copy of `PROTECTED_TOOLS` — a second copy of a security-relevant list is
      // exactly what an implementer writes when nothing on the row says so, and
      // it goes stale the day the list changes on the server. The two fixtures
      // set `stopped` against `protected`, so a toggle disabling on the wrong
      // field flips both rows rather than neither.
      boardAnswer = () => board([entry]);
      renderBoard();

      const target = await rowFor(TOOL);
      const globalControl = within(target).getByRole("button", { name: stopGlobal(TOOL) });
      const tenantControl = within(target).getByRole("button", { name: stopTenant(TOOL) });
      if (enabled) {
        expect(globalControl).toBeEnabled();
        expect(tenantControl).toBeEnabled();
      } else {
        expect(globalControl).toBeDisabled();
        expect(tenantControl).toBeDisabled();
      }
    },
  );
});

describe("KillSwitchToggle — the body each control posts", () => {
  const BODIES: ReadonlyArray<{
    name: string;
    entry: Record<string, unknown>;
    clicks: readonly string[];
    body: Record<string, unknown>;
  }> = [
    {
      name: "a Stop platform-wide",
      entry: ROW_NEITHER,
      clicks: [stopGlobal(TOOL), confirmGlobal(TOOL)],
      body: { tool_name: TOOL, enabled: true, scope: "global", reason: REASON },
    },
    {
      name: "a Release platform-wide",
      entry: ROW_BOTH,
      clicks: [releaseGlobal(TOOL)],
      body: { tool_name: TOOL, enabled: false, scope: "global", reason: REASON },
    },
    {
      name: "a Stop for this tenant",
      entry: ROW_NEITHER,
      clicks: [stopTenant(TOOL)],
      body: { tool_name: TOOL, enabled: true, scope: "tenant", reason: REASON },
    },
    {
      name: "a Release for this tenant",
      entry: ROW_BOTH,
      clicks: [releaseTenant(TOOL)],
      body: { tool_name: TOOL, enabled: false, scope: "tenant", reason: REASON },
    },
  ];

  it.each(BODIES)("$name posts its exact four-key body", async ({ entry, clicks, body }) => {
    // Several checks at once.
    // The body is asserted as a WHOLE object, so a fifth key is red: a page
    // sending `tenant_id` would trust the caller's tenant in the browser (the
    // router substitutes the token's own) and `SetKillSwitchBody`
    // forbids extras, so it arrives as a 400 an admin reads as a broken page; a
    // page sending an idempotency key would be inventing one that
    // says nothing derives. All four rows together are the polarity kill:
    // `enabled` describes the SWITCH and `stopped` the TOOL,
    // so a page that inverted it would RELEASE a tool an admin just stopped,
    // silently, with a 200 — and one row alone cannot see that, because a
    // constant `enabled: true` passes the two Stops. The two `scope` literals
    // are pinned the same way: `"tenant"` is the vocabulary the tool takes, and
    // a page sending a tenant id or `"local"` there is refused by a surface
    // this test never reaches.
    boardAnswer = () => board([entry]);
    const user = userEvent.setup();
    renderBoard();

    await typeReason(user, TOOL, REASON);
    for (const name of clicks) {
      await clickControl(user, TOOL, name);
    }

    await waitFor(() => {
      expect(toggleCalls()).toHaveLength(1);
    });
    const call = only(toggleCalls());
    expect(new URL(call.url, ORIGIN).pathname).toBe(BOARD_PATH);
    expect(bodyOf(call.init)).toEqual(body);
  });

  it("sends a 500 character reason whole, with no maxLength on the field", async () => {
    // Input validation, following the earlier precedent. Kills a
    // reason box carrying `maxLength={500}` or any lower bound of its own: the
    // browser would hand the tool a silently truncated reason, and the stored
    // switch — the only record of why a tool was stopped for everybody — would
    // carry a sentence the admin did not write, with no error anywhere. The
    // exact length is the tool's inclusive cap, so a page
    // slicing to 499 or to a round number is red, and the absent attribute is
    // asserted directly because a page truncating on submit instead would pass
    // the length assertion only by not truncating at all.
    const reason = "x".repeat(REASON_MAX_LENGTH);
    boardAnswer = () => board([ROW_NEITHER]);
    const user = userEvent.setup();
    renderBoard();

    const target = await rowFor(TOOL);
    const field = within(target).getByRole("textbox", { name: reasonField(TOOL) });
    expect(field).not.toHaveAttribute("maxlength");

    fireEvent.change(field, { target: { value: reason } });
    await clickControl(user, TOOL, stopTenant(TOOL));

    await waitFor(() => {
      expect(toggleCalls()).toHaveLength(1);
    });
    const sent = bodyOf(only(toggleCalls()).init).reason;
    expect(sent).toBe(reason);
    expect(String(sent)).toHaveLength(REASON_MAX_LENGTH);
  });
});

describe("KillSwitchToggle — the misclick guard on the platform-wide stop", () => {
  it("posts nothing on the first click and renames the control to confirm", async () => {
    // The arming half. This is the only
    // click in the product that stops a tool for EVERY tenant, and the kill is
    // the absent call: a toggle with no arming posts on this click, and the
    // assertion that the call list is empty is what catches it — the renamed
    // control alone would not, since a page could both post and rename. The
    // confirm name is asserted as present AND the unarmed name as absent, so a
    // page adding a second button beside the first (two live platform-wide
    // stops in one row) is red too.
    boardAnswer = () => board([ROW_NEITHER]);
    const user = userEvent.setup();
    renderBoard();

    await typeReason(user, TOOL, REASON);
    await clickControl(user, TOOL, stopGlobal(TOOL));

    expect(toggleCalls()).toHaveLength(0);
    const target = await rowFor(TOOL);
    expect(within(target).getByRole("button", { name: confirmGlobal(TOOL) })).toBeInTheDocument();
    expect(within(target).queryByRole("button", { name: stopGlobal(TOOL) })).toBeNull();
  });

  it("posts exactly once on the second click", async () => {
    // The other half of the guard. Kills a guard that arms and never fires — an
    // admin who needs to stop a tool platform-wide during an incident would
    // click twice and nothing would happen — and kills one that posts on BOTH
    // clicks, which is the shape an implementer produces by renaming the label
    // without gating the mutation: the exact count is what sees it, because the
    // body assertions elsewhere in this file read the FIRST call and would be
    // green with a duplicate behind it.
    boardAnswer = () => board([ROW_NEITHER]);
    const user = userEvent.setup();
    renderBoard();

    await typeReason(user, TOOL, REASON);
    await clickControl(user, TOOL, stopGlobal(TOOL));
    await clickControl(user, TOOL, confirmGlobal(TOOL));

    await waitFor(() => {
      expect(toggleCalls()).toHaveLength(1);
    });
    expect(bodyOf(only(toggleCalls()).init).scope).toBe("global");
  });

  it("clears the arming when another control in the same row is clicked", async () => {
    // Last sentence. Kills a toggle whose armed flag
    // survives an unrelated click: the admin arms the platform-wide stop,
    // changes their mind, stops the tool for their own tenant instead, and the
    // NEXT click on the platform-wide control — which still reads `Confirm` —
    // stops the tool for every tenant on one click. The assertion is that the
    // control is found by its UNARMED name again, which a page that merely
    // hides the confirm label without resetting the flag cannot satisfy, and
    // the single POST pins that the intervening click was the tenant one.
    boardAnswer = () => board([ROW_NEITHER]);
    const user = userEvent.setup();
    renderBoard();

    await typeReason(user, TOOL, REASON);
    await clickControl(user, TOOL, stopGlobal(TOOL));
    await clickControl(user, TOOL, stopTenant(TOOL));

    await waitFor(() => {
      expect(toggleCalls()).toHaveLength(1);
    });
    expect(bodyOf(only(toggleCalls()).init).scope).toBe("tenant");
    const target = await rowFor(TOOL);
    expect(within(target).getByRole("button", { name: stopGlobal(TOOL) })).toBeInTheDocument();
    expect(within(target).queryByRole("button", { name: confirmGlobal(TOOL) })).toBeNull();
  });

  const UNARMED: ReadonlyArray<{ name: string; entry: Record<string, unknown>; control: string }> =
    [
      { name: "a Release platform-wide", entry: ROW_BOTH, control: releaseGlobal(TOOL) },
      { name: "a Stop for this tenant", entry: ROW_NEITHER, control: stopTenant(TOOL) },
      { name: "a Release for this tenant", entry: ROW_BOTH, control: releaseTenant(TOOL) },
    ];

  it.each(UNARMED)("$name posts on its FIRST click", async ({ entry, control }) => {
    // The arming applies to the platform-wide STOP
    // alone. Kills a guard written over the whole row rather than over one
    // control — making an admin click twice to bring a platform back up is a
    // cost paid during an incident, when the release is the act everyone is
    // waiting on, and a second click on a tenant-scope stop guards against
    // nothing. The kill is specifically the single click: the four-body cases
    // above click these three controls once too, but they only assert WHAT was
    // posted, so a toggle that armed everything would fail them for a reason
    // that reads as a body fault. This says the count after one click.
    boardAnswer = () => board([entry]);
    const user = userEvent.setup();
    renderBoard();

    await typeReason(user, TOOL, REASON);
    await clickControl(user, TOOL, control);

    await waitFor(() => {
      expect(toggleCalls()).toHaveLength(1);
    });
    const target = await rowFor(TOOL);
    expect(within(target).queryByRole("button", { name: confirmGlobal(TOOL) })).toBeNull();
  });
});

describe("KillSwitchToggle — what a successful toggle does to the board", () => {
  it("re-reads the board, rewrites nothing before the answer and clears the reason", async () => {
    // All three halves. The second read is
    // held open, so the moment AFTER the toggle succeeded and BEFORE the
    // refetch answered is observable: a page flipping the row optimistically
    // shows a stop as landed that the tool may not have applied — on the one
    // screen an operator uses to decide whether a tool is actually down — so
    // the row is asserted to still offer `Stop for this tenant` at that moment.
    // Kills a mutation with no `onChanged` / no invalidation at all, where the
    // row would keep offering a Stop until the admin reloaded by hand. The
    // cleared field kills a page that leaves the last reason in the box, where
    // the next tool stopped from this screen is filed with the previous tool's
    // reason. Arithmetic: board GETs = 2 (paint, then the
    // invalidation), POSTs = 1.
    let releaseRead: (value: Response) => void = () => {};
    let reads = 0;
    boardAnswer = () => {
      reads += 1;
      if (reads === 1) {
        return board([ROW_NEITHER]);
      }
      return new Promise<Response>((resolve) => {
        releaseRead = resolve;
      });
    };
    const user = userEvent.setup();
    renderBoard();

    await typeReason(user, TOOL, REASON);
    await clickControl(user, TOOL, stopTenant(TOOL));

    await waitFor(() => {
      expect(boardCalls()).toHaveLength(2);
    });
    expect(
      within(await rowFor(TOOL)).getByRole("button", { name: stopTenant(TOOL) }),
    ).toBeInTheDocument();

    releaseRead(board([row({ tool: TOOL, stopped: true, tenant_state: TENANT_STATE })]));
    await waitFor(async () => {
      expect(
        within(await rowFor(TOOL)).getByRole("button", { name: releaseTenant(TOOL) }),
      ).toBeInTheDocument();
    });
    expect(
      within(await rowFor(TOOL)).getByRole("textbox", { name: reasonField(TOOL) }),
    ).toHaveValue("");
    expect(toggleCalls()).toHaveLength(1);
  });

  it("disables only the acting row's controls while its toggle is in flight", async () => {
    // One component instance per
    // row. Kills a board holding ONE in-flight flag for every row: an admin
    // stopping one tool during an incident would find every other tool's
    // controls greyed out, including the releases they need next. Kills a page
    // with no flag at all too, where the other control in the SAME row is still
    // live and a second click files a contradictory change against the tool
    // that is mid-write. The POST is held open by an unresolved promise, so the
    // in-flight moment is observable rather than raced.
    boardAnswer = () => board([ROW_NEITHER, ROW_OTHER]);
    let release: (value: Response) => void = () => {};
    toggleAnswer = () =>
      new Promise<Response>((resolve) => {
        release = resolve;
      });
    const user = userEvent.setup();
    renderBoard();

    await typeReason(user, TOOL, REASON);
    await clickControl(user, TOOL, stopTenant(TOOL));

    await waitFor(async () => {
      expect(
        within(await rowFor(TOOL)).getByRole("button", { name: stopTenant(TOOL) }),
      ).toBeDisabled();
    });
    const acting = await rowFor(TOOL);
    expect(within(acting).getByRole("button", { name: stopGlobal(TOOL) })).toBeDisabled();
    const other = await rowFor(OTHER_TOOL);
    expect(within(other).getByRole("button", { name: stopGlobal(OTHER_TOOL) })).toBeEnabled();
    expect(within(other).getByRole("button", { name: stopTenant(OTHER_TOOL) })).toBeEnabled();

    release(setResult({ tool_name: TOOL, scope: "tenant" }));
    await waitFor(async () => {
      expect(
        within(await rowFor(TOOL)).getByRole("button", { name: stopGlobal(TOOL) }),
      ).toBeEnabled();
    });
  });
});

describe("KillSwitchToggle — a reason the page refuses for itself", () => {
  const BLANK: ReadonlyArray<{ name: string; reason: string }> = [
    { name: "an empty reason", reason: "" },
    { name: "a whitespace-only reason", reason: "   " },
  ];

  it.each(BLANK)("$name makes no call at all and says so in the row", async ({ reason }) => {
    // A floor the page owns, and never a second cap.
    // Kills a toggle that posts whatever is in the box — the tool would answer
    // `VALIDATION`, the admin would read an error for something the page knew
    // before it asked, and a round trip through the proxy would be spent on it.
    // The whitespace row is the load-bearing one: a `reason === ""` check lets
    // three spaces through and files a switch nobody can explain later, so only
    // a page that TRIMS is green. The assertion is the ABSENCE of a call, not
    // the presence of a message, because a page that both warns and posts has
    // not satisfied the criterion; the board read count pins that nothing else
    // moved either.
    boardAnswer = () => board([ROW_NEITHER]);
    const user = userEvent.setup();
    renderBoard();

    if (reason !== "") {
      await typeReason(user, TOOL, reason);
    }
    await clickControl(user, TOOL, stopTenant(TOOL));

    expect(toggleCalls()).toHaveLength(0);
    expect(boardCalls()).toHaveLength(1);
    const target = await rowFor(TOOL);
    expect(await within(target).findByRole("alert")).toHaveTextContent(
      KILL_SWITCH_REASON_REQUIRED_MESSAGE,
    );
  });
});

describe("KillSwitchToggle — a refused toggle", () => {
  const REFUSALS: ReadonlyArray<{
    name: string;
    status: number;
    code: string;
    sentence: string;
    shared: string | null;
  }> = [
    {
      name: "FORBIDDEN",
      status: 403,
      code: "FORBIDDEN",
      sentence: FAILURE_MESSAGES.FORBIDDEN,
      shared: null,
    },
    {
      name: "POLICY_BLOCKED",
      status: 409,
      code: "POLICY_BLOCKED",
      sentence: KILL_SWITCH_POLICY_BLOCKED_MESSAGE,
      shared: FAILURE_MESSAGES.POLICY_BLOCKED,
    },
    {
      name: "VALIDATION",
      status: 400,
      code: "VALIDATION",
      sentence: KILL_SWITCH_VALIDATION_MESSAGE,
      shared: FAILURE_MESSAGES.VALIDATION,
    },
  ];

  it.each(REFUSALS)(
    "$name renders its sentence in the acting row and leaves the board alone",
    async ({ status, code, sentence, shared }) => {
      // One case per pinned code.
      // Kills a page rendering the CODE at the admin ("POLICY_BLOCKED" means
      // nothing to the person reading it), and kills one answering every
      // refusal with `KILL_SWITCH_TOGGLE_MESSAGE` — a principal who is not a
      // `platform_admin` would be told to "try again in a moment" for something
      // no retry fixes. The FORBIDDEN sentence is READ from `FAILURE_MESSAGES`,
      // so a page inventing its own wording is red without widening the shared
      // pinned map; the other two rows assert the override AND the absence of
      // the shared string, which is the whole point — the shared
      // `POLICY_BLOCKED` sentence is about chat rate limiting ("Too many
      // messages just now.") and would tell an admin to wait for a refusal that
      // is a permanent property of a protected tool, and the shared
      // `VALIDATION` one is about a chat message. The board read count is the
      // other kill: a refusal that invalidated anyway would repaint the row as
      // though something had happened.
      boardAnswer = () => board([ROW_NEITHER]);
      toggleAnswer = () => jsonResponse({ code }, status);
      const user = userEvent.setup();
      renderBoard();

      await typeReason(user, TOOL, REASON);
      await clickControl(user, TOOL, stopTenant(TOOL));

      const target = await rowFor(TOOL);
      const alert = await within(target).findByRole("alert");
      expect(alert).toHaveTextContent(sentence);
      if (shared !== null) {
        expect(alert.textContent ?? "").not.toContain(shared);
      }
      expect(boardCalls()).toHaveLength(1);
    },
  );

  const BROKEN: ReadonlyArray<{ name: string; answer: Answer }> = [
    {
      name: "a body that is not JSON at all",
      answer: () =>
        new Response("<html>bad gateway</html>", {
          status: 200,
          headers: { "content-type": "text/html" },
        }),
    },
    {
      name: "a 200 the set-result parser refuses",
      answer: () =>
        jsonResponse(
          { tool_name: TOOL, scope: "tenant", changed_at: "2026-09-13T09:00:00+00:00" },
          200,
        ),
    },
    {
      name: "the proxy's own 502",
      answer: () => jsonResponse({ code: "UPSTREAM_UNAVAILABLE" }, 502),
    },
  ];

  it.each(BROKEN)("$name renders the toggle message and re-reads nothing", async ({ answer }) => {
    // From three directions, all
    // killing one implementation: a toggle that treats any answer it did not
    // understand as a success and invalidates the board. The admin would then
    // be shown a freshly re-read board as evidence that a stop landed, on the
    // strength of a reply that never said so. The first row kills an unguarded
    // `response.json()`, where the parse error escapes as an unhandled
    // rejection instead of a sentence. The second row is a set result missing `stopped`
    // alone, so it kills a page that CASTS the body instead of running
    // `safeParse` — which is exactly why the schema exists. The third row is the
    // refusal-versus-failure discriminator: `UPSTREAM_UNAVAILABLE` is
    // not one of the eight typed error codes, so it is a failed toggle and not a
    // typed refusal, and a page answering `failureMessage(code)` here shows
    // "Something went wrong. Nothing was changed." — red against this constant.
    boardAnswer = () => board([ROW_NEITHER]);
    toggleAnswer = answer;
    const user = userEvent.setup();
    renderBoard();

    await typeReason(user, TOOL, REASON);
    await clickControl(user, TOOL, stopTenant(TOOL));

    const target = await rowFor(TOOL);
    const alert = await within(target).findByRole("alert");
    expect(alert).toHaveTextContent(KILL_SWITCH_TOGGLE_MESSAGE);
    expect(boardCalls()).toHaveLength(1);
  });
});

describe("KillSwitchToggle — an expired session at the toggle", () => {
  it("follows the proxy's redirect to /login, with no alert and no re-read", async () => {
    // And the half a fetcher fixed only at the read
    // misses: a board sits open on a screen for minutes, so a session expiring
    // BETWEEN the paint and the click is the common case. The read here
    // SUCCEEDS, so only the POST path can make the assignment happen and no
    // read-side redirect handling can stand in for it. Kills a mutation
    // checking `response.ok` or `response.status === 401` — the browser has
    // already followed the proxy's redirect, so what arrives is a 200 carrying
    // the login page; that page then parses the HTML, fails,
    // and shows a refusal sentence for a toggle that was never attempted. The
    // absent alert and the single board read are asserted because the other
    // wrong answer is a page that navigates AND files a failure, leaving an
    // alert flashing behind a navigation.
    boardAnswer = () => board([ROW_NEITHER]);
    toggleAnswer = redirectedResponse;
    const user = userEvent.setup();
    renderBoard();

    await typeReason(user, TOOL, REASON);
    await clickControl(user, TOOL, stopTenant(TOOL));

    await waitFor(() => {
      expect(assign).toHaveBeenCalledWith(LOGIN_URL);
    });
    expect(screen.queryByRole("alert")).toBeNull();
    expect(boardCalls()).toHaveLength(1);
  });
});

describe("KillSwitchToggle — the four sentences this surface owns", () => {
  it("pins the failure and the reason-required sentences exactly", async () => {
    // The exact literals for two of the new strings. The cases
    // above read the CONSTANTS, so they stay green for whatever value the
    // module chooses; only these literals fix them. Kills an implementation
    // that exports the names and fills them with a reworded variant, and the
    // inequality kills the lazier fault of pointing either at the shared map.
    expect(KILL_SWITCH_TOGGLE_MESSAGE).toBe(
      "The kill switch could not be changed. Try again in a moment.",
    );
    expect(KILL_SWITCH_REASON_REQUIRED_MESSAGE).toBe(
      "Give a reason before changing a kill switch.",
    );
    expect(KILL_SWITCH_TOGGLE_MESSAGE).not.toBe(KILL_SWITCH_REASON_REQUIRED_MESSAGE);
  });

  it("pins the two override sentences exactly and apart from the shared map", async () => {
    // The two override values, verbatim. Kills an implementation that
    // exports the two names but assigns them `FAILURE_MESSAGES.POLICY_BLOCKED`
    // and `FAILURE_MESSAGES.VALIDATION` — the parametrised refusal rows would
    // then pass their positive assertion and only their `not.toContain` half
    // would see it, and that half is asserted against the same map this case
    // reads. Written as its own case so a reword is red HERE, next to the
    // value, rather than inside a case about an alert.
    expect(KILL_SWITCH_POLICY_BLOCKED_MESSAGE).toBe(
      "That tool is protected and cannot be stopped.",
    );
    expect(KILL_SWITCH_VALIDATION_MESSAGE).toBe(
      "That could not be saved. Shorten the reason and try again.",
    );
    expect(KILL_SWITCH_POLICY_BLOCKED_MESSAGE).not.toBe(FAILURE_MESSAGES.POLICY_BLOCKED);
    expect(KILL_SWITCH_VALIDATION_MESSAGE).not.toBe(FAILURE_MESSAGES.VALIDATION);
  });
});

describe("KillSwitchToggle — the vocabulary check", () => {
  it("renders the shared TOOL_DISABLED sentence for a typed refusal", async () => {
    // The half that must stay TRUE when this file's
    // private `isTypedCode` becomes `lib/wire-read.ts`'s import: a code that IS
    // one of the eight typed error codes still reaches `ToggleRefusedError` and still
    // renders that code's shared sentence. `TOOL_DISABLED` is the row no
    // existing case covers — the three pinned ones are `FORBIDDEN` and the two
    // overrides — so this kills a predicate narrowed to the codes the other
    // rows happen to name, which would answer "The kill switch could not be
    // changed. Try again in a moment." for a route deliberately switched off
    // and send an admin retrying a stop that can never land. The sentence is
    // READ from the shared map, so the page cannot satisfy it by inventing
    // wording, and the board read count kills a refusal that invalidates
    // anyway.
    boardAnswer = () => board([ROW_NEITHER]);
    toggleAnswer = () => jsonResponse({ code: "TOOL_DISABLED" }, 503);
    const user = userEvent.setup();
    renderBoard();

    await typeReason(user, TOOL, REASON);
    await clickControl(user, TOOL, stopTenant(TOOL));

    const target = await rowFor(TOOL);
    const alert = await within(target).findByRole("alert");
    expect(alert).toHaveTextContent(FAILURE_MESSAGES.TOOL_DISABLED);
    expect(alert.textContent ?? "").not.toContain(KILL_SWITCH_TOGGLE_MESSAGE);
    expect(boardCalls()).toHaveLength(1);
  });

  it("treats the map's fallback KEY as no code at all", async () => {
    // The other half, and the one assertion that separates the real
    // predicate from the lazy one. `default` is `FAILURE_MESSAGES`' fallback
    // KEY and never a wire value, so `code in FAILURE_MESSAGES` alone answers
    // TRUE for it: the existing `UPSTREAM_UNAVAILABLE` row cannot see that,
    // because that code is outside the map entirely and both predicates refuse
    // it. This kills a swap that drops the `code !== "default"` guard — the
    // page would dress a body carrying the fallback key as a typed refusal and
    // print "Something went wrong. Nothing was changed.", a claim that the
    // stop did NOT land, for an answer nobody validated. The `not.toContain`
    // is what makes the two sentences tell apart.
    boardAnswer = () => board([ROW_NEITHER]);
    toggleAnswer = () => jsonResponse({ code: "default" }, 502);
    const user = userEvent.setup();
    renderBoard();

    await typeReason(user, TOOL, REASON);
    await clickControl(user, TOOL, stopTenant(TOOL));

    const target = await rowFor(TOOL);
    const alert = await within(target).findByRole("alert");
    expect(alert).toHaveTextContent(KILL_SWITCH_TOGGLE_MESSAGE);
    expect(alert.textContent ?? "").not.toContain(FAILURE_MESSAGES.default);
    expect(boardCalls()).toHaveLength(1);
  });
});
