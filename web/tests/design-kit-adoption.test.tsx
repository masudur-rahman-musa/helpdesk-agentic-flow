import { readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { LoginForm } from "@/app/login/login-form";
import { AuditSearch } from "@/features/admin/audit/components/audit-search";
import { KillSwitchBoard } from "@/features/admin/killswitch/components/kill-switch-board";
import { PoliciesBoard } from "@/features/admin/policies/components/policies-board";
import { RulesBoard } from "@/features/admin/rules/components/rules-board";
import { GrantList } from "@/features/access/components/grant-list";
import { ApprovalQueue } from "@/features/approvals/components/approval-queue";
import { ChatPanel } from "@/features/chat/components/chat-panel";
import { NotificationList } from "@/features/notifications/components/notification-list";
import { QueryProvider } from "@/components/layout/query-provider";
import { RequestList } from "@/features/requests/components/request-list";
import { ReviewQueue } from "@/features/reviews/components/review-queue";
import * as field from "@/components/ui/field";
import { LIVE_VERSION_TEXT, NOT_LIVE_VERSION_TEXT } from "@/features/admin/policies/format";

/**
 * The shared design kit is applied to
 * every page WITHOUT changing one accessible name, heading, status word or
 * empty sentence. jsdom, no agent-api, no backing service.
 *
 * Every list and board does its own read through TanStack Query, so each is
 * wrapped in `<QueryProvider>` and its `/api/...` read stubbed with a success
 * payload, exactly the fixture shapes the per-page test files already use
 * (requests, access, approvals, reviews, notifications, audit, killswitch,
 * rules, policies). A fixture that does not parse would drop the component into
 * its error or empty branch and make the heading and badge assertions lie
 * so every rendered component here reaches its SUCCESS
 * branch on purpose.
 *
 */

/** `LoginForm` reads `useRouter`; mock the router the way `shell.test.tsx` does
 * so it renders outside an app-router context. */
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), refresh: vi.fn(), push: vi.fn() }),
  usePathname: () => "/login",
}));

const SRC_ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");

/** Every `.tsx` file under `web/src`, as absolute paths. */
function tsxFiles(relative: string): string[] {
  const root = join(SRC_ROOT, relative);
  const out: string[] = [];
  const walk = (dir: string): void => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const full = join(dir, entry.name);
      if (entry.isDirectory()) {
        walk(full);
      } else if (entry.name.endsWith(".tsx")) {
        out.push(full);
      }
    }
  };
  walk(root);
  return out;
}

/** True when `source` imports the name `Button` from `module`. */
function importsButtonFrom(source: string, module: string): boolean {
  const pattern = new RegExp(
    String.raw`import\s*\{[^}]*\bButton\b[^}]*\}\s*from\s*["']${module.replace(
      /[/@.]/g,
      "\\$&",
    )}["']`,
  );
  return pattern.test(source);
}

const fetchStub = vi.fn<typeof fetch>();
const assign = vi.fn<(url: string) => void>();

