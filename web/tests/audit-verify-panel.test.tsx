import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AUDIT_VALIDATION_MESSAGE } from "@/components/admin/audit-search";
import { AUDIT_VERIFY_READ_MESSAGE, AuditVerify } from "@/components/admin/audit-verify";
import { QueryProvider } from "@/components/query-provider";
import { FAILURE_MESSAGES } from "@/lib/chat-errors";

/**
 * An earlier change §6, `audit-verify-panel.test.tsx`: the `Chain integrity` region of
 * `/admin/audit` — when it calls an earlier change's chain walk, what it says about the
 * answer, and what it says about every way the answer can fail — against a
 * stubbed `fetch`. jsdom, no agent-api, no backing service. (No basename
 * collision in the tree, a project rule: the neighbours are
 * `audit-csv.test.ts`, `audit-export.test.tsx`, `audit-filters.test.ts`,
 * `audit-proxy-routes.test.ts`, `audit-schema.test.ts` and
 * `audit-search-page.test.tsx`.)
 *
 * The unit under test is `AuditVerify` from
 * `web/components/admin/audit-verify.tsx`, taking NO props and firing on a
 * CLICK through `useMutation` (decisions D and E). It is its own component
 * because it shares no state with the search, so nothing here renders
 * `AuditSearch` and 24ba's cases are untouched.
 *
 * Everything here is a READ, and the read takes no argument at all: 24a's route
 * forwards no query and no body, and agent-api walks the chain against the
 * tenant in the verified token. A page that could ask about
 * another tenant's chain is the thing that must not exist, which is why the
 * empty query string is asserted rather than assumed.
 *
 * Shapes this file fixes that the plan left open (a project rule and 55;
 * these belong in 24bb's Deviations):
 * - "GET" is asserted as `init?.method ?? "GET"`, so an omitted method — what
 *   `fetch` defaults to and what 24ba's search fetcher relies on — passes, and
 *   a POST does not. The plan fixes the verb, not whether it is spelled out.
 * - the two result sentences are found by TEXT (§6: a `<p>`), and the refusal
 *   and failure sentences inside `role="alert"`. The case asserting a sentence
 *   also asserts that the other class of sentence is ABSENT, which is what
 *   makes the discriminator assertable at all.
 * - the in-flight case holds the response open with a promise the case
 *   resolves, the shape `approvals-page.test.tsx` uses for the same question.
 *
 * The refusal bodies are `{"code": "<value>"}` — agent-api's own envelope, SPEC
 * §8.1 — and the two refusal sentences are READ from `FAILURE_MESSAGES`, which
 * 22b's `tests/chat-schema.test.ts` pins verbatim. This surface overrides none
 * of them (§1: no `VALIDATION` override, because the route takes no argument
 * there is nothing for an auditor to correct).
 */

const ORIGIN = "http://localhost:3000";
const VERIFY_PATH = "/api/admin/audit/verify";
const LOGIN_URL = "http://localhost:3000/login";
const INTACT = "The chain is intact.";
const BROKEN_SEQ = 42;
const BROKEN = "The chain is broken at event 42.";

const fetchStub = vi.fn<typeof fetch>();
const assign = vi.fn<(url: string) => void>();

type VerifyAnswer = () => Response | Promise<Response>;

let verifyAnswer: VerifyAnswer = () => jsonResponse({ ok: true, broken_at: null }, 200);

