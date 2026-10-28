import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AppShell } from "@/components/layout/app-shell";
import { orderedLabels } from "@/components/layout/role-header";
import { UserMenu } from "@/components/layout/user-menu";

/**
 * `user-menu.test.tsx`: the signed-in person's name and account
 * type become ONE button at the top right of the "Account" group, and Sign out
 * lives in the panel that button opens. jsdom, a stubbed `fetch`, no agent-api.
 *
 * Shapes this file fixes:
 * - the trigger's accessible name is `Fake Person One, Employee` (the name,
 *   a comma, the principal labels).
 * - the group is `role="group"` named `Signed in user` (kept from an earlier version), and
 *   holds the trigger and the open panel.
 * - the panel is located by its only content: the button named `Sign out`.
 * - an outside click is a `pointerdown` on `document.body`, fired with
 *   `fireEvent` so focus does NOT move; the focus-leaves case moves focus on
 *   its own. Each dismissal path is therefore decided by its own handler.
 * - `UserMenu` and `orderedLabels` are imported to pin the export names
 *   (`components/user-menu.tsx`, `components/role-header.tsx`).
 *
 * `next/navigation` is mocked as `tests/shell.test.tsx` mocks it, with the
 * spies shared so the sign-out case can read them.
 */

const replaceMock = vi.fn<(href: string) => void>();
const refreshMock = vi.fn<() => void>();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: replaceMock, refresh: refreshMock, push: vi.fn() }),
  usePathname: () => "/chat",
}));

const GROUP_NAME = "Signed in user";
const TRIGGER_NAME = "Fake Person One, Employee";
const DISPLAY_NAME = "Fake Person One";

const fetchStub = vi.fn<typeof fetch>();
let meAnswer: () => Promise<Response>;
let meCalls = 0;

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

beforeEach(() => {
  meCalls = 0;
  meAnswer = async () =>
    json({
      display_name: DISPLAY_NAME,
      principals: ["employee"],
      tabs: ["chat", "requests", "access", "notifications"],
    });
  replaceMock.mockReset();
  refreshMock.mockReset();
  fetchStub.mockReset();
  fetchStub.mockImplementation(async (input) => {
    const url = String(input);
    if (url === "/api/me") {
      meCalls += 1;
      return meAnswer();
    }
    if (url === "/api/logout") {
      return new Response(null, { status: 204 });
    }
    // The bell and anything else the shell reads: a refusal it stays silent on.
    return json({ code: "INTERNAL" }, 500);
  });
  vi.stubGlobal("fetch", fetchStub);
});

function newClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

function renderShell(client: QueryClient = newClient(), showNav = true) {
  return render(
    <QueryClientProvider client={client}>
      <AppShell showNav={showNav}>
        <p>hello</p>
      </AppShell>
    </QueryClientProvider>,
  );
}

async function trigger(): Promise<HTMLElement> {
  return screen.findByRole("button", { name: TRIGGER_NAME });
}

function signOutButton(): HTMLElement | null {
  return screen.queryByRole("button", { name: "Sign out" });
}

async function settled(client: QueryClient): Promise<void> {
  await waitFor(() => expect(meCalls).toBeGreaterThan(0));
  await waitFor(() => expect(client.isFetching()).toBe(0), { timeout: 5_000 });
}

