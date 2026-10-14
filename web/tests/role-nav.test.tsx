import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AppShell } from "@/components/layout/app-shell";
import { ApprovalQueue } from "@/features/approvals/components/approval-queue";

/**
 * An earlier change §6, `role-nav.test.tsx`: the shell reads `/api/me` and shows the
 * signed-in user's name and account type, and only the tabs that user can use.
 * jsdom, a stubbed `fetch`, no agent-api.
 *
 * Shapes this file fixes that the plan left open (a project rule and 55;
 * these belong in 42's Deviations):
 * - `/api/me` answers `{ display_name, principals, tabs }`; `tabs` is a list of
 *   ten stable keys: `chat requests access approvals reviews notifications
 *   audit killswitch rules policies`. The Python side holds the same ten as a
 *   literal list (`packages/helpdesk/tests/test_rbac_tabs.py`), so a key
 *   renamed on one side only is red on that side.
 * - the nav shows exactly the links whose key was returned, in the existing
 *   order; the four universal keys are what it shows while the read is pending
 *   or has failed.
 * - the header strip is an element with role `group` and accessible name
 *   `Signed in user`, holding the display name and one plain-words label per
 *   principal (`principalLabel`) in `Principal` enum order, not wire order.
 * - the strip is the proof the read RESOLVED: every "four links" row that could
 *   also be satisfied by a pending read first waits for the strip.
 * - the page the hidden-URL row renders is `ApprovalQueue`, whose refused read
 *   is the region alert its own tests pin (23d decision B). The plan calls that
 *   "the tool's forbidden sentence"; the page's sentence is the read alert,
 *   so that is what is asserted and it is recorded as a deviation.
 *
 * `next/navigation` is mocked as `tests/shell.test.tsx` mocks it.
 */

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), refresh: vi.fn(), push: vi.fn() }),
  usePathname: () => "/chat",
}));

const READ_ALERT = "The approval queue could not be read. Try again in a moment.";
const STRIP_NAME = "Signed in user";
const DISPLAY_NAME = "Fake Person One";

const CHAT = { name: "Chat", href: "/chat" };
const REQUESTS = { name: "My requests", href: "/requests" };
const ACCESS = { name: "My access", href: "/access" };
const APPROVALS = { name: "Approvals", href: "/approvals" };
const REVIEWS = { name: "Reviews", href: "/reviews" };
const NOTIFICATIONS = { name: "Notifications", href: "/notifications" };
const AUDIT = { name: "Audit log", href: "/admin/audit" };
const KILLSWITCH = { name: "Kill switch", href: "/admin/killswitch" };
const RULES = { name: "Rules", href: "/admin/rules" };
const POLICIES = { name: "Policies", href: "/admin/policies" };

const FOUR = [CHAT, REQUESTS, ACCESS, NOTIFICATIONS];

const ALL_KEYS = [
  "chat",
  "requests",
  "access",
  "approvals",
  "reviews",
  "notifications",
  "audit",
  "killswitch",
  "rules",
  "policies",
] as const;

type Link = { name: string; href: string };

const fetchStub = vi.fn<typeof fetch>();
let meAnswer: () => Promise<Response>;
let meCalls = 0;
let approvalsAnswer: () => Promise<Response>;

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function meBody(principals: string[], tabs: readonly string[]) {
  return { display_name: DISPLAY_NAME, principals, tabs };
}

beforeEach(() => {
  meCalls = 0;
  meAnswer = async () =>
    json(meBody(["employee"], ["chat", "requests", "access", "notifications"]));
  approvalsAnswer = async () => json({ code: "FORBIDDEN" }, 403);
  fetchStub.mockReset();
  fetchStub.mockImplementation(async (input) => {
    const url = String(input);
    if (url === "/api/me") {
      meCalls += 1;
      return meAnswer();
    }
    if (url.startsWith("/api/me/approvals")) {
      return approvalsAnswer();
    }
    // The bell and anything else the shell reads: a refusal it stays silent on.
    return json({ code: "INTERNAL" }, 500);
  });
  vi.stubGlobal("fetch", fetchStub);
});

function newClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

function renderShell(client: QueryClient = newClient(), children: React.ReactNode = <p>hello</p>) {
  return render(
    <QueryClientProvider client={client}>
      <AppShell>{children}</AppShell>
    </QueryClientProvider>,
  );
}

function navLinks(): Link[] {
  const nav = screen.getByRole("navigation", { name: "Main" });
  return within(nav)
    .getAllByRole("link")
    .map((link) => ({ name: link.textContent ?? "", href: link.getAttribute("href") ?? "" }));
}

function strip(): HTMLElement | null {
  return screen.queryByRole("group", { name: STRIP_NAME });
}

async function resolvedStrip(): Promise<HTMLElement> {
  return screen.findByRole("group", { name: STRIP_NAME });
}

/** Waits until the `/api/me` read has been made and every query has settled. */
async function settled(client: QueryClient): Promise<void> {
  await waitFor(() => expect(meCalls).toBeGreaterThan(0));
  await waitFor(() => expect(client.isFetching()).toBe(0), { timeout: 5_000 });
}

describe("the shell by profile", () => {
  const profiles = [
    {
      who: "an employee",
      principals: ["employee"],
      tabs: ["chat", "requests", "access", "notifications"],
      labels: ["Employee"],
      absent: ["Manager", "Platform admin"],
      links: FOUR,
    },
    {
      who: "a manager",
      principals: ["manager"],
      tabs: ["chat", "requests", "access", "approvals", "reviews", "notifications", "audit"],
      labels: ["Manager"],
      absent: ["Employee", "Platform admin"],
      links: [CHAT, REQUESTS, ACCESS, APPROVALS, REVIEWS, NOTIFICATIONS, AUDIT],
    },
    {
      who: "a platform_admin",
      principals: ["platform_admin"],
      tabs: ALL_KEYS,
      labels: ["Platform admin"],
      absent: ["Employee", "Manager"],
      links: [
        CHAT,
        REQUESTS,
        ACCESS,
        APPROVALS,
        REVIEWS,
        NOTIFICATIONS,
        AUDIT,
        KILLSWITCH,
        RULES,
        POLICIES,
      ],
    },
  ] as const;

  it.each(profiles)(
    "shows $who the links and the name and account type the read returned",
    async ({ principals, tabs, labels, absent, links }) => {
      // Catches a nav that still lists all ten for everyone (the comment this
      // plan rewrites records exactly that decision), one that lists fewer than
      // the read returned, and a strip that omits the name or the account type.
      // Names AND hrefs are asserted in order, so a link filtered by position
      // instead of by key is red as well. The strip is awaited first: a pending
      // read also shows four links, so counting before it resolves would let a
      // nav that ignores the read pass the employee row.
      meAnswer = async () => json(meBody([...principals], tabs));
      renderShell();

      const header = await resolvedStrip();

      expect(navLinks()).toEqual(links);
      expect(header).toHaveTextContent(DISPLAY_NAME);
      for (const label of labels) {
        expect(header).toHaveTextContent(label);
      }
      for (const other of absent) {
        expect(header).not.toHaveTextContent(other);
      }
    },
  );

  it("shows the principals in enum order, not in the order the wire sent them", async () => {
    // Catches a label list built by mapping the wire array as it arrived
    // (decision D: enum order). The wire sends `system_owner` BEFORE `manager`,
    // the enum has `manager` first, and one strip carries both labels so the
    // order of their positions is the only thing that differs.
    meAnswer = async () =>
      json(
        meBody(
          ["system_owner", "manager"],
          ["chat", "requests", "access", "approvals", "reviews", "notifications", "audit"],
        ),
      );
    renderShell();

    const text = (await resolvedStrip()).textContent ?? "";

    expect(text).toContain("Manager");
    expect(text).toContain("System owner");
    expect(text.indexOf("Manager")).toBeLessThan(text.indexOf("System owner"));
  });
});