function jsonResponse(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
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

function verifyCalls(): Array<[string, RequestInit | undefined]> {
  return fetchStub.mock.calls
    .map(([input, init]): [string, RequestInit | undefined] => [urlOf(input), init])
    .filter(([url]) => url.includes(VERIFY_PATH));
}

function firstVerifyCall(): [string, RequestInit | undefined] {
  const call = verifyCalls()[0];
  if (call === undefined) {
    throw new Error("expected a call to the verify proxy, found none");
  }
  return call;
}

function renderVerify(): void {
  render(
    <QueryProvider>
      <AuditVerify />
    </QueryProvider>,
  );
}

async function region(): Promise<HTMLElement> {
  return await screen.findByRole("region", { name: "Chain integrity" });
}

async function verifyButton(): Promise<HTMLElement> {
  return within(await region()).getByRole("button", { name: "Verify integrity" });
}

async function check(user: ReturnType<typeof userEvent.setup>): Promise<void> {
  await user.click(await verifyButton());
}

beforeEach(() => {
  verifyAnswer = () => jsonResponse({ ok: true, broken_at: null }, 200);
  assign.mockReset();
  fetchStub.mockReset();
  fetchStub.mockImplementation(async (input) => {
    const url = urlOf(input);
    if (url.includes(VERIFY_PATH)) {
      return await verifyAnswer();
    }
    throw new Error(`unexpected fetch to ${url}`);
  });
  vi.stubGlobal("fetch", fetchStub);
  vi.stubGlobal("location", {
    ...window.location,
    href: `${ORIGIN}/admin/audit`,
    assign,
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("AuditVerify — first paint", () => {
  it("renders the named region and its control without calling the proxy", async () => {
    // §1's seventh criterion, both halves in the one case that can hold them.
    // Kills a panel built on `useQuery` with no `enabled: false` — a chain walk
    // is a full scan of the tenant's log, and firing it on every visit to
    // `/admin/audit` puts that scan behind a page an auditor opens to read one
    // row. Decision E is why it is a mutation: an answer asked for twice is two
    // answers, not a cache hit, because the second ask exists precisely because
    // something may have changed. The region and control names are asserted
    // because every other case in this file locates the control through them
    //, and a second region is what separates this panel from
    // 24ba's `Audit log` on the same screen.
    renderVerify();

    expect(await region()).toBeInTheDocument();
    expect(await verifyButton()).toBeEnabled();
    expect(verifyCalls()).toHaveLength(0);
  });
});

describe("AuditVerify — the call it makes", () => {
  it("asks the verify proxy once, as a GET, with no query string at all", async () => {
    // §1's note that the route takes NO argument, and a project rule seen from the
    // caller. Kills a panel sending a tenant, a user or a `before_seq` it read
    // off the search form — agent-api derives the tenant from the verified
    // token, so a parameter here is either ignored (and misleading to every
    // later reader) or, on a route that grew one, a way to ask about a chain
    // that is not yours. The exact pathname kills a call to the SEARCH proxy,
    // whose 200 would parse as nothing this panel reads; `toHaveLength(1)`
    // kills a handler wired to fire twice per click.
    const user = userEvent.setup();
    renderVerify();

    await check(user);

    await waitFor(() => {
      expect(verifyCalls()).toHaveLength(1);
    });
    const [url, init] = firstVerifyCall();
    expect(new URL(url, ORIGIN).pathname).toBe(VERIFY_PATH);
    expect(new URL(url, ORIGIN).search).toBe("");
    expect(init?.body).toBeUndefined();
    expect(init?.method ?? "GET").toBe("GET");
  });

  it("disables the control while the check is in flight and enables it after", async () => {
    // §1's eighth criterion. Kills a control with no pending state, where an
    // auditor waiting on a full chain walk clicks again — and again — and each
    // click is another scan of the whole log for the same answer, which is the
    // cheapest way to make an audit page expensive. The re-enable is the other
    // half and kills a button disabled for good after the first check: the
    // second ask is the entire point of decision E, and a panel answering once
    // per page load makes the auditor reload to re-check.
    let release: (value: Response) => void = () => {};
    verifyAnswer = () =>
      new Promise<Response>((resolve) => {
        release = resolve;
      });
    const user = userEvent.setup();
    renderVerify();

    await check(user);

    await waitFor(async () => {
      expect(await verifyButton()).toBeDisabled();
    });

    release(jsonResponse({ ok: true, broken_at: null }, 200));
    await waitFor(async () => {
      expect(await verifyButton()).toBeEnabled();
    });
    expect(await screen.findByText(INTACT)).toBeInTheDocument();
  });
});

describe("AuditVerify — what the chain says", () => {
  it("renders the intact sentence for ok true and no break", async () => {
    // §1's eighth criterion, the good answer, as the literal sentence. Kills a
    // panel rendering the raw `{"ok":true,"broken_at":null}` at an auditor, and
    // kills one that says nothing at all on success — a check whose only signal
    // is the absence of an error is a check nobody can report. The broken
    // sentence is asserted ABSENT, so a panel rendering both is red.
    verifyAnswer = () => jsonResponse({ ok: true, broken_at: null }, 200);
    const user = userEvent.setup();
    renderVerify();

    await check(user);

    expect(await screen.findByText(INTACT)).toBeInTheDocument();
    expect(screen.queryByText(BROKEN)).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("names the broken event for ok false with a seq", async () => {
    // The same criterion's other row, and the one the whole feature exists for.
    // Kills a panel that reads `ok` and ignores `broken_at` — "the chain is
    // broken" with no seq leaves an auditor to binary-search a log by hand for
    // the event that does not follow from the one before it, which is the
    // single fact an earlier change computed for them. It also kills a panel that treats
    // any non-200-shaped answer as an error: a broken chain is a SUCCESSFUL
    // read whose content is bad news, and rendering it as "the chain could not
    // be checked" would report tampering as an outage. The generic failure
    // sentence is asserted absent for exactly that reason.
    verifyAnswer = () => jsonResponse({ ok: false, broken_at: BROKEN_SEQ }, 200);
    const user = userEvent.setup();
    renderVerify();

    await check(user);

    expect(await screen.findByText(BROKEN)).toBeInTheDocument();
    expect(screen.queryByText(INTACT)).toBeNull();
    expect(screen.queryByText(AUDIT_VERIFY_READ_MESSAGE)).toBeNull();
  });
});

describe("AuditVerify — a refused check", () => {
  const REFUSALS = [
    {
      name: "a 403 FORBIDDEN",
      status: 403,
      code: "FORBIDDEN",
      sentence: FAILURE_MESSAGES.FORBIDDEN,
    },
    {
      name: "a 503 TOOL_DISABLED",
      status: 503,
      code: "TOOL_DISABLED",
      sentence: FAILURE_MESSAGES.TOOL_DISABLED,
    },
  ] as const;

  it.each(REFUSALS)("$name renders its own sentence and not the read failure", async (refusal) => {
    // §1's first unhappy path and §5's fourth risk. The `FORBIDDEN` row is the
    // load-bearing one: an earlier change answers 403 to a verified token holding no
    // audit principal and 24a's proxy forwards that typed body rather than
    // flattening it, so a panel answering the generic sentence would tell an
    // employee the chain is UNREACHABLE — which reads as an outage or as
    // tampering, and is a sentence an auditor escalates — when the truth is
    // that they may not ask. The `TOOL_DISABLED` row kills the same collapse
    // from the kill-switch side, where "try again in a moment" is advice that
    // never comes true. Both sentences are read from the shared map, so a panel
    // inventing its own wording is red without widening 22b's pinned strings.
    verifyAnswer = () => jsonResponse({ code: refusal.code }, refusal.status);
    const user = userEvent.setup();
    renderVerify();

    await check(user);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(refusal.sentence);
    expect(screen.queryByText(AUDIT_VERIFY_READ_MESSAGE)).toBeNull();
    expect(screen.queryByText(INTACT)).toBeNull();
  });
});

describe("AuditVerify — a check that did not come back", () => {
  const FAILURES = [
    {
      name: "the proxy's own 502",
      answer: (): Response => jsonResponse({ code: "UPSTREAM_UNAVAILABLE" }, 502),
    },
    {
      name: "a 200 whose payload the parser refuses",
      answer: (): Response => jsonResponse({ ok: "yes" }, 200),
    },
    {
      name: "a 500 carrying no JSON at all",
      answer: (): Response =>
        new Response("<html>bad gateway</html>", {
          status: 500,
          headers: { "content-type": "text/html" },
        }),
    },
  ] as const;

  it.each(FAILURES)("$name renders the read failure and no verdict", async (row) => {
    // §1's second unhappy path, and the discriminator §1 restates so the pair
    // is satisfiable together: a body carrying one of SPEC
    // §8.1's eight codes is a refusal, and everything else is a failure.
    // `UPSTREAM_UNAVAILABLE` is the proxy's OWN code and not one of the eight,
    // so the first row kills a panel passing any `code` to `failureMessage` and
    // rendering whatever comes back — the shared map's `default` sentence,
    // "Something went wrong. Nothing was changed.", which on a chain check is a
    // claim about a mutation this surface never makes. The second row kills a
    // panel that CASTS the body instead of running `auditVerifySchema`: `ok`
    // arriving as the string "yes" is truthy, and the auditor is told the chain
    // is intact by a payload nobody validated — the single worst sentence this
    // page can print wrongly. The third kills an unguarded `response.json()`,
    // whose parse error escapes as an unhandled rejection and leaves the panel
    // showing nothing at all. Both verdicts are asserted absent, so a panel
    // that shows an alert AND a verdict says two contradictory things about one
    // answer.
    verifyAnswer = row.answer;
    const user = userEvent.setup();
    renderVerify();

    await check(user);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(AUDIT_VERIFY_READ_MESSAGE);
    expect(screen.queryByText(INTACT)).toBeNull();
    expect(screen.queryByText(BROKEN)).toBeNull();
  });

  it("pins the read failure sentence exactly", async () => {
    // a project rule on the one string this component introduces. The rows
    // above READ the constant, so they stay green for whatever the module
    // exports; only this literal fixes the words. Kills an implementation that
    // exports the symbol and fills it with the shared map's `default` sentence
    // ("Nothing was changed." is about a mutation, and this surface makes
    // none), and kills a variant that says the chain is BROKEN rather than
    // unchecked — the two are opposite facts and only one of them is true here.
    expect(AUDIT_VERIFY_READ_MESSAGE).toBe(
      "The chain could not be checked. Try again in a moment.",
    );
    expect(AUDIT_VERIFY_READ_MESSAGE).not.toBe(FAILURE_MESSAGES.default);
  });
});

describe("AuditVerify — an expired session", () => {
  it("follows the proxy's redirect to /login instead of answering about the chain", async () => {
    // §1's last unhappy path, and the reason it is `response.redirected` and
    // never a status (22b decision E, a project rule): the browser has
    // ALREADY followed the proxy's redirect, so what arrives is a 200 carrying
    // the login page. Kills a fetcher checking `response.ok` or
    // `response.status === 401` — both see a successful response, the parser
    // then refuses the HTML, and the auditor reads "the chain could not be
    // checked" for a session that merely expired. On THIS surface that is the
    // sentence most likely to be escalated as evidence of tampering, so the
    // absent alert is the assertion that separates the two outcomes.
    verifyAnswer = redirectedResponse;
    const user = userEvent.setup();
    renderVerify();

    await check(user);

    await waitFor(() => {
      expect(assign).toHaveBeenCalledWith(LOGIN_URL);
    });
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByText(INTACT)).toBeNull();
  });
});

describe("AuditVerify — the failures and the sentence the migration must keep (plan 24ib §6)", () => {
  it("renders the read failure for a 500 carrying no body at all", async () => {
    // An earlier change §5's second risk, named for this panel: `readWire` reads the
    // body of a non-ok response to look for a typed code, and the existing
    // `a 500 carrying no JSON at all` row still hands it HTML BYTES to fail on.
    // This one is `new Response(null, ...)` — nothing to read at all. Kills a
    // fetcher whose error path calls `response.json()` unguarded: the parse
    // error escapes as an unhandled rejection instead of a sentence, and an
    // auditor who clicked `Verify integrity` over a dead proxy is shown neither
    // a verdict nor a failure and is free to read the silence as either.
    verifyAnswer = () => new Response(null, { status: 500 });
    const user = userEvent.setup();
    renderVerify();

    await check(user);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(AUDIT_VERIFY_READ_MESSAGE);
    expect(screen.queryByText(INTACT)).toBeNull();
    expect(screen.queryByText(BROKEN)).toBeNull();
  });

  it("renders the read failure for a 200 whose body is not JSON at all", async () => {
    // An earlier change decision E on this panel: today `bodyOf` guards the parse and
    // `auditVerifySchema` refuses the `undefined`; through `readWire` the same
    // response takes the same route to the same sentence, and the case is what
    // turns that equivalence from an argument into an assertion. Kills a
    // migration that lets a 200 carrying an HTML error page reach the verdict
    // branch — the panel would print "The chain is intact." on the strength of
    // a body nobody parsed, which is the single worst sentence this surface can
    // print wrongly, and both verdicts are asserted absent for that reason.
    verifyAnswer = () =>
      new Response("<html>not json</html>", {
        status: 200,
        headers: { "content-type": "text/html" },
      });
    const user = userEvent.setup();
    renderVerify();

    await check(user);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(AUDIT_VERIFY_READ_MESSAGE);
    expect(screen.queryByText(INTACT)).toBeNull();
    expect(screen.queryByText(BROKEN)).toBeNull();
  });

  it("renders the SHARED VALIDATION sentence and never the audit-search override", async () => {
    // 24ba decision H seen from the surface it does NOT belong to, and the
    // reason an earlier change keeps `errorMessage` per board rather than sharing one.
    // Kills a migration that lifts the `VALIDATION` override out of
    // `AuditSearch` into anything both panels reach — this route takes no
    // argument at all, so "Check the filters and try again." points an auditor
    // at filters that do not exist on this surface and at a correction nobody
    // can make. The `not.toContain` is the load-bearing half: the shared
    // sentence alone would stay green on a panel rendering both.
    verifyAnswer = () => jsonResponse({ code: "VALIDATION" }, 400);
    const user = userEvent.setup();
    renderVerify();

    await check(user);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(FAILURE_MESSAGES.VALIDATION);
    expect(alert.textContent ?? "").not.toContain(AUDIT_VALIDATION_MESSAGE);
    expect(screen.queryByText(AUDIT_VERIFY_READ_MESSAGE)).toBeNull();
    expect(screen.queryByText(INTACT)).toBeNull();
  });
});