describe("the menu's open and closed states", () => {
  it("is closed by default, with no Sign out in the page", async () => {
    // Catches a menu that renders its panel hidden by CSS (or always open), so
    // Sign out is in the page before the name is clicked. `aria-expanded` is
    // asserted as the string "false", the value assistive tech reads, not as an
    // absent attribute.
    renderShell();

    const button = await trigger();

    expect(button).toHaveAttribute("aria-expanded", "false");
    expect(signOutButton()).toBeNull();
  });

  it("opens on a click, with Sign out inside the Signed in user group", async () => {
    // Catches a trigger that flips `aria-expanded` but renders the panel
    // elsewhere (a portal, or outside the group that the outside-click test
    // measures against), and a panel that opens without the flag following.
    const user = userEvent.setup();
    renderShell();

    const button = await trigger();
    await user.click(button);

    expect(button).toHaveAttribute("aria-expanded", "true");
    const group = screen.getByRole("group", { name: GROUP_NAME });
    expect(within(group).getByRole("button", { name: "Sign out" })).toBeInTheDocument();
  });

  it("closes on a second click on the trigger", async () => {
    // Catches a trigger that only ever opens (open set to true, not toggled):
    // the first click passes it, the second leaves the panel up. The click is
    // inside the group, so the outside-click handler cannot be what closes it.
    const user = userEvent.setup();
    renderShell();

    const button = await trigger();
    await user.click(button);
    await user.click(button);

    expect(button).toHaveAttribute("aria-expanded", "false");
    expect(signOutButton()).toBeNull();
  });

  it("closes on Escape and returns focus to the trigger", async () => {
    // Catches an Escape handler that closes the panel but leaves focus on the
    // removed Sign out button (the page body then holds it), which a keyboard
    // user loses. Focus is MOVED to Sign out first: a trigger that never lost
    // focus would hold it after Escape whether or not the handler returns it.
    const user = userEvent.setup();
    renderShell();

    const button = await trigger();
    await user.click(button);
    const item = signOutButton();
    expect(item).not.toBeNull();
    act(() => item?.focus());
    expect(item).toHaveFocus();
    await user.keyboard("{Escape}");

    expect(button).toHaveAttribute("aria-expanded", "false");
    expect(signOutButton()).toBeNull();
    expect(button).toHaveFocus();
  });

  it("closes on a pointerdown outside the group", async () => {
    // Catches a menu with no outside-click handler. The event is fired on the
    // body without moving focus, so the focus-leaves path cannot close it: the
    // pointerdown handler is the only thing that can flip this result.
    const user = userEvent.setup();
    renderShell();

    const button = await trigger();
    await user.click(button);
    fireEvent.pointerDown(document.body);

    await waitFor(() => expect(button).toHaveAttribute("aria-expanded", "false"));
    expect(signOutButton()).toBeNull();
  });

  it("stays open on a pointerdown inside the group", async () => {
    // Catches an outside-click handler that listens on the document and never
    // checks the target, closing the panel on the press that was meant for
    // Sign out (the click would then land on nothing).
    const user = userEvent.setup();
    renderShell();

    const button = await trigger();
    await user.click(button);
    const item = signOutButton();
    expect(item).not.toBeNull();
    if (item !== null) {
      fireEvent.pointerDown(item);
    }

    expect(button).toHaveAttribute("aria-expanded", "true");
    expect(signOutButton()).not.toBeNull();
  });

  it("closes when focus leaves the group", async () => {
    // Catches a menu that survives a Tab out of it: keyboard focus
    // moves to the theme toggle, no pointer is involved and Escape is not
    // pressed, so only the focus-leaves handler can close the panel.
    const user = userEvent.setup();
    renderShell();

    const button = await trigger();
    await user.click(button);
    const toggle = screen.getByRole("button", { name: /^Switch to (dark|light) theme$/ });
    act(() => toggle.focus());

    await waitFor(() => expect(button).toHaveAttribute("aria-expanded", "false"));
    expect(signOutButton()).toBeNull();
  });
});

describe("signing out from the menu", () => {
  it("POSTs /api/logout, then replaces to /login and refreshes", async () => {
    // Catches a menu item that closes the panel without signing out (a plain
    // button with no handler), one that posts to another path, and one that
    // navigates without the POST, which would leave the cookie alive. The mock
    // router is the one the tree mounts, so both calls are the button's own.
    const user = userEvent.setup();
    renderShell();

    await user.click(await trigger());
    await user.click(screen.getByRole("button", { name: "Sign out" }));

    await waitFor(() => expect(replaceMock).toHaveBeenCalledWith("/login"));
    expect(fetchStub).toHaveBeenCalledWith("/api/logout", { method: "POST" });
    expect(refreshMock).toHaveBeenCalledTimes(1);
  });
});

describe("while /api/me is pending, has failed or has no provider", () => {
  async function expectPlainSignOut(): Promise<void> {
    expect(screen.getByRole("button", { name: "Sign out" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: TRIGGER_NAME })).toBeNull();
    expect(screen.queryByRole("group", { name: GROUP_NAME })).toBeNull();
    expect(document.querySelector("[aria-expanded]")).toBeNull();
  }

  it("shows a plain Sign out and no trigger while the read is pending", async () => {
    // Catches a shell that hides Sign out until `/me` answers (signing
    // out never depends on `/me`), and one that renders an empty trigger. The
    // read never answers, so this is the state a slow `/me` leaves the page in.
    meAnswer = () => new Promise<Response>(() => undefined);
    renderShell();
    await waitFor(() => expect(meCalls).toBeGreaterThan(0));

    await expectPlainSignOut();
  });

  it("shows a plain Sign out and no trigger when agent-api answers 500", async () => {
    // Catches a shell that keeps the menu (or hides Sign out) after a failed
    // read. The client is awaited until it has settled, so this is the state
    // AFTER the failure, not the pending state the previous case already pins.
    meAnswer = async () => json({ code: "INTERNAL" }, 500);
    const client = newClient();
    renderShell(client);

    await settled(client);

    await expectPlainSignOut();
  });

  it("shows a plain Sign out and no trigger with no QueryClientProvider above", async () => {
    // Catches a `UserMenu` that calls a provider-needing hook unguarded (it would
    // throw here), and one that renders nothing without the provider: the
    // provider-free shell renders in `tests/shell.test.tsx` need Sign out.
    render(
      <AppShell>
        <p>hello</p>
      </AppShell>,
    );

    await expectPlainSignOut();
  });

  it("signs out through the plain button when /api/me is pending", async () => {
    // Catches a fallback button rendered but not wired (a bare label): the
    // plain button must post and navigate exactly as the menu item does.
    meAnswer = () => new Promise<Response>(() => undefined);
    const user = userEvent.setup();
    renderShell();
    await waitFor(() => expect(meCalls).toBeGreaterThan(0));

    await user.click(screen.getByRole("button", { name: "Sign out" }));

    await waitFor(() => expect(replaceMock).toHaveBeenCalledWith("/login"));
    expect(fetchStub).toHaveBeenCalledWith("/api/logout", { method: "POST" });
  });
});