function jsonResponse(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/** Each GET route the pages read, mapped to the body this run answers for it. */
type Answers = Record<string, () => Response>;

let answers: Answers = {};

beforeEach(() => {
  answers = {};
  assign.mockReset();
  fetchStub.mockReset();
  fetchStub.mockImplementation(async (input) => {
    const raw =
      typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
    const path = new URL(raw, "http://localhost:3000").pathname;
    const answer = answers[path];
    if (answer !== undefined) {
      return answer();
    }
    throw new Error(`unexpected fetch to ${raw}`);
  });
  vi.stubGlobal("fetch", fetchStub);
  vi.stubGlobal("location", { ...window.location, href: "http://localhost:3000/", assign });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function renderWithQuery(ui: React.ReactElement): void {
  render(<QueryProvider>{ui}</QueryProvider>);
}

// ---------------------------------------------------------------------------
// Fixtures: the exact wire shapes each component's zod schema parses, copied
// from the per-page test files so they reach the success branch.
// ---------------------------------------------------------------------------

const REQUEST_ROW: Record<string, unknown> = {
  request_id: "66666666-6666-5666-8666-666666666666",
  request_number: "REQ-0001",
  target_user_id: "11111111-1111-5111-8111-111111111111",
  role_id: "77777777-7777-5777-8777-777777777777",
  justification: "Obviously fake justification.",
  requested_expiry_days: 30,
  risk_class: "elevated",
  decision: "approval",
  status: "pending",
  created_at: "2026-06-02T10:15:00Z",
  role_slug: "payroll-read",
  role_name: "Fake Payroll Read",
  system_slug: "payroll",
  system_name: "Fake Payroll",
  target_display_name: "Fake Person One",
  decision_reasons: ["NOT_IN_TEMPLATE"],
  citations: [],
  approvals: [],
  grant_id: null,
  expires_at: null,
};

/** An EXPIRED grant: its expiry is in the past, so the row's expiry word reads
 * a negative countdown. The clock is frozen so that word is exact. */
const NOW = new Date("2026-06-04T12:00:00Z");
const EXPIRED_GRANT: Record<string, unknown> = {
  grant_id: "55555555-5555-5555-8555-555555555555",
  role_slug: "payroll-read",
  role_name: "Fake Payroll Read",
  privilege: "read",
  system_slug: "payroll",
  system_name: "Fake Payroll",
  granted_at: "2026-01-05T09:00:00Z",
  expires_at: "2026-06-01T12:01:00Z",
};
/**
 * `-3 days left`. The gap from `NOW` is a minute short of three whole days in
 * the past, so `daysUntil` floors to -3 even after `shouldAdvanceTime` lets a
 * few real milliseconds pass between `setSystemTime` and the render (the
 * headroom `access-page.test.tsx` keeps for the same reason).
 */
const EXPIRED_WORD = "-3 days left";

const APPROVAL_ROW: Record<string, unknown> = {
  request_id: "66666666-6666-5666-8666-666666666666",
  request_number: "REQ-0001",
  requester_user_id: "11111111-1111-5111-8111-111111111111",
  target_user_id: "22222222-2222-5222-8222-222222222222",
  role_id: "77777777-7777-5777-8777-777777777777",
  justification: "Obviously fake justification.",
  requested_expiry_days: 30,
  risk_class: "elevated",
  decision: "approval",
  status: "pending",
  created_at: "2026-06-02T10:15:00Z",
  my_slot: "it_admin",
  approvals: [{ slot: "it_admin", user_id: "44444444-4444-5444-8444-444444444444" }],
  role_slug: "payroll-read",
  role_name: "Fake Payroll Read",
  system_slug: "payroll",
  system_name: "Fake Payroll",
  target_display_name: "Fake Person One",
  requester_display_name: "Fake Person Two",
  decision_reasons: ["NOT_IN_TEMPLATE"],
  citations: [],
};

const REVIEW_ROW: Record<string, unknown> = {
  review_id: "11111111-1111-5111-8111-111111111111",
  period: "2026-Q2",
  status: "open",
  due_at: "2026-07-01T00:00:00Z",
  completed_at: null,
  department_id: "88888888-8888-5888-8888-888888888888",
  department_name: "Fake Finance",
  system_id: null,
  system_name: null,
  reviewer_user_id: "77777777-7777-5777-8777-777777777777",
  reviewer_display_name: "Fake Person One",
  item_count: 2,
  decided_count: 0,
};

/** Two items of the selected review: one undecided (its Keep/Revoke controls
 * show), one already decided `keep` (so a `keep` decision word renders). */
const REVIEW_ITEM_UNDECIDED: Record<string, unknown> = {
  item_id: "33333333-3333-5333-8333-333333333333",
  grant_id: "aaaaaaaa-aaaa-5aaa-8aaa-aaaaaaaaaaaa",
  flag: "sod_conflict",
  decision: null,
  decided_at: null,
  target_display_name: "Fake Person Three",
  role_name: "Fake Payroll Read",
  system_name: "Fake Payroll",
  privilege: "read",
  granted_at: "2025-11-03T09:00:00Z",
  expires_at: null,
};
const REVIEW_ITEM_KEPT: Record<string, unknown> = {
  ...REVIEW_ITEM_UNDECIDED,
  item_id: "44444444-4444-5444-8444-444444444444",
  decision: "keep",
  decided_at: "2026-06-01T09:00:00Z",
};

const NOTIFICATION_ROW: Record<string, unknown> = {
  notification_id: "11111111-1111-5111-8111-111111111111",
  kind: "grant_expired",
  title: "Fake Payroll Read has expired",
  body: "Your access to Fake Payroll ended on the first.",
  subject_type: "grant",
  subject_id: "aaaaaaaa-aaaa-5aaa-8aaa-aaaaaaaaaaaa",
  created_at: "2026-06-01T09:00:00Z",
  read_at: null,
};

const AUDIT_EVENT: Record<string, unknown> = {
  seq: 902,
  occurred_at: "2026-09-12T08:15:30.123456Z",
  actor_kind: "user",
  actor_ref: "fake-person-one",
  event_type: "grant.created",
  subject_type: "grant",
  subject_id: "aaaaaaaa-aaaa-5aaa-8aaa-aaaaaaaaaaaa",
  subject_number: null,
  payload: { fake_note: "fake detail" },
  hash: "ab".repeat(32),
};

/** A kill switch board: one running tool and one stopped tool, so both status
 * words (`Running`, `Stopped`) appear. */
function killRow(overrides: Record<string, unknown>): Record<string, unknown> {
  return {
    tool: "request_access",
    protected: false,
    stopped: false,
    global_state: null,
    tenant_state: null,
    unreadable: false,
    ...overrides,
  };
}
const KILL_RUNNING = killRow({ tool: "grant_access", stopped: false });
const KILL_STOPPED = killRow({
  tool: "request_access",
  stopped: true,
  global_state: {
    reason: "Fake platform-wide stop.",
    actor: "fake-admin-one",
    at: "2026-09-12T08:00:00+00:00",
  },
});

const RULE_ROW: Record<string, unknown> = {
  rule_id: "cccccccc-0000-4000-8000-000000000009",
  role_a_id: "aaaaaaaa-0000-4000-8000-000000000001",
  role_a: {
    role_id: "aaaaaaaa-0000-4000-8000-000000000001",
    slug: "fake-approver",
    name: "Fake Approver",
    system_slug: "fake-ledger",
    system_name: "Fake Ledger",
  },
  role_b_id: "bbbbbbbb-0000-4000-8000-000000000002",
  role_b: {
    role_id: "bbbbbbbb-0000-4000-8000-000000000002",
    slug: "fake-poster",
    name: "Fake Poster",
    system_slug: "fake-books",
    system_name: "Fake Books",
  },
  rationale: "Fake rationale for a fake pair of roles.",
  policy_ref: "SOD-A",
};

/** A policies board with one live version and one superseded version, so both
 * status words (`Live`, `Not live`) appear. */
const POLICY_DOCUMENT: Record<string, unknown> = {
  slug: "fake-access-policy",
  title: "Fake Access Policy",
  versions: [
    {
      version: "v2",
      is_current: true,
      ingested_at: "2026-09-13T14:05:09+00:00",
      chunk_count: 7,
      source_path: "fake-corpus/fake-access-policy/v2.md",
      sha256: `1111111111aa${"b".repeat(52)}`,
    },
    {
      version: "v1",
      is_current: false,
      ingested_at: "2026-09-10T14:05:09+00:00",
      chunk_count: 4,
      source_path: "fake-corpus/fake-access-policy/v1.md",
      sha256: `2222222222cc${"d".repeat(52)}`,
    },
  ],
};

// ---------------------------------------------------------------------------
// Case 1
// ---------------------------------------------------------------------------

describe("no page imports Button from the field module", () => {
  it("leaves zero files importing Button from the field module", () => {
    // Catches a page left unpointed at the new kit: any page still importing `Button`
    // from `@/components/ui/field` would keep the old re-export
    // alive, so dropping it would break that page's compile.
    const files = tsxFiles("src");
    const offenders = files.filter((path) =>
      importsButtonFrom(readFileSync(path, "utf8"), "@/components/ui/field"),
    );
    expect(offenders).toEqual([]);
  });

  const IMPORTERS: ReadonlyArray<{ name: string; path: string }> = [
    { name: "login-form", path: "src/app/login/login-form.tsx" },
    { name: "composer", path: "src/features/chat/components/composer.tsx" },
    {
      name: "kill-switch-toggle",
      path: "src/features/admin/killswitch/components/kill-switch-toggle.tsx",
    },
    { name: "audit-verify", path: "src/features/admin/audit/components/audit-verify.tsx" },
    {
      name: "policies-reingest",
      path: "src/features/admin/policies/components/policies-reingest.tsx",
    },
    { name: "review-queue", path: "src/features/reviews/components/review-queue.tsx" },
    { name: "audit-search", path: "src/features/admin/audit/components/audit-search.tsx" },
    {
      name: "notification-list",
      path: "src/features/notifications/components/notification-list.tsx",
    },
    { name: "approval-queue", path: "src/features/approvals/components/approval-queue.tsx" },
  ];

  it.each(IMPORTERS)("$name imports Button from @/components/ui/button", ({ path }) => {
    // Catches a repoint that merely DROPS the field import: each of the nine
    // files still uses `Button`, so one that imports it from nowhere would not
    // compile. Reading it from `@/components/ui/button` is the only valid
    // source once field stops re-exporting it.
    const source = readFileSync(join(SRC_ROOT, path), "utf8");
    expect(importsButtonFrom(source, "@/components/ui/button")).toBe(true);
    expect(importsButtonFrom(source, "@/components/ui/field")).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Case 2
// ---------------------------------------------------------------------------

describe("the field module exports Input and Label but not Button", () => {
  it("has no Button export and keeps Input and Label", () => {
    // Catches the migration leaving the `export { Button } from "./button"` line in
    // field.tsx: with it still there `field.Button` is a function, not
    // `undefined`. Input and Label are asserted present so the delete did not
    // take the two primitives the login form still reads from here.
    expect((field as Record<string, unknown>).Button).toBeUndefined();
    expect(typeof field.Input).toBe("function");
    expect(typeof field.Label).toBe("function");
  });
});

// ---------------------------------------------------------------------------
// Case 3
// ---------------------------------------------------------------------------

describe("the login form still works after the repoint", () => {
  it("has a Sign in button and the three labelled inputs", () => {
    // Catches a repoint that swaps the login button's source for a `Button`
    // that defaults `type="button"` (so the form submits nothing) or renames
    // the submit control: the "Sign in" button and the three named inputs are
    // what the whole login flow is reached through.
    render(<LoginForm />);
    expect(screen.getByRole("button", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.getByLabelText("Tenant")).toBeInTheDocument();
    expect(screen.getByLabelText("Email")).toBeInTheDocument();
    expect(screen.getByLabelText("Password")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Case 4
// ---------------------------------------------------------------------------

describe("each page shows its name as the one level-one heading", () => {
  it("requests keeps My requests as its one h1 and its region name", async () => {
    // Catches a PageHeader adoption that drops the page's level-one heading
    // (renders the title as an h2 or styled div) or renames it, and one that
    // adds a second h1. The region name is asserted unchanged so the kit did
    // not move the list out of its named landmark.
    answers["/api/me/requests"] = () => jsonResponse({ requests: [REQUEST_ROW] }, 200);
    renderWithQuery(<RequestList />);

    const region = await screen.findByRole("region", { name: "My requests" });
    expect(
      within(region).getByRole("heading", { level: 1, name: "My requests" }),
    ).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(await screen.findByRole("list", { name: "Requests" })).toBeInTheDocument();
  });

  it("access keeps Active access as its one h1 and its region name", async () => {
    answers["/api/me/access"] = () => jsonResponse({ grants: [EXPIRED_GRANT] }, 200);
    renderWithQuery(<GrantList />);

    const region = await screen.findByRole("region", { name: "Active access" });
    expect(
      within(region).getByRole("heading", { level: 1, name: "Active access" }),
    ).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(await screen.findByRole("list", { name: "Grants" })).toBeInTheDocument();
  });

  it("approvals keeps Approvals as its one h1 and its region name", async () => {
    answers["/api/me/approvals"] = () => jsonResponse({ approvals: [APPROVAL_ROW] }, 200);
    renderWithQuery(<ApprovalQueue />);

    const region = await screen.findByRole("region", { name: "Approvals" });
    expect(
      within(region).getByRole("heading", { level: 1, name: "Approvals" }),
    ).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(await screen.findByRole("list", { name: "Pending approvals" })).toBeInTheDocument();
  });

  it("reviews keeps Reviews as its one h1 and its region name", async () => {
    answers["/api/me/reviews"] = () => jsonResponse({ reviews: [REVIEW_ROW] }, 200);
    renderWithQuery(<ReviewQueue />);

    const region = await screen.findByRole("region", { name: "Reviews" });
    expect(within(region).getByRole("heading", { level: 1, name: "Reviews" })).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(await screen.findByRole("list", { name: "Review queue" })).toBeInTheDocument();
  });

  it("notifications keeps Notifications as its one h1 and its region name", async () => {
    answers["/api/me/notifications"] = () =>
      jsonResponse({ notifications: [NOTIFICATION_ROW], unread_count: 1 }, 200);
    renderWithQuery(<NotificationList />);

    const region = await screen.findByRole("region", { name: "Notifications" });
    expect(
      within(region).getByRole("heading", { level: 1, name: "Notifications" }),
    ).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(await screen.findByRole("list", { name: "Your notifications" })).toBeInTheDocument();
  });

  it("audit keeps Audit log as its one h1, its region and its table name", async () => {
    // Audit's region aria-label is `Audit log`, which is ALSO the heading text,
    // so both are asserted: the region by role, the single h1 by level.
    answers["/api/admin/audit/search"] = () =>
      jsonResponse({ events: [AUDIT_EVENT], next_before_seq: null }, 200);
    renderWithQuery(<AuditSearch />);

    const region = await screen.findByRole("region", { name: "Audit log" });
    expect(
      within(region).getByRole("heading", { level: 1, name: "Audit log" }),
    ).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(await within(region).findByRole("table", { name: "Audit events" })).toBeInTheDocument();
  });

  it("kill switch keeps Kill switches as its one h1, its region and its table name", async () => {
    answers["/api/admin/killswitch"] = () => jsonResponse({ switches: [KILL_RUNNING] }, 200);
    renderWithQuery(<KillSwitchBoard />);

    const region = await screen.findByRole("region", { name: "Kill switches" });
    expect(
      within(region).getByRole("heading", { level: 1, name: "Kill switches" }),
    ).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(
      await within(region).findByRole("table", { name: "Tool kill switches" }),
    ).toBeInTheDocument();
  });

  it("rules keeps Separation of duties rules as its one h1, its region and its table name", async () => {
    answers["/api/admin/rules"] = () => jsonResponse({ rules: [RULE_ROW] }, 200);
    renderWithQuery(<RulesBoard />);

    const region = await screen.findByRole("region", { name: "Rules" });
    expect(
      within(region).getByRole("heading", { level: 1, name: "Separation of duties rules" }),
    ).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(
      await within(region).findByRole("table", { name: "Separation of duties rules" }),
    ).toBeInTheDocument();
  });

  it("policies keeps Policy documents as its one h1 and its region name", async () => {
    answers["/api/admin/policies"] = () => jsonResponse({ documents: [POLICY_DOCUMENT] }, 200);
    renderWithQuery(<PoliciesBoard />);

    const region = await screen.findByRole("region", { name: "Policies" });
    expect(
      within(region).getByRole("heading", { level: 1, name: "Policy documents" }),
    ).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
  });

  it("chat keeps Chat as its one h1", async () => {
    answers["/api/chat/messages"] = () => jsonResponse([], 200);
    renderWithQuery(<ChatPanel />);

    expect(await screen.findByRole("heading", { level: 1, name: "Chat" })).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
  });
});

// ---------------------------------------------------------------------------
// Case 5
// ---------------------------------------------------------------------------

describe("a status shows as a toned badge", () => {
  /** The one badge inside `region` whose visible text is exactly `text`. An
   * exact match on purpose: a substring match would let `Live` also match the
   * `Not live` badge, since a badge's text is always its whole label. */
  function tonedBadge(region: HTMLElement, text: string): HTMLElement {
    const matches = within(region)
      .getAllByText(text)
      .filter((node) => node.getAttribute("data-tone") !== null);
    if (matches.length !== 1) {
      throw new Error(`expected one toned badge holding "${text}", found ${matches.length}`);
    }
    return matches[0] as HTMLElement;
  }

  it("requests shows pending as a warning badge", async () => {
    // Catches a request row that prints the status word as plain text with no
    // tone, so `pending` cannot be told from `executed` at a glance. The badge
    // must carry both the warning tone and the row's own visible status word.
    answers["/api/me/requests"] = () => jsonResponse({ requests: [REQUEST_ROW] }, 200);
    renderWithQuery(<RequestList />);

    const region = await screen.findByRole("region", { name: "My requests" });
    await within(region).findByRole("list", { name: "Requests" });
    const badge = tonedBadge(region, "Pending approval");
    expect(badge).toHaveAttribute("data-tone", "warning");
  });

  it("access shows an expired grant as a neutral badge", async () => {
    // Catches an access row that shows its expiry word untoned: `expired` maps
    // to neutral, so the expiry word must sit in a neutral badge and not be a
    // bare span the kit left behind.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(NOW);
    try {
      answers["/api/me/access"] = () => jsonResponse({ grants: [EXPIRED_GRANT] }, 200);
      renderWithQuery(<GrantList />);

      const region = await screen.findByRole("region", { name: "Active access" });
      await within(region).findByRole("list", { name: "Grants" });
      const badge = tonedBadge(region, EXPIRED_WORD);
      expect(badge).toHaveAttribute("data-tone", "neutral");
    } finally {
      vi.useRealTimers();
    }
  });

  it("approvals shows pending as a warning badge", async () => {
    // Catches an approval row whose status word is untoned. Same kill as the
    // requests row, on the queue surface.
    answers["/api/me/approvals"] = () => jsonResponse({ approvals: [APPROVAL_ROW] }, 200);
    renderWithQuery(<ApprovalQueue />);

    const region = await screen.findByRole("region", { name: "Approvals" });
    await within(region).findByRole("list", { name: "Pending approvals" });
    const badge = tonedBadge(region, "Pending approval");
    expect(badge).toHaveAttribute("data-tone", "warning");
  });

  it("reviews shows open as warning and the kept item as success", async () => {
    // Catches a review surface that tones neither the queue status nor the item
    // decision: `open` is warning and `keep` is success, and both must be
    // badges. The undecided item's Keep/Revoke controls are the surface the
    // `revoke`→danger and `keep`→success tones are meant for; here the already
    // decided item carries the `keep` word as a success badge.
    answers["/api/me/reviews"] = () => jsonResponse({ reviews: [REVIEW_ROW] }, 200);
    answers["/api/reviews/11111111-1111-5111-8111-111111111111/items"] = () =>
      jsonResponse(
        {
          review_id: "11111111-1111-5111-8111-111111111111",
          items: [REVIEW_ITEM_UNDECIDED, REVIEW_ITEM_KEPT],
        },
        200,
      );
    const user = userEvent.setup();
    renderWithQuery(<ReviewQueue />);

    const region = await screen.findByRole("region", { name: "Reviews" });
    // Await the queue list before reading the badge: the status word only
    // exists once the async read resolves, and before it the select's own
    // `Open` option (untoned) is the one match.
    await within(region).findByRole("list", { name: "Review queue" });
    const openBadge = tonedBadge(region, "Open");
    expect(openBadge).toHaveAttribute("data-tone", "warning");

    await user.click(await within(region).findByRole("button", { name: "Show items for 2026-Q2" }));
    await within(region).findByRole("list", { name: "Items in review" });
    const keptBadge = tonedBadge(region, "Kept");
    expect(keptBadge).toHaveAttribute("data-tone", "success");
  });

  it("kill switch shows Running as success and Stopped as danger", async () => {
    // Catches a board that prints the stopped/running words untoned, so an
    // operator cannot see a stopped tool at a glance: `Running`→success and
    // `Stopped`→danger as hand-toned badges on their rows.
    answers["/api/admin/killswitch"] = () =>
      jsonResponse({ switches: [KILL_RUNNING, KILL_STOPPED] }, 200);
    renderWithQuery(<KillSwitchBoard />);

    const region = await screen.findByRole("region", { name: "Kill switches" });
    await within(region).findByRole("table", { name: "Tool kill switches" });
    expect(tonedBadge(region, "Running")).toHaveAttribute("data-tone", "success");
    expect(tonedBadge(region, "Stopped")).toHaveAttribute("data-tone", "danger");
  });

  it("policies shows Live as success and Not live as neutral", async () => {
    // Catches a version row that prints the status word untoned: `Live`→success
    // and `Not live`→neutral as hand-toned badges, both in one document.
    answers["/api/admin/policies"] = () => jsonResponse({ documents: [POLICY_DOCUMENT] }, 200);
    renderWithQuery(<PoliciesBoard />);

    const region = await screen.findByRole("region", { name: "Policies" });
    await within(region).findByRole("table", { name: "Versions of fake-access-policy" });
    expect(tonedBadge(region, LIVE_VERSION_TEXT)).toHaveAttribute("data-tone", "success");
    expect(tonedBadge(region, NOT_LIVE_VERSION_TEXT)).toHaveAttribute("data-tone", "neutral");
  });
});

// ---------------------------------------------------------------------------
// Case 6
// ---------------------------------------------------------------------------

describe("an empty list shows its exact sentence inside an EmptyState", () => {
  /** The sentence must sit inside EmptyState, whose root carries `border-dashed`. */
  function inEmptyState(sentence: HTMLElement): HTMLElement {
    const host = sentence.closest(".border-dashed");
    if (host === null) {
      throw new Error("expected the empty sentence inside an EmptyState (border-dashed root)");
    }
    return host as HTMLElement;
  }

  it("requests shows its empty sentence inside an EmptyState", async () => {
    // Catches an empty list rendered as a bare paragraph the kit left untouched:
    // the sentence must now sit inside EmptyState, so a page that renders both
    // the old sentence AND an empty EmptyState, or neither, is red.
    answers["/api/me/requests"] = () => jsonResponse({ requests: [] }, 200);
    renderWithQuery(<RequestList />);

    const sentence = await screen.findByText("You have not asked for any access yet.");
    expect(inEmptyState(sentence)).toBeInTheDocument();
  });

  it("access shows its empty sentence inside an EmptyState", async () => {
    answers["/api/me/access"] = () => jsonResponse({ grants: [] }, 200);
    renderWithQuery(<GrantList />);

    const sentence = await screen.findByText("You hold no active access.");
    expect(inEmptyState(sentence)).toBeInTheDocument();
  });

  it("approvals shows its empty sentence inside an EmptyState", async () => {
    answers["/api/me/approvals"] = () => jsonResponse({ approvals: [] }, 200);
    renderWithQuery(<ApprovalQueue />);

    const sentence = await screen.findByText("Nothing is waiting for your decision.");
    expect(inEmptyState(sentence)).toBeInTheDocument();
  });

  it("reviews shows its empty sentence inside an EmptyState", async () => {
    answers["/api/me/reviews"] = () => jsonResponse({ reviews: [] }, 200);
    renderWithQuery(<ReviewQueue />);

    const sentence = await screen.findByText("No reviews are waiting for you.");
    expect(inEmptyState(sentence)).toBeInTheDocument();
  });

  it("notifications shows its empty sentence inside an EmptyState", async () => {
    answers["/api/me/notifications"] = () =>
      jsonResponse({ notifications: [], unread_count: 0 }, 200);
    renderWithQuery(<NotificationList />);

    const sentence = await screen.findByText("You have no notifications.");
    expect(inEmptyState(sentence)).toBeInTheDocument();
  });

  it("audit shows its empty sentence inside an EmptyState", async () => {
    answers["/api/admin/audit/search"] = () =>
      jsonResponse({ events: [], next_before_seq: null }, 200);
    renderWithQuery(<AuditSearch />);

    const sentence = await screen.findByText("No events match that search.");
    expect(inEmptyState(sentence)).toBeInTheDocument();
  });

  it("kill switch shows its empty sentence inside an EmptyState", async () => {
    answers["/api/admin/killswitch"] = () => jsonResponse({ switches: [] }, 200);
    renderWithQuery(<KillSwitchBoard />);

    const sentence = await screen.findByText("No tools are registered.");
    expect(inEmptyState(sentence)).toBeInTheDocument();
  });

  it("rules shows its empty sentence inside an EmptyState", async () => {
    answers["/api/admin/rules"] = () => jsonResponse({ rules: [] }, 200);
    renderWithQuery(<RulesBoard />);

    const sentence = await screen.findByText("No separation of duties rules are configured.");
    expect(inEmptyState(sentence)).toBeInTheDocument();
  });

  it("policies shows its empty sentence inside an EmptyState", async () => {
    answers["/api/admin/policies"] = () => jsonResponse({ documents: [] }, 200);
    renderWithQuery(<PoliciesBoard />);

    const sentence = await screen.findByText("No policy documents have been ingested.");
    expect(inEmptyState(sentence)).toBeInTheDocument();
  });
});
