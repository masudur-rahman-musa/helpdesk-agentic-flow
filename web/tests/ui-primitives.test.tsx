import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { StatusBadge, statusTone } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { PageHeader } from "@/components/ui/page-header";
import { Table, TableBody, TableCell, TableRow } from "@/components/ui/table";

/**
 * The shared UI kit in `components/ui/`: the status-to-tone map,
 * the badge, the button variants, and the structural primitives every page
 * will adopt in 39b. Each case pins what a page relies on: a role, a name, a
 * tone or a class, never markup for its own sake.
 */

describe("statusTone", () => {
  it("maps every known status to its tone", () => {
    // Catches a map with one row moved to the wrong colour (an `approved`
    // shown green as if executed, a `revoke` shown neutral) and asserts the
    // whole table at once, so no row is checked only by its neighbours.
    const KNOWN = [
      "pending",
      "open",
      "approved",
      "executed",
      "completed",
      "keep",
      "rejected",
      "failed",
      "revoke",
      "rolled_back",
      "expired",
    ] as const;
    const actual = Object.fromEntries(KNOWN.map((status) => [status, statusTone(status)]));
    expect(actual).toEqual({
      pending: "warning",
      open: "warning",
      approved: "info",
      executed: "success",
      completed: "success",
      keep: "success",
      rejected: "danger",
      failed: "danger",
      revoke: "danger",
      rolled_back: "neutral",
      expired: "neutral",
    });
  });

  it("maps an unknown status to neutral", () => {
    // Catches a lookup that returns `undefined` (or throws) for a status the
    // backend adds later, which would leave the badge with no tone at all.
    expect(statusTone("something_new")).toBe("neutral");
  });
});

describe("StatusBadge", () => {
  it("renders a status badge with its text and tone", () => {
    // Catches a badge that ignores `status` and renders a fixed tone, and one
    // that renders the raw status instead of the caller's label.
    render(<StatusBadge status="pending">Pending approval</StatusBadge>);
    expect(screen.getByText("Pending approval")).toHaveAttribute("data-tone", "warning");
  });

  it("renders an unknown status badge with its text, never blank", () => {
    // Catches a badge that renders nothing (returns null) when the status is
    // not in the map, so a new backend status would vanish from the page.
    render(<StatusBadge status="something_new">something_new</StatusBadge>);
    expect(screen.getByText("something_new")).toHaveAttribute("data-tone", "neutral");
  });
});

describe("Button", () => {
  it("gives each button variant its colour class", () => {
    // Catches a cva map that ignores `variant` (every button primary) or has
    // no default variant (a bare `<Button>` unstyled, the login button among
    // them). Classes are compared as whole tokens, so `bg-primary/90` alone
    // does not satisfy `bg-primary`.
    const classes = (v?: "primary" | "secondary" | "ghost" | "destructive"): string[] =>
      buttonVariants(v === undefined ? {} : { variant: v }).split(/\s+/);
    expect(classes("primary")).toContain("bg-primary");
    expect(classes("secondary")).toContain("bg-secondary");
    expect(classes("ghost")).toContain("hover:bg-accent");
    expect(classes("destructive")).toContain("bg-destructive");
    expect(classes()).toContain("bg-primary");
  });

  it("adds no default type to a button", () => {
    // Catches a Button that defaults `type="button"`, which turns the login
    // form's submit button into a button that submits nothing.
    render(<Button>Go</Button>);
    expect(screen.getByRole("button", { name: "Go" })).not.toHaveAttribute("type");
  });
});

describe("PageHeader", () => {
  it("renders the page header title as the level-one heading", () => {
    // Catches a header that renders the title as an `h2` or a styled `div`,
    // which leaves every page with no level-one heading for screen readers,
    // and one that drops the description.
    render(<PageHeader title="My requests" description="Everything you have asked for." />);
    expect(screen.getByRole("heading", { level: 1, name: "My requests" })).toBeInTheDocument();
    expect(screen.getByText("Everything you have asked for.")).toBeInTheDocument();
  });
});

describe("Card", () => {
  it("renders a card as a named region", () => {
    // Catches a Card that ignores `as` (always a `div`, so no landmark) or
    // swallows `aria-label` instead of passing it through to its root.
    render(
      <Card as="section" aria-label="Decision">
        body
      </Card>,
    );
    expect(screen.getByRole("region", { name: "Decision" })).toHaveTextContent("body");
  });
});

describe("Table", () => {
  it("renders a table by name inside a horizontal scroll wrapper", () => {
    // Catches a Table that puts `aria-label` on the wrapper instead of the
    // `<table>` (the table loses its name), and one with no scroll wrapper,
    // so a wide table pushes the whole page sideways at phone width.
    render(
      <Table aria-label="Audit events">
        <TableBody>
          <TableRow>
            <TableCell>row</TableCell>
          </TableRow>
        </TableBody>
      </Table>,
    );
    const table = screen.getByRole("table", { name: "Audit events" });
    expect(table.parentElement).toHaveClass("overflow-x-auto");
  });
});

describe("EmptyState", () => {
  it("renders the empty state text and passes props through", () => {
    // Catches an EmptyState that drops extra props (a page's `role="status"`
    // never reaches the DOM) or drops the description.
    render(<EmptyState title="No requests yet." description="Ask in chat." role="status" />);
    const status = screen.getByRole("status");
    expect(within(status).getByText("No requests yet.")).toBeInTheDocument();
    expect(within(status).getByText("Ask in chat.")).toBeInTheDocument();
  });
});