describe("the sign-in shell (showNav off)", () => {
  it("shows neither the menu nor Sign out", async () => {
    // Catches a shell that mounts `UserMenu` or the fallback button outside the
    // `showNav` guard, putting a sign-out on the sign-in page. `/me` WOULD
    // resolve to a full profile, so a menu rendered regardless of `showNav`
    // has data to show and is red.
    renderShell(newClient(), false);
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });

    expect(screen.queryByRole("button", { name: TRIGGER_NAME })).toBeNull();
    expect(screen.queryByRole("button", { name: "Sign out" })).toBeNull();
    expect(screen.queryByRole("group", { name: GROUP_NAME })).toBeNull();
  });
});

describe("placement in the header", () => {
  it("orders the Account group as bell, theme toggle, user menu", async () => {
    // Catches a menu placed before the bell or the toggle, and one left outside
    // the Account group. The three controls are read in DOM order from inside
    // the group, so an order flip or a control outside it both fail.
    renderShell();

    const button = await trigger();
    const account = screen.getByRole("group", { name: "Account" });
    const bell = within(account).getByRole("link", { name: /^Notifications/ });
    const toggle = within(account).getByRole("button", { name: /^Switch to (dark|light) theme$/ });

    expect(account).toContainElement(button);
    const follows = Node.DOCUMENT_POSITION_FOLLOWING;
    expect(bell.compareDocumentPosition(toggle) & follows).toBeTruthy();
    expect(toggle.compareDocumentPosition(button) & follows).toBeTruthy();
  });

  it("has no Signed in user group between the brand row and the main nav", async () => {
    // Catches the strip row surviving between the rows (the place): the
    // group must live inside the Account group, so the nodes between the
    // header's top row and `MainNav` hold no such group.
    renderShell();

    await trigger();
    const nav = screen.getByRole("navigation", { name: "Main" });
    const account = screen.getByRole("group", { name: "Account" });
    const groups = screen.getAllByRole("group", { name: GROUP_NAME });

    expect(groups).toHaveLength(1);
    expect(account).toContainElement(groups[0] ?? null);
    expect(nav.previousElementSibling).not.toBeNull();
    expect(nav.previousElementSibling).not.toHaveAttribute("aria-label", GROUP_NAME);
    expect(nav.previousElementSibling?.getAttribute("role")).not.toBe("group");
  });
});

describe("the trigger's text", () => {
  it("shows the display name, then the account type", async () => {
    // Catches a trigger that shows only the name or only the type, and one with
    // the type first. Read from the visible text, not the accessible name.
    renderShell();

    const button = await trigger();
    const text = button.textContent ?? "";

    expect(text).toContain(DISPLAY_NAME);
    expect(text).toContain("Employee");
    expect(text.indexOf(DISPLAY_NAME)).toBeLessThan(text.indexOf("Employee"));
  });

  it("exports orderedLabels in the enum's order, not the wire's", () => {
    // Catches `orderedLabels` mapping the wire array as it arrived once it moves
    // to an export (enum order). The wire sends `system_owner`
    // BEFORE `manager`; the enum has `manager` first.
    expect(orderedLabels(["system_owner", "manager"])).toEqual(["Manager", "System owner"]);
  });

  it("is rendered by the exported UserMenu inside a provider", async () => {
    // Catches a `UserMenu` export that only works when mounted by `AppShell`
    // (it reads shell context): rendered alone it must still show the trigger.
    render(
      <QueryClientProvider client={newClient()}>
        <UserMenu />
      </QueryClientProvider>,
    );

    expect(await trigger()).toHaveAttribute("aria-expanded", "false");
  });
});

describe("the name button looks clickable at rest", () => {
  it("has a resting background class, with the hover look kept", async () => {
    // Catches a button that only gains a background on hover (`hover:bg-muted`
    // or `hover:bg-accent` alone), so nobody sees it is a place to click. The
    // class list is read as whole tokens: `hover:bg-muted` does not count as
    // `bg-muted`. The hover token stays, so hover still differs from rest.
    renderShell();

    const button = await trigger();

    expect(button.classList.contains("bg-muted")).toBe(true);
    expect(button.classList.contains("hover:bg-accent")).toBe(true);
  });

  it("still opens on a click and closes on Escape", async () => {
    // Catches a restyle that breaks the disclosure (a button swapped for a link,
    // or the open flag no longer following the click). Escape is the unhappy
    // path: after it the panel must be gone and focus must be on the trigger.
    const user = userEvent.setup();
    renderShell();

    const button = await trigger();
    await user.click(button);
    expect(button).toHaveAttribute("aria-expanded", "true");
    expect(signOutButton()).not.toBeNull();
    await user.keyboard("{Escape}");

    expect(button).toHaveAttribute("aria-expanded", "false");
    expect(signOutButton()).toBeNull();
    expect(button).toHaveFocus();
  });
});
