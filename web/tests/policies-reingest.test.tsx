import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PoliciesBoard } from "@/components/admin/policies-board";
import {
  REINGEST_CREATED_TEXT,
  REINGEST_FAILED_MESSAGE,
  REINGEST_NOT_FOUND_MESSAGE,
  REINGEST_UNCHANGED_TEXT,
  REINGEST_VALIDATION_MESSAGE,
} from "@/components/admin/policies-reingest";
import { QueryProvider } from "@/components/query-provider";
import { FAILURE_MESSAGES } from "@/lib/chat-errors";
import { NOT_LIVE_VERSION_TEXT } from "@/lib/policies-format";

/**
 * An earlier change §6, `policies-reingest.test.tsx`: the per-version re-ingest control —
 * one control per distinct version, an arming on the dangerous direction, one
 * POST — against a stubbed `fetch`. jsdom, no agent-api, no backing service.
 * (No basename collision in the tree, a project rule: the neighbours are
 * `policies-board.test.tsx`, `policies-format.test.ts`,
 * `policies-schema.test.ts` and `policies-proxy-routes.test.ts`.)
 *
 * The control is NOT rendered standalone: it holds `useMutation` and is a child
 * of `PoliciesBoard`, so every case here renders `<PoliciesBoard />` inside
 * `QueryProvider` (§6, a project rule) against a stub that answers the board
 * on `GET /api/admin/policies` and the control on
 * `POST /api/admin/policies/reingest`. The reingest path CONTAINS the board
 * path, so every call assertion in this file discriminates on the exact
 * `pathname` and never on `includes`.
 *
 * Shapes this file fixes that the plan left open (a project rule and 55;
 * these belong in 31b's Deviations):
 * - the component is `PoliciesReingest` from
 *   `web/components/admin/policies-reingest.tsx`. No case imports it: the board
 *   is the only renderer, exactly as `killswitch-toggle.test.tsx` never imports
 *   `KillSwitchToggle`, so its PROP SHAPE stays the implementation's business
 *   and the SENTENCES are what this file imports. §2's division — the arming
 *   and the in-flight flag per version, the query key private to the board — is
 *   therefore pinned by BEHAVIOUR here (the disarm case and the in-flight case)
 *   rather than by a signature, which is the only form in which "an arming that
 *   survived an unrelated click" is even expressible.
 * - `onChanged` is observed as the board's second `GET /api/admin/policies`
 *   rather than as a spy, for the same reason: the callback is the board's and
 *   this file renders the board. §6's "`onChanged` called exactly once" reads
 *   here as "exactly two board reads, the paint and the invalidation", and a
 *   control that never invalidated makes exactly one.
 * - `REINGEST_CREATED_TEXT` is a FUNCTION `(chunkCount: number, isCurrent:
 *   boolean) => string`: §1 wants the exact `chunk_count` AND whether the
 *   version is now live in that line, and both are runtime values.
 *   `REINGEST_UNCHANGED_TEXT` is a plain string: an unchanged source has no
 *   number worth printing (`chunk_count` is 0 because nothing was written).
 *   The three refusal sentences are plain strings, pinned verbatim by the last
 *   case; the two result lines are pinned by their ARGUMENTS instead, because
 *   a sentence that ignores the count is the defect and a verbatim literal
 *   cannot see it.
 * - a POST is read off the stub's call list as `[input, init]`, with
 *   `JSON.parse(init.body)` asserted as a WHOLE object (`toEqual`), so a fourth
 *   key fails the case. The relative-versus-absolute form of the URL is not
 *   fixed; the pathname is.
 * - the control is located by its accessible name, `Re-ingest {slug}
 *   {version}`, and the armed form is `Confirm re-ingest {slug} {version},
 *   replacing the live version` (§1). Both are looked up `within` the version's
 *   own row, which is found by its `Version` cell the way
 *   `policies-board.test.tsx` finds one.
 * - a refusal's sentence is a `role="alert"` and a result line a `role="status"`,
 *   both INSIDE the acting row: the board's read failure is the region-level
 *   alert, and the two are located differently on purpose.
 * - an expired session at the click is `response.redirected` with `url` on the
 *   INSTANCE and `location` stubbed through `vi.stubGlobal`.
 * - the parametrised refusal table carries a FOURTH row, `VALIDATION`, beyond
 *   §6's three. §1 names it as one of this surface's two overrides, so the
 *   exported string would otherwise be pinned verbatim and wired to nothing.
 *
 * Every board fixture parses under `web/lib/policies-schema.ts` as 24g built
 * it, and every success answers `policyReingestResultSchema`'s eight fields.
 * Both parsers are pinned by `tests/policies-schema.test.ts` and only CONSUMED
 * here. Obviously fake slugs, titles and paths throughout.
 */

