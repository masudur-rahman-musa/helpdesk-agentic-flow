import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { Citations } from "@/features/chat/components/citations";
import type { Citation } from "@/features/chat/components/types";

/**
 * `citations.test.tsx`: the Sources box under a reply. It must read as a helper
 * note (its own color and a left border), hide the score, merge repeated
 * sections into one line and leave weak hits out. jsdom only.
 */

function citation(score: number): Citation {
  return {
    document: "fake-access-policy",
    title: "Fake Access Policy",
    version: "v3",
    headingPath: ["Fake Section"],
    chunkId: "00000000-0000-4000-8000-000000000abc",
    text: "Only a department manager may approve finance access.",
    score,
  };
}

function sourcesSection(): HTMLElement {
  const section = screen.getByRole("heading", { name: "Sources" }).closest("section");
  expect(section).not.toBeNull();
  return section as HTMLElement;
}

describe("Citations", () => {
  it("test_sources_box_has_its_own_color", () => {
    // Kills an implementation that leaves the box on the reply's `bg-muted`
    // (so it still reads as a second answer) or that sets the color but drops
    // the thin primary left border.
    render(<Citations citations={[citation(0.5)]} />);

    const section = sourcesSection();
    expect(section).toHaveClass("bg-card");
    expect(section).toHaveClass("border-l-2");
    expect(section).toHaveClass("border-l-primary");
    expect(section).not.toHaveClass("bg-muted");
  });

  it("test_score_is_not_shown", () => {
    // Kills an implementation that still prints the score, in any format.
    render(<Citations citations={[citation(0.030621785881252923)]} />);

    expect(document.body.textContent).not.toContain("0.0306");
    expect(document.body.textContent).not.toContain("0.03");
  });

  it("test_repeated_section_shows_once", async () => {
    // Kills an implementation that lists each passage as its own button, or
    // that merges them but shows only the first passage's text.
    const rows = ["alpha text", "beta text", "gamma text"].map((text, i) => ({
      ...citation(0.03),
      chunkId: `00000000-0000-4000-8000-00000000000${i}`,
      text,
    }));
    render(<Citations citations={rows} />);

    const buttons = screen.getAllByRole("button", { name: /Fake Access Policy/ });
    expect(buttons).toHaveLength(1);

    await userEvent.setup().click(buttons[0] as HTMLElement);

    expect(screen.getByText(/alpha text/)).toBeInTheDocument();
    expect(screen.getByText(/beta text/)).toBeInTheDocument();
    expect(screen.getByText(/gamma text/)).toBeInTheDocument();
  });

  it("test_weak_hit_is_not_listed", () => {
    // Kills an implementation that merges but never drops weak lines in the box.
    const weak: Citation = {
      ...citation(0.01),
      document: "fake-travel-policy",
      title: "Fake Travel Policy",
      chunkId: "00000000-0000-4000-8000-000000000def",
    };
    render(<Citations citations={[citation(0.0328), weak]} />);

    expect(screen.getByText(/Fake Access Policy/)).toBeInTheDocument();
    expect(screen.queryByText(/Fake Travel Policy/)).toBeNull();
  });

  it("test_no_sources_renders_nothing", () => {
    // Kills an implementation that draws an empty Sources box (heading and
    // colored frame with no rows) when the reply has no citations.
    const { container } = render(<Citations citations={[]} />);

    expect(screen.queryByText("Sources")).toBeNull();
    expect(container).toBeEmptyDOMElement();
  });
});
