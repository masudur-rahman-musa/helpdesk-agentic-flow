import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AppShell } from "@/components/layout/app-shell";
import { DEMO_BANNER_TEXT } from "@/components/layout/demo-banner";
import { QueryProvider } from "@/components/layout/query-provider";
import { THEME_STORAGE_KEY } from "@/components/layout/theme-toggle";

/**
 * The authenticated shell: demo banner, keyboard reachability and the dark-mode
 * toggle that persists (last criterion).
 *
 * Value fixed here:
 * the theme control is located as a button whose accessible name matches
 * /theme/i, so the implementation owes it a name containing that word.
 *
 * The shell reaches `useRouter` through `MainNav`'s `SignOutButton`, and
 * `next/navigation` throws `invariant expected app router to be mounted`
 * outside a Next tree, so the module is mocked here. The
 * mock is a test seam only: `replaceMock` is asserted by the sign-out case
 * below, so a shell that stopped rendering the button, or a button that stopped
 * routing, is red rather than silently satisfied by a mock nothing reads.
 */

const replaceMock = vi.fn<(href: string) => void>();
const refreshMock = vi.fn<() => void>();
const pushMock = vi.fn<(href: string) => void>();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: replaceMock, refresh: refreshMock, push: pushMock }),
  usePathname: () => "/chat",
}));

const fetchStub = vi.fn<typeof fetch>();
vi.stubGlobal("fetch", fetchStub);

const FOCUSABLE = 'a[href], button, input, [tabindex]:not([tabindex="-1"])';

function must<T>(value: T | null, what: string): T {
  if (value === null) {
    throw new Error(`expected ${what} to be rendered`);
  }
  return value;
}

beforeEach(() => {
  document.documentElement.className = "";
  localStorage.clear();
  replaceMock.mockReset();
  refreshMock.mockReset();
  pushMock.mockReset();
  fetchStub.mockReset();
  fetchStub.mockResolvedValue(new Response(null, { status: 204 }));
});

describe("AppShell — demo banner", () => {
  it("renders the demo banner text on the shell", () => {
    // Kills a shell that renders the banner only on one page (or drops it while
    // rearranging the layout). Asserted against the exported constant, so a
    // reworded banner stays green and a MISSING banner goes red.
    render(
      <AppShell>
        <p>hello</p>
      </AppShell>,
    );
    expect(screen.getByText(DEMO_BANNER_TEXT)).toBeInTheDocument();
  });

  it("renders its children inside the main landmark", () => {
    // Kills a shell that drops children (a wrapper rendering only chrome), which
    // every other case here would pass.
    const { container } = render(
      <AppShell>
        <p>hello</p>
      </AppShell>,
    );
    const main = must(container.querySelector("main#main-content"), "<main id=main-content>");
    expect(main).toHaveTextContent("hello");
  });

  it("signs out through the router the shell mounts", async () => {
    // The mock's kill: `next/navigation` is stubbed for every case in this file,
    // so without this one a shell that dropped the nav entirely would still be
    // green. It asserts the POST and the route the button owes together, which
    // is the only pair that proves the mocked hook is the one the tree calls.
    const user = userEvent.setup();
    render(
      <AppShell>
        <p>hello</p>
      </AppShell>,
    );

    await user.click(screen.getByRole("button", { name: "Sign out" }));

    await waitFor(() => expect(replaceMock).toHaveBeenCalledWith("/login"));
    expect(fetchStub).toHaveBeenCalledWith("/api/logout", { method: "POST" });
    expect(refreshMock).toHaveBeenCalledTimes(1);
  });
});

describe("AppShell — keyboard reachability", () => {
  it("makes the skip link the first focusable element in the shell", () => {
    // Kills a skip link placed after the banner, the nav or the theme toggle:
    // it then takes several tabs to reach, which is the whole point of it. The
    // first-match assertion is what the mere presence of the link cannot make.
    const { container } = render(
      <AppShell>
        <p>hello</p>
      </AppShell>,
    );
    const skipLink = must(container.querySelector('a[href="#main-content"]'), "the skip link");
    const first = must(container.querySelector(FOCUSABLE), "any focusable element");
    expect(first).toBe(skipLink);
  });

  it("gives the skip link a target with id main-content", () => {
    // Kills a skip link whose href points at an id nothing carries, so the link
    // is present, focusable and does nothing.
    const { container } = render(
      <AppShell>
        <p>hello</p>
      </AppShell>,
    );
    expect(container.querySelector("#main-content")).not.toBeNull();
  });
});