describe("the tab keys the web knows", () => {
  const rows: { key: (typeof ALL_KEYS)[number]; link: Link }[] = [
    { key: "chat", link: CHAT },
    { key: "requests", link: REQUESTS },
    { key: "access", link: ACCESS },
    { key: "approvals", link: APPROVALS },
    { key: "reviews", link: REVIEWS },
    { key: "notifications", link: NOTIFICATIONS },
    { key: "audit", link: AUDIT },
    { key: "killswitch", link: KILLSWITCH },
    { key: "rules", link: RULES },
    { key: "policies", link: POLICIES },
  ];

  it.each(rows)("returning only $key shows only the $link.name link", async ({ key, link }) => {
    // Catches a key renamed or misspelled on the web side (`audit-log` for
    // `audit`): that link never appears and its row finds an empty nav. The
    // server's ten keys are literals in `test_rbac_tabs.py`; this table holds
    // the same ten, so a key on one side only fails on that side. Asking for
    // ONE key also catches a nav that keeps the four universal links on
    // regardless of what the read returned, which would turn the filter into
    // an add-only list.
    meAnswer = async () => json(meBody(["employee"], [key]));
    renderShell();

    await resolvedStrip();

    expect(navLinks()).toEqual([link]);
  });
});

describe("while /api/me is pending or has failed", () => {
  it("shows the four universal links and no strip while the read is pending", async () => {
    // Catches a nav that shows all ten (or none) until the read resolves, and
    // a strip rendered with an empty name. The read never answers, so this is
    // the state a slow `/me` leaves the page in.
    meAnswer = () => new Promise<Response>(() => undefined);
    renderShell();
    await waitFor(() => expect(meCalls).toBeGreaterThan(0));

    expect(navLinks()).toEqual(FOUR);
    expect(strip()).toBeNull();
  });

  const failures = [
    {
      why: "the network fails",
      answer: async (): Promise<Response> => {
        throw new TypeError("fetch failed");
      },
    },
    { why: "agent-api answers 500", answer: async () => json({ code: "INTERNAL" }, 500) },
    {
      why: "the kill switch refuses the read (403)",
      answer: async () => json({ code: "FORBIDDEN" }, 403),
    },
    { why: "the body is not the shape the shell reads", answer: async () => json({ tabs: "all" }) },
  ] as const;

  it.each(failures)("shows the four universal links and no strip when $why", async ({ answer }) => {
    // Decision E: a failed `/me` never shows MORE than four. Catches a nav that
    // falls back to all ten on error (the old behaviour, and the tempting
    // "fail open" one), and a strip rendered from a half-parsed body. The
    // client is awaited until it has settled, so this is the state AFTER the
    // failure and not the pending state the previous case already pins.
    meAnswer = answer;
    const client = newClient();
    renderShell(client);

    await settled(client);

    expect(navLinks()).toEqual(FOUR);
    expect(strip()).toBeNull();
  });
});

describe("a hidden route opened by URL", () => {
  it("still renders the page and its refusal for an employee, with no Approvals link", async () => {
    // Hiding is cosmetic (§1). Catches two opposite defects: a nav that still
    // lists Approvals for an employee, and a shell that BLOCKS the page it
    // hides (a redirect, a 404 or an empty main) instead of letting the tool's
    // 403 stand. The employee profile is awaited first, so "no Approvals link"
    // is read after the read resolved and not off the pending four.
    renderShell(newClient(), <ApprovalQueue />);

    await resolvedStrip();
    const alert = await screen.findByRole("alert", {}, { timeout: 5_000 });

    expect(alert).toHaveTextContent(READ_ALERT);
    expect(screen.queryByRole("link", { name: "Approvals" })).toBeNull();
    expect(navLinks()).toEqual(FOUR);
  }, 10_000);
});
