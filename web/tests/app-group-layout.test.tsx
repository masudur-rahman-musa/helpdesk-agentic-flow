import { existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { readFileSync, readdirSync } from "node:fs";

import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import AppGroupLayout from "@/app/(app)/layout";
import { QueryProvider } from "@/components/layout/query-provider";

/**
 * The signed-in pages share one
 * route-group layout that renders the shell, every signed-in route has moved
 * into the `(app)` group, and no page wraps itself in `AppShell` any more.
 * jsdom only, no agent-api, no backing service.
 *
 * The layout reaches `useRouter` through the nav's sign-out button, exactly as
 * `AppShell` does, so `next/navigation` is mocked the way `shell.test.tsx`
 * does; the layout is wrapped in `QueryProvider` because the nav reads
 * `/api/me`.
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

const SRC_ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "src");

/** The ten signed-in routes, as paths under `src/app` relative to the app dir. */
const SIGNED_IN_ROUTES = [
  "chat",
  "access",
  "requests",
  "approvals",
  "reviews",
  "notifications",
  "admin/audit",
  "admin/killswitch",
  "admin/policies",
  "admin/rules",
] as const;

beforeEach(() => {
  replaceMock.mockReset();
  refreshMock.mockReset();
  pushMock.mockReset();
  fetchStub.mockReset();
  fetchStub.mockResolvedValue(new Response(null, { status: 204 }));
});

describe("app group layout", () => {
  it("test_layout_renders_the_shell_around_its_children", () => {
    // Catches a layout that forgets the shell (renders children bare) or drops
    // the children (renders only chrome): both the brand/nav AND the child
    // inside <main> are asserted, so neither mistake stays green.
    const { container } = render(
      <QueryProvider>
        <AppGroupLayout>
          <p>hi</p>
        </AppGroupLayout>
      </QueryProvider>,
    );
    expect(screen.getByText("Helpdesk")).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Main" })).toBeInTheDocument();
    const main = container.querySelector("main#main-content");
    expect(main).not.toBeNull();
    expect(main).toHaveTextContent("hi");
  });
});

describe("app group — the move", () => {
  it.each(SIGNED_IN_ROUTES)("test_every_signed_in_route_sits_in_the_app_group: %s", (route) => {
    // Catches a route left at its old path, or a move that forgot one of the
    // ten: the new path must exist AND the old path must be gone, so a copy
    // that leaves the original behind is red too.
    expect(existsSync(join(SRC_ROOT, "app", "(app)", route, "page.tsx"))).toBe(true);
    expect(existsSync(join(SRC_ROOT, "app", route, "page.tsx"))).toBe(false);
  });
});

/** Every `page.tsx` under `src/app/(app)`, as absolute paths. */
function appGroupPages(): string[] {
  const root = join(SRC_ROOT, "app", "(app)");
  const out: string[] = [];
  const walk = (dir: string): void => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const full = join(dir, entry.name);
      if (entry.isDirectory()) {
        walk(full);
      } else if (entry.name === "page.tsx") {
        out.push(full);
      }
    }
  };
  walk(root);
  return out;
}

describe("app group — no page imports the shell", () => {
  it("test_no_signed_in_page_imports_the_shell", () => {
    // Unhappy path: catches a moved page that still wraps itself in <AppShell>,
    // which would nest the shell twice under the group layout. The shell must
    // come only from (app)/layout.tsx.
    const offenders = appGroupPages().filter((file) => {
      const source = readFileSync(file, "utf8");
      return source.includes("AppShell") || source.includes("@/components/layout/app-shell");
    });
    expect(offenders).toEqual([]);
  });
});