describe("AppShell — theme toggle", () => {
  it("flips the dark class on the document root and back", async () => {
    // Kills a toggle that writes component state only (the class never reaches
    // <html>, so Tailwind's darkMode: "class" never applies), and kills a
    // one-way toggle that can turn dark on but not off.
    const user = userEvent.setup();
    render(
      <AppShell>
        <p>hello</p>
      </AppShell>,
    );
    const toggle = screen.getByRole("button", { name: /theme/i });

    expect(document.documentElement.classList.contains("dark")).toBe(false);
    await user.click(toggle);
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    await user.click(toggle);
    expect(document.documentElement.classList.contains("dark")).toBe(false);
  });

  it("persists the chosen theme under helpdesk-theme", async () => {
    // Kills a toggle that flips the class without writing storage, so the choice
    // is lost on the next navigation; the class-flipping case above passes for
    // such a toggle. The key is pinned as a literal so renaming it is red.
    const user = userEvent.setup();
    render(
      <AppShell>
        <p>hello</p>
      </AppShell>,
    );
    expect(THEME_STORAGE_KEY).toBe("helpdesk-theme");

    await user.click(screen.getByRole("button", { name: /theme/i }));
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark");
  });
});

/**
 * The two nav links that used to be 404s, and the
 * routes it deliberately does NOT link to.
 *
 * The nav is reached through `AppShell` rather than by rendering `MainNav`
 * directly, so a link that exists but is dropped from the shell (the `showNav`
 * branch) fails here too.
 */
describe("MainNav — the routes it links to", () => {
  const LINKS = [
    { name: "Chat", href: "/chat" },
    { name: "My requests", href: "/requests" },
    { name: "My access", href: "/access" },
    { name: "Approvals", href: "/approvals" },
    // `/reviews` is a page today, so it moves out of
    // `NOT_YET` and into the links the nav owes. The rule is unchanged — the
    // nav links to routes that exist and to no others.
    { name: "Reviews", href: "/reviews" },
    // `/notifications` is a page today, so it moves out
    // of `NOT_YET` and into the links the nav owes. The rule is unchanged —
    // the nav links to routes that exist and to no others.
    { name: "Notifications", href: "/notifications" },
    // `/admin/audit` is a page today, so it moves out of
    // `NOT_YET` and into the links the nav owes. The rule is unchanged — the
    // nav links to routes that exist and to no others.
    { name: "Audit log", href: "/admin/audit" },
    // `/admin/killswitch` is a page today, so the nav
    // owes it a link. The rule is unchanged — the nav links to routes that
    // exist and to no others.
    { name: "Kill switch", href: "/admin/killswitch" },
    // `/admin/rules` is a page today, so it moves out of
    // `NOT_YET` and into the links the nav owes. The rule is unchanged — the
    // nav links to routes that exist and to no others.
    { name: "Rules", href: "/admin/rules" },
    // `/admin/policies` is a page today, so it moves out
    // of `NOT_YET` and into the links the nav owes — the tenth link, and the
    // last route to be served. The rule is unchanged — the nav links
    // to routes that exist and to no others.
    { name: "Policies", href: "/admin/policies" },
  ] as const;

  it.each(LINKS)("links $name to $href", async ({ name, href }) => {
    // The ten links are asserted for a platform_admin profile only,
    // since the shell is role-aware now (the employee's four are held by
    // `role-nav.test.tsx`). `/api/me` is stubbed to answer all ten keys,
    // and each link is AWAITED, because the six gated ones only exist once the
    // read has resolved.
    // Catches a nav that names the destination but points elsewhere — a link
    // labelled "My access" going to `/chat` is invisible to every other case in
    // this file — and catches a label written as the route (`/requests` at the
    // user). `Chat` rides along so a rearranged nav that loses the existing
    // link while adding two is red here rather than at a user.
    fetchStub.mockImplementation(async (input) =>
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
        : new Response(null, { status: 204 }),
    );
    render(
      <QueryProvider>
        <AppShell>
          <p>hello</p>
        </AppShell>
      </QueryProvider>,
    );

    // Scoped to the main nav: with a query client mounted the bell renders a
    // second link to `/notifications`, so an unscoped lookup is ambiguous.
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(await within(nav).findByRole("link", { name })).toHaveAttribute("href", href);
  });

  // `NOT_YET` and its case are gone. `/admin/policies` was its
  // last entry and is a page today, so the list would be empty, and
  // `it.each([])` is an error rather than a skip. Nothing takes its place: this
  // was the last route nothing served, so the rule the case carried —
  // the nav links to routes that exist and to no others — is now held by the
  // `LINKS` table above alone.
});
