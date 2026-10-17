import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AppShell } from "@/components/layout/app-shell";

/**
 * The shell's layout: the current-page marker in `MainNav`, the
 * "Account" group holding the user actions, the shared width container, and a
 * nav row that scrolls inside itself at phone width.
 *
 * `next/navigation` is mocked: `SignOutButton` calls `useRouter`, which throws
 * outside a Next tree, and `MainNav` reads `usePathname`, whose answer each
 * case sets through `nav.pathname`. No `QueryClientProvider` is mounted, so
 * `NotificationBell` renders `null` as in `tests/shell.test.tsx`; every case here
 * rendering also holds that "no provider still renders" case.
 */

const nav = vi.hoisted(() => ({ pathname: "/chat" }));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), refresh: vi.fn(), push: vi.fn() }),
  usePathname: () => nav.pathname,
}));

const LINK_NAMES = [
  "Chat",
  "My requests",
  "My access",
  "Approvals",
  "Reviews",
  "Notifications",
  "Audit log",
  "Kill switch",
  "Rules",
  "Policies",
] as const;

function renderShell(showNav?: boolean) {
  return render(
    <AppShell showNav={showNav}>
      <p>hello</p>
    </AppShell>,
  );
}

/**
 * The nav lists the links `/api/me` returns, so the current-page
 * cases below answer it as a platform_admin to keep all ten present. The
 * marker logic they pin is unchanged.
 */
function stubMeAsPlatformAdmin() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) =>
      String(input) === "/api/me"
        ? new Response(
            JSON.stringify({
              display_name: "Fake Person One",
              principals: ["platform_admin"],
              tabs: [
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
              ],
            }),
            { status: 200, headers: { "content-type": "application/json" } },
          )
        : new Response("{}", { status: 500 }),
    ),
  );
}

function renderShellWithAllTabs() {
  stubMeAsPlatformAdmin();
  return render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <AppShell>
        <p>hello</p>
      </AppShell>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.unstubAllGlobals();
  document.documentElement.className = "";
  localStorage.clear();
  nav.pathname = "/chat";
});

describe("MainNav — current page", () => {
  it.each([
    ["My requests", "/requests"],
    ["Audit log", "/admin/audit"],
  ] as const)("marks only %s current on %s", async (name, pathname) => {
    // Catches a nav that never sets `aria-current` (the current page is
    // invisible to screen readers), one that hard-codes Chat as current, and
    // one matching on the first segment, which marks every `/admin/*` link
    // current on `/admin/audit`.
    nav.pathname = pathname;
    renderShellWithAllTabs();
    await screen.findByRole("link", { name: "Policies" });

    const main = screen.getByRole("navigation", { name: "Main" });

    expect(within(main).getByRole("link", { name })).toHaveAttribute("aria-current", "page");
    for (const other of LINK_NAMES.filter((n) => n !== name)) {
      expect(within(main).getByRole("link", { name: other }), other).not.toHaveAttribute(
        "aria-current",
      );
    }
  });

  it("marks no link current on /", async () => {
    // Catches a reversed prefix test (`href.startsWith(pathname)`), which on
    // `/` marks all ten links current because every href starts with `/`.
    nav.pathname = "/";
    renderShellWithAllTabs();
    await screen.findByRole("link", { name: "Policies" });

    const main = screen.getByRole("navigation", { name: "Main" });

    for (const name of LINK_NAMES) {
      expect(within(main).getByRole("link", { name }), name).not.toHaveAttribute("aria-current");
    }
  });
});

describe("AppShell — account actions", () => {
  it("groups the account actions on the right", () => {
    // Catches a shell that leaves Sign out inside the nav or the theme toggle
    // loose in the header, so the "Account" group is missing one of them.
    renderShell();
    const account = screen.getByRole("group", { name: "Account" });

    expect(within(account).getByRole("button", { name: "Sign out" })).toBeInTheDocument();
    expect(within(account).getByRole("button", { name: /theme/i })).toBeInTheDocument();
  });

  it("keeps account actions out of the main nav", () => {
    // Catches a shell that adds Sign out to the "Account" group but forgets to
    // remove it from `MainNav`, rendering it twice. The premise line keeps the
    // case from passing for a shell that dropped Sign out altogether.
    renderShell();
    expect(screen.getByRole("button", { name: "Sign out" })).toBeInTheDocument();

    const main = screen.getByRole("navigation", { name: "Main" });
    expect(within(main).queryByRole("button", { name: "Sign out" })).toBeNull();
  });

  it("shows only the theme toggle on the sign-in shell", () => {
    // Catches a sign-in shell that leaks Sign out (rendered in the group
    // regardless of `showNav`) or hides the whole "Account" group with the
    // nav, taking the theme toggle with it.
    renderShell(false);

    expect(screen.queryByRole("navigation", { name: "Main" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Sign out" })).toBeNull();
    const account = screen.getByRole("group", { name: "Account" });
    expect(within(account).getByRole("button", { name: /theme/i })).toBeInTheDocument();
  });
});

describe("AppShell — layout", () => {
  it("renders page content inside the max-w-6xl container", () => {
    // Catches a shell that puts children straight into `<main>` with no
    // shared container, so pages stretch edge to edge on a wide screen.
    const { container } = renderShell();
    const main = container.querySelector("main#main-content");
    if (main === null) {
      throw new Error("expected <main id=main-content> to be rendered");
    }

    expect(main.firstElementChild).toHaveClass("mx-auto", "max-w-6xl");
  });

  it("lets the nav scroll inside itself at narrow widths", () => {
    // Catches a nav row without its own horizontal scroll, whose ten links
    // overflow and scroll the whole page sideways at phone width.
    renderShell();
    expect(screen.getByRole("navigation", { name: "Main" })).toHaveClass("overflow-x-auto");
  });
});