const ORIGIN = "http://localhost:3000";
const BOARD_PATH = "/api/admin/policies";
const REINGEST_PATH = "/api/admin/policies/reingest";
const LOGIN_URL = "http://localhost:3000/login";

/** The canonical UUID shape `crypto.randomUUID` answers, and its length. */
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const UUID_LENGTH = 36;

/** The six columns 24h fixed; this plan appends `Actions` at index 6. */
const CHUNKS_CELL = 3;
const STATUS_CELL = 1;

const SLUG = "fake-access-policy";
const OTHER_SLUG = "fake-zeta-policy";

const SHA_LIVE = `1111111111aa${"b".repeat(52)}`;
const SHA_OLD = `2222222222cc${"d".repeat(52)}`;

const INGESTED_AT = "2026-09-13T14:05:09+00:00";

function version(overrides: Record<string, unknown>): Record<string, unknown> {
  return {
    version: "v2",
    is_current: true,
    ingested_at: INGESTED_AT,
    chunk_count: 7,
    source_path: `fake-corpus/${SLUG}/v2.md`,
    sha256: SHA_LIVE,
    ...overrides,
  };
}

/**
 * One document holding a LIVE `v2` and a superseded `v1`: the two directions of
 * the arming, and the two controls the disarm case needs, in one payload.
 */
const TWO_VERSIONS = {
  slug: SLUG,
  title: "Fake Access Policy",
  versions: [
    version({ version: "v2", is_current: true, chunk_count: 7, sha256: SHA_LIVE }),
    version({
      version: "v1",
      is_current: false,
      chunk_count: 3,
      sha256: SHA_OLD,
      source_path: `fake-corpus/${SLUG}/v1.md`,
    }),
  ],
};

/** A second document, so "no OTHER document's control" is assertable. */
const OTHER_DOCUMENT = {
  slug: OTHER_SLUG,
  title: "Fake Zeta Policy",
  versions: [version({ version: "v1", is_current: true, sha256: SHA_OLD })],
};

const fetchStub = vi.fn<typeof fetch>();
const assign = vi.fn<(url: string) => void>();

type Answer = () => Response | Promise<Response>;

let boardAnswer: Answer = () => board([TWO_VERSIONS]);
let reingestAnswer: Answer = () => reingestResult();

