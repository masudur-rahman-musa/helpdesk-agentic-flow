import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";

import { AppShell } from "@/components/app-shell";
import { DEMO_BANNER_TEXT } from "@/components/demo-banner";
import { THEME_STORAGE_KEY } from "@/components/theme-toggle";

/**
 * The authenticated shell: demo banner, keyboard reachability and the dark-mode
 * toggle that persists.
 *
 * Value the plan left open (a project rule, recorded in 22a's Deviations):
 * the theme control is located as a button whose accessible name matches
 * /theme/i, so the implementation owes it a name containing that word.
 */

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
 * An earlier change §6 additions: the two nav links this plan stops being 404s, and the
 * routes it deliberately does NOT link to (decision E). Additions only —
 * nothing above this comment is edited.
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
  ] as const;

  it.each(LINKS)("links $name to $href", ({ name, href }) => {
    // Catches a nav that names the destination but points elsewhere — a link
    // labelled "My access" going to `/chat` is invisible to every other case in
    // this file — and catches a label written as the route (`/requests` at the
    // user). `Chat` rides along so a rearranged nav that loses the existing
    // link while adding two is red here rather than at a user.
    render(
      <AppShell>
        <p>hello</p>
      </AppShell>,
    );

    expect(screen.getByRole("link", { name })).toHaveAttribute("href", href);
  });

  const NOT_YET = ["/reviews", "/admin/audit", "/admin/rules", "/notifications"];

  it.each(NOT_YET)("carries no link to %s, which does not exist yet", (href) => {
    // 22a's rule, and the reason decision E adds exactly two links: a link to a
    // route no page serves is worse than no link, because the user pays a 404
    // to find out. Catches a nav written ahead of rows 23c–24 — the shape a
    // "finish the nav while we are here" edit produces, which nothing else in
    // this file would notice.
    const { container } = render(
      <AppShell>
        <p>hello</p>
      </AppShell>,
    );

    expect(container.querySelector(`a[href="${href}"]`)).toBeNull();
  });
});