function jsonResponse(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/** The corpus in `policyDocumentsBoardSchema`'s envelope. */
function board(documents: Record<string, unknown>[]): Response {
  return jsonResponse({ documents }, 200);
}

/** A re-ingest answer in `policyReingestResultSchema`'s shape: all eight. */
function reingestResult(overrides: Record<string, unknown> = {}): Response {
  return jsonResponse(
    {
      slug: SLUG,
      version: "v2",
      created: true,
      chunk_count: 7,
      is_current: true,
      sha256: SHA_LIVE,
      ingested_at: INGESTED_AT,
      summary: "Fake summary, tests only.",
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

function pathOf(input: RequestInfo | URL): string {
  return new URL(urlOf(input), ORIGIN).pathname;
}

interface Call {
  url: string;
  init: RequestInit;
}

/** Every call the page made to one exact pathname, in order. */
function callsTo(path: string): Call[] {
  return fetchStub.mock.calls
    .map(([input, init]) => ({ input, url: urlOf(input), init: init ?? {} }))
    .filter((call) => pathOf(call.input) === path)
    .map(({ url, init }) => ({ url, init }));
}

function boardCalls(): Call[] {
  return callsTo(BOARD_PATH);
}

function reingestCalls(): Call[] {
  return callsTo(REINGEST_PATH);
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

/** The two control names §1 fixes, per document and version. */
function controlName(slug: string, versionName: string): string {
  return `Re-ingest ${slug} ${versionName}`;
}

function confirmName(slug: string, versionName: string): string {
  return `Confirm re-ingest ${slug} ${versionName}, replacing the live version`;
}

function renderBoard(): void {
  render(
    <QueryProvider>
      <PoliciesBoard />
    </QueryProvider>,
  );
}

async function documentRegion(slug: string): Promise<HTMLElement> {
  const region = await screen.findByRole("region", { name: "Policies" });
  return await within(region).findByRole("region", { name: slug });
}

async function versionRows(slug: string): Promise<HTMLElement[]> {
  const table = await within(await documentRegion(slug)).findByRole("table", {
    name: `Versions of ${slug}`,
  });
  return within(table)
    .getAllByRole("row")
    .filter((candidate) => within(candidate).queryAllByRole("cell").length > 0);
}

/** One row's cell at a column index, with its text collapsed. */
function cellText(rowElement: HTMLElement, column: number): string {
  const cells = within(rowElement).getAllByRole("cell");
  return (at(cells, column).textContent ?? "").trim();
}

/** The one row of a document whose `Version` cell names this version. */
async function rowFor(slug: string, versionName: string): Promise<HTMLElement> {
  const rows = await versionRows(slug);
  const match = rows.filter((rowElement) => cellText(rowElement, 0) === versionName);
  if (match.length !== 1) {
    throw new Error(`expected exactly one row for ${versionName}, found ${match.length}`);
  }
  return at(match, 0);
}

/** Click a control by its accessible name, re-found in its row every time. */
async function clickControl(
  user: ReturnType<typeof userEvent.setup>,
  slug: string,
  versionName: string,
  name: string,
): Promise<void> {
  const target = await rowFor(slug, versionName);
  await user.click(within(target).getByRole("button", { name }));
}

beforeEach(() => {
  boardAnswer = () => board([TWO_VERSIONS]);
  reingestAnswer = () => reingestResult();
  assign.mockReset();
  fetchStub.mockReset();
  fetchStub.mockImplementation(async (input) => {
    const path = pathOf(input);
    if (path === REINGEST_PATH) {
      return await reingestAnswer();
    }
    if (path === BOARD_PATH) {
      return await boardAnswer();
    }
    throw new Error(`unexpected fetch to ${urlOf(input)}`);
  });
  vi.stubGlobal("fetch", fetchStub);
  vi.stubGlobal("location", {
    ...window.location,
    href: `${ORIGIN}/admin/policies`,
    assign,
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("PoliciesReingest — the body one click puts on the wire", () => {
  it("posts its exact three-key body", async () => {
    // §1's fourth criterion, as the call's whole argument list. Asserted as a
    // WHOLE object, so a fourth key is red: a page sending `tenant_id` would be
    // a project rule written into the browser (the tenant is the token's, decided
    // behind the MCP boundary), and a page sending the row's `sha256` would
    // hand the tool a digest it never asked for — the tool reads a version
    // DIRECTORY and re-computes the digest itself, so a stale one from a board
    // painted minutes ago would be a second opinion about what is on disk. The
    // click is on the CURRENT version, so no arming stands between the click
    // and the call and the body is the only thing under test. The pathname is
    // asserted exactly because `/api/admin/policies/reingest` contains the
    // board's own path: a control posting to the board route instead would be
    // green under any `includes` check.
    const user = userEvent.setup();
    renderBoard();

    await clickControl(user, SLUG, "v2", controlName(SLUG, "v2"));

    await waitFor(() => {
      expect(reingestCalls()).toHaveLength(1);
    });
    const call = only(reingestCalls());
    expect(new URL(call.url, ORIGIN).pathname).toBe(REINGEST_PATH);
    expect(call.init.method?.toUpperCase()).toBe("POST");
    expect(bodyOf(call.init)).toEqual({
      slug: SLUG,
      version: "v2",
      idempotency_key: expect.any(String),
    });
  });

  it("mints a fresh idempotency key on every click", async () => {
    // §1's fifth criterion and decision B. Kills a key DERIVED from the row —
    // from `slug`, `version` and `sha256`, the obvious stable choice — which
    // replays forever: an admin who edits one policy file twice would get the
    // first answer handed back and the second edit would never reach the
    // corpus, silently, with a 200 and a status line saying it landed. The two
    // keys are asserted UNEQUAL, which one click alone cannot see, and each is
    // matched against the canonical shape AND its exact length, so a page
    // sending `undefined`, an empty string or a counter is red rather than
    // posting a value the guard's own length check refuses at the far end
    // (§1's input-validation criterion: the 36 characters reach the proxy
    // whole).
    const user = userEvent.setup();
    renderBoard();

    await clickControl(user, SLUG, "v2", controlName(SLUG, "v2"));
    await waitFor(() => {
      expect(reingestCalls()).toHaveLength(1);
    });
    await clickControl(user, SLUG, "v2", controlName(SLUG, "v2"));
    await waitFor(() => {
      expect(reingestCalls()).toHaveLength(2);
    });

    const keys = reingestCalls().map((call) => String(bodyOf(call.init).idempotency_key));
    for (const key of keys) {
      expect(key).toMatch(UUID_PATTERN);
      expect(key).toHaveLength(UUID_LENGTH);
    }
    expect(at(keys, 0)).not.toBe(at(keys, 1));
  });
});

describe("PoliciesReingest — the misclick guard on a version that is not live", () => {
  it("posts on the first click for a version that is already live", async () => {
    // §1's seventh criterion: the arming is for the VERSION CHANGE alone.
    // Kills a guard written over every control rather than over the dangerous
    // direction — refreshing the file that is already live changes nothing
    // about which version answers, so a second confirmation taxes the ordinary
    // act for the rare one (decision C, 24db's precedent). The kill is
    // specifically the single click: the body case above clicks this control
    // once too, but it only asserts WHAT was posted, so a control that armed
    // everything would fail it for a reason that reads as a body fault. The
    // absent confirm name is the other half, so a page that posts AND renames
    // is red too.
    const user = userEvent.setup();
    renderBoard();

    await clickControl(user, SLUG, "v2", controlName(SLUG, "v2"));

    await waitFor(() => {
      expect(reingestCalls()).toHaveLength(1);
    });
    const target = await rowFor(SLUG, "v2");
    expect(
      within(target).getByRole("button", { name: controlName(SLUG, "v2") }),
    ).toBeInTheDocument();
    expect(within(target).queryByRole("button", { name: confirmName(SLUG, "v2") })).toBeNull();
  });

  it("arms before posting for a version that is not live", async () => {
    // §1's sixth criterion and decision C, both halves. Re-ingesting an older
    // version MAKES it live (31a decision E), which is the one act on this page
    // an admin can be surprised by: a click meant to refresh an edited draft
    // would demote the version the retriever is answering from, with no
    // warning. The kill is the ABSENT call on the first click — a control with
    // no arming posts there, and the renamed label alone would not catch it,
    // since a page could both post and rename. The confirm name is asserted
    // present AND the unarmed name absent, so a page adding a second button
    // beside the first (two live re-ingests of one version in one cell) is red;
    // the second click's exact count kills a guard that arms and never fires,
    // where an admin would click twice and nothing would happen at all.
    const user = userEvent.setup();
    renderBoard();

    await clickControl(user, SLUG, "v1", controlName(SLUG, "v1"));

    expect(reingestCalls()).toHaveLength(0);
    const armed = await rowFor(SLUG, "v1");
    expect(
      within(armed).getByRole("button", { name: confirmName(SLUG, "v1") }),
    ).toBeInTheDocument();
    expect(within(armed).queryByRole("button", { name: controlName(SLUG, "v1") })).toBeNull();

    await clickControl(user, SLUG, "v1", confirmName(SLUG, "v1"));

    await waitFor(() => {
      expect(reingestCalls()).toHaveLength(1);
    });
    expect(bodyOf(only(reingestCalls()).init).version).toBe("v1");
  });

  it("disarms when another control in the same document is clicked", async () => {
    // §1's seventh criterion, last sentence. Kills an armed flag that survives
    // an unrelated click: the admin arms the superseded version, changes their
    // mind, refreshes the LIVE version instead, and the next click on the older
    // version's control — which still reads `Confirm` — promotes it to live in
    // one press. The assertion is that the control is found by its UNARMED name
    // again, which a page that merely hides the confirm label without resetting
    // the flag cannot satisfy, and the posted `version` pins that the
    // intervening click was the live one and not the armed one firing early.
    const user = userEvent.setup();
    renderBoard();

    await clickControl(user, SLUG, "v1", controlName(SLUG, "v1"));
    expect(reingestCalls()).toHaveLength(0);

    await clickControl(user, SLUG, "v2", controlName(SLUG, "v2"));

    await waitFor(() => {
      expect(reingestCalls()).toHaveLength(1);
    });
    expect(bodyOf(only(reingestCalls()).init).version).toBe("v2");
    const target = await rowFor(SLUG, "v1");
    expect(
      within(target).getByRole("button", { name: controlName(SLUG, "v1") }),
    ).toBeInTheDocument();
    expect(within(target).queryByRole("button", { name: confirmName(SLUG, "v1") })).toBeNull();
  });
});

describe("PoliciesReingest — what a re-ingest in flight does to the board", () => {
  it("disables only the acting control while its re-ingest is in flight", async () => {
    // §1's eighth criterion and §2's reason for one component instance per
    // control. Kills a board holding ONE in-flight flag for every control: a
    // re-ingest is a redaction call plus an embed pass (decision E's 35
    // seconds), so a single flag would grey out every other version and every
    // other document for most of a minute while one file is read. Kills a page
    // with no flag at all too — the control is what §5's first risk leans on,
    // because a repeated click during a slow embed is a second ingest of the
    // same directory under a second minted key. The POST is held open by an
    // unresolved promise, so the in-flight moment is observable rather than
    // raced, and the two enabled controls are one in the SAME document and one
    // in another, which a per-document flag would fail on the first alone.
    boardAnswer = () => board([TWO_VERSIONS, OTHER_DOCUMENT]);
    let release: (value: Response) => void = () => {};
    reingestAnswer = () =>
      new Promise<Response>((resolve) => {
        release = resolve;
      });
    const user = userEvent.setup();
    renderBoard();

    await clickControl(user, SLUG, "v2", controlName(SLUG, "v2"));

    await waitFor(async () => {
      expect(
        within(await rowFor(SLUG, "v2")).getByRole("button", { name: controlName(SLUG, "v2") }),
      ).toBeDisabled();
    });
    expect(
      within(await rowFor(SLUG, "v1")).getByRole("button", { name: controlName(SLUG, "v1") }),
    ).toBeEnabled();
    expect(
      within(await rowFor(OTHER_SLUG, "v1")).getByRole("button", {
        name: controlName(OTHER_SLUG, "v1"),
      }),
    ).toBeEnabled();

    release(reingestResult());
    await waitFor(async () => {
      expect(
        within(await rowFor(SLUG, "v2")).getByRole("button", { name: controlName(SLUG, "v2") }),
      ).toBeEnabled();
    });
  });

  it("invalidates the board on success and rewrites no row itself", async () => {
    // §1's ninth criterion and decision D's invalidation half. The second read
    // is held open, so the moment AFTER the re-ingest succeeded and BEFORE the
    // refetch answered is observable: a control that rewrote its own row
    // optimistically would show an ingest that has not landed on the one screen
    // an admin uses to decide whether the corpus matches the files — the tool
    // decides what the rows become, including which version is live. Both cells
    // are asserted at that moment, the chunk count and the superseded row's
    // status, because either alone is green for a page that rewrites only the
    // other. Kills a mutation with no invalidation at all, where the board
    // would keep showing the pre-ingest counts until the admin reloaded by
    // hand. Arithmetic: board GETs = 2 (paint, then the
    // invalidation), POSTs = 1.
    let releaseRead: (value: Response) => void = () => {};
    let reads = 0;
    boardAnswer = () => {
      reads += 1;
      if (reads === 1) {
        return board([TWO_VERSIONS]);
      }
      return new Promise<Response>((resolve) => {
        releaseRead = resolve;
      });
    };
    const user = userEvent.setup();
    renderBoard();

    await clickControl(user, SLUG, "v2", controlName(SLUG, "v2"));

    await waitFor(() => {
      expect(boardCalls()).toHaveLength(2);
    });
    expect(cellText(await rowFor(SLUG, "v2"), CHUNKS_CELL)).toBe("7");
    expect(cellText(await rowFor(SLUG, "v1"), STATUS_CELL)).toBe(NOT_LIVE_VERSION_TEXT);
    expect(reingestCalls()).toHaveLength(1);

    releaseRead(
      board([
        {
          ...TWO_VERSIONS,
          versions: [
            version({ version: "v2", is_current: true, chunk_count: 9, sha256: SHA_LIVE }),
            version({
              version: "v1",
              is_current: false,
              chunk_count: 3,
              sha256: SHA_OLD,
              source_path: `fake-corpus/${SLUG}/v1.md`,
            }),
          ],
        },
      ]),
    );
    await waitFor(async () => {
      expect(cellText(await rowFor(SLUG, "v2"), CHUNKS_CELL)).toBe("9");
    });
  });
});

describe("PoliciesReingest — the line a successful re-ingest renders", () => {
  it("renders the created line with the exact chunk count", async () => {
    // §1's tenth criterion and decision D's status half. The board's own rows
    // are re-read from the tool, so the only thing that says what THIS click
    // did is this line. Kills a control rendering a fixed "done" sentence: the
    // count is what tells an admin whether the edited file ingested as spans or
    // registered as an empty document, and the live flag is what says whether
    // the version they refreshed is now the one answering. Both are asserted as
    // the FUNCTION of the values in the answer, and the two inequalities below
    // are the kill for a sentence that takes the arguments and ignores them —
    // a template returning one constant passes a containment check against
    // itself. The literal `7` is asserted in the rendered text as well, so a
    // line built from the BOARD's stale count rather than from the answer is
    // red once the two differ.
    reingestAnswer = () =>
      reingestResult({ version: "v2", created: true, chunk_count: 7, is_current: true });
    const user = userEvent.setup();
    renderBoard();

    await clickControl(user, SLUG, "v2", controlName(SLUG, "v2"));

    const target = await rowFor(SLUG, "v2");
    const line = await within(target).findByRole("status");
    expect(line).toHaveTextContent(REINGEST_CREATED_TEXT(7, true));
    expect(line).toHaveTextContent("7");
    expect(line.textContent ?? "").not.toContain(REINGEST_UNCHANGED_TEXT);
    expect(REINGEST_CREATED_TEXT(7, true)).not.toBe(REINGEST_CREATED_TEXT(3, true));
    expect(REINGEST_CREATED_TEXT(7, true)).not.toBe(REINGEST_CREATED_TEXT(7, false));
    expect(REINGEST_CREATED_TEXT(7, true)).not.toBe(REINGEST_UNCHANGED_TEXT);
  });

  it("renders the unchanged line for a source that did not differ", async () => {
    // §1's tenth criterion, the other branch, and the reason 31a shaped
    // `ReingestPolicyDocumentOutput` around the `created` / `chunk_count` pair.
    // An unchanged source writes NOTHING, so no row on the board moves and an
    // invalidate-only control would be indistinguishable from a click that did
    // nothing at all — the admin would conclude the button is broken and edit
    // the file again. Kills a control that reads `chunk_count` and calls a 0 a
    // failure, and kills one that renders the created line regardless, which
    // would report an ingest of zero chunks over a corpus that never changed.
    // The re-read is still asserted (board GETs = 2): it is what proves the
    // board matches the corpus even when the answer says nothing was written.
    reingestAnswer = () =>
      reingestResult({ version: "v2", created: false, chunk_count: 0, is_current: true });
    const user = userEvent.setup();
    renderBoard();

    await clickControl(user, SLUG, "v2", controlName(SLUG, "v2"));

    const target = await rowFor(SLUG, "v2");
    const line = await within(target).findByRole("status");
    expect(line).toHaveTextContent(REINGEST_UNCHANGED_TEXT);
    expect(line.textContent ?? "").not.toContain(REINGEST_CREATED_TEXT(0, true));
    await waitFor(() => {
      expect(boardCalls()).toHaveLength(2);
    });
    expect(within(target).queryByRole("alert")).toBeNull();
  });
});

describe("PoliciesReingest — a refused re-ingest", () => {
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
      name: "NOT_FOUND",
      status: 404,
      code: "NOT_FOUND",
      sentence: REINGEST_NOT_FOUND_MESSAGE,
      shared: FAILURE_MESSAGES.NOT_FOUND,
    },
    {
      name: "TOOL_DISABLED",
      status: 503,
      code: "TOOL_DISABLED",
      sentence: FAILURE_MESSAGES.TOOL_DISABLED,
      shared: null,
    },
    {
      name: "VALIDATION",
      status: 400,
      code: "VALIDATION",
      sentence: REINGEST_VALIDATION_MESSAGE,
      shared: FAILURE_MESSAGES.VALIDATION,
    },
  ];

  it.each(REFUSALS)(
    "$name renders its own sentence in an alert beside the control",
    async ({ status, code, sentence, shared }) => {
      // §1's first unhappy path, one case per pinned code. Kills a page
      // rendering the CODE at the admin ("TOOL_DISABLED" means nothing to the
      // person reading it), and kills one answering every refusal with
      // `REINGEST_FAILED_MESSAGE` — a principal who is neither `it_admin` nor
      // `platform_admin` would be told to "try again in a moment" for something
      // no retry fixes, and a tool held down by a kill switch would read as a
      // broken page rather than as the deliberate stop it is. The FORBIDDEN and
      // TOOL_DISABLED sentences are READ from `FAILURE_MESSAGES`, so a page
      // inventing its own wording is red without widening 22b's pinned map; the
      // two override rows assert this file's constant AND the absence of the
      // shared string, which is the whole of §1's override clause — the shared
      // `NOT_FOUND` ("That could not be found.") says nothing about WHICH
      // thing, when the only useful fact is that the corpus file is missing on
      // the ingest host, and the shared `VALIDATION` is about a chat message.
      // The board read count is the other kill: a refusal that invalidated
      // anyway would repaint the board as though something had happened.
      reingestAnswer = () => jsonResponse({ code }, status);
      const user = userEvent.setup();
      renderBoard();

      await clickControl(user, SLUG, "v2", controlName(SLUG, "v2"));

      const target = await rowFor(SLUG, "v2");
      const alert = await within(target).findByRole("alert");
      expect(alert).toHaveTextContent(sentence);
      if (shared !== null) {
        expect(alert.textContent ?? "").not.toContain(shared);
      }
      expect(within(target).queryByRole("status")).toBeNull();
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
      name: "a 200 the reingest parser refuses",
      answer: () =>
        jsonResponse(
          {
            slug: SLUG,
            version: "v2",
            created: true,
            is_current: true,
            sha256: SHA_LIVE,
            ingested_at: INGESTED_AT,
            summary: "Fake summary, tests only.",
          },
          200,
        ),
    },
    {
      name: "the proxy's own 502",
      answer: () => jsonResponse({ code: "UPSTREAM_UNAVAILABLE" }, 502),
    },
  ];

  it.each(BROKEN)("$name renders the failure sentence and re-reads nothing", async ({ answer }) => {
    // §1's second unhappy path, from three directions, all killing ONE
    // implementation: a control that treats any answer it did not understand as
    // a success, invalidates the board and prints a status line. The admin
    // would then be shown a freshly re-read board as evidence that an ingest
    // landed, on the strength of a reply that never said so — over a corpus the
    // retriever answers from. Row 1 kills an unguarded `response.json()`, where
    // the parse error escapes as an unhandled rejection instead of a sentence.
    // Row 2 is a result missing `chunk_count` ALONE, so it kills a control that
    // CASTS the body instead of running `policyReingestResultSchema.safeParse`
    // — the status line would render an ingest of `undefined` chunks. Row 3 is
    // the a project rule discriminator: `UPSTREAM_UNAVAILABLE` is not one of
    // the design's eight codes, so it is a failed call and not a typed refusal,
    // and a page answering `failureMessage(code)` here shows "Something went
    // wrong. Nothing was changed." — red against this constant. The absent
    // status line and the single board read are what separate this path from a
    // success.
    reingestAnswer = answer;
    const user = userEvent.setup();
    renderBoard();

    await clickControl(user, SLUG, "v2", controlName(SLUG, "v2"));

    const target = await rowFor(SLUG, "v2");
    const alert = await within(target).findByRole("alert");
    expect(alert).toHaveTextContent(REINGEST_FAILED_MESSAGE);
    expect(within(target).queryByRole("status")).toBeNull();
    expect(boardCalls()).toHaveLength(1);
  });
});

describe("PoliciesReingest — an expired session at the click", () => {
  it("follows the proxy's redirect to /login, with no alert and no re-read", async () => {
    // §1's third unhappy path, and the half a fetcher fixed only at the read
    // misses: a board sits open on a screen for minutes, so a session expiring
    // BETWEEN the paint and the click is the common case. The read here
    // SUCCEEDS, so only the POST path can make the assignment happen and no
    // read-side redirect handling can stand in for it. Kills a mutation
    // checking `response.ok` or `response.status === 401` — the browser has
    // already followed the proxy's redirect, so what arrives is a 200 carrying
    // the login page; the control then fails to parse the
    // HTML and shows a refusal for a re-ingest that was never attempted. The
    // absent alert and the single board read are asserted because the other
    // wrong answer is a page that navigates AND files a failure, leaving an
    // alert flashing behind a navigation.
    reingestAnswer = redirectedResponse;
    const user = userEvent.setup();
    renderBoard();

    await clickControl(user, SLUG, "v2", controlName(SLUG, "v2"));

    await waitFor(() => {
      expect(assign).toHaveBeenCalledWith(LOGIN_URL);
    });
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByRole("status")).toBeNull();
    expect(boardCalls()).toHaveLength(1);
  });
});

describe("PoliciesReingest — the three sentences this surface owns", () => {
  it("pins the failure and the two override sentences exactly", async () => {
    // a project rule on the three strings this plan introduces. The cases
    // above read the CONSTANTS, so they stay green for whatever value the
    // module chooses; only these literals fix them. Kills an implementation
    // that exports the names and fills them with a reworded variant, and the
    // inequalities kill the lazier fault of pointing either override at the
    // shared map — which the parametrised rows' `not.toContain` half would
    // otherwise be the only guard against, and that half reads the same map
    // this case reads. The mutual inequality kills one sentence assigned to all
    // three names, where a missing corpus file and a refused argument would be
    // indistinguishable to the admin who has to fix one of them.
    expect(REINGEST_FAILED_MESSAGE).toBe(
      "The re-ingest could not be completed. Try again in a moment.",
    );
    expect(REINGEST_NOT_FOUND_MESSAGE).toBe(
      "No file for that version was found on the ingest host.",
    );
    expect(REINGEST_VALIDATION_MESSAGE).toBe(
      "That version could not be re-ingested. Check the document and try again.",
    );
    expect(REINGEST_NOT_FOUND_MESSAGE).not.toBe(FAILURE_MESSAGES.NOT_FOUND);
    expect(REINGEST_VALIDATION_MESSAGE).not.toBe(FAILURE_MESSAGES.VALIDATION);
    expect(REINGEST_FAILED_MESSAGE).not.toBe(REINGEST_NOT_FOUND_MESSAGE);
    expect(REINGEST_FAILED_MESSAGE).not.toBe(REINGEST_VALIDATION_MESSAGE);
  });
});
