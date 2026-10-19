import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Citations } from "@/features/chat/components/citations";
import type { Citation } from "@/features/chat/components/types";

/**
 * `citations.test.tsx`: the Sources box under a reply. It must read as a helper
 * note (its own color and a left border) and show a short score. jsdom only.
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

  it("test_score_shows_four_decimals", () => {
    // Kills an implementation that prints the raw float, or rounds to a
    // different number of decimals (two or three) instead of exactly four.
    render(<Citations citations={[citation(0.030621785881252923)]} />);

    expect(screen.getByText("0.0306")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("0.030621785881252923");
    expect(document.body.textContent).not.toContain("0.03062");
  });

  it("test_missing_score_shows_zero", () => {
    // Kills an implementation that formats a zero score as empty, NaN or a
    // bare "0" instead of padding to four decimals.
    render(<Citations citations={[citation(0)]} />);

    expect(screen.getByText("0.0000")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("NaN");
  });

  it("test_no_sources_renders_nothing", () => {
    // Kills an implementation that draws an empty Sources box (heading and
    // colored frame with no rows) when the reply has no citations.
    const { container } = render(<Citations citations={[]} />);

    expect(screen.queryByText("Sources")).toBeNull();
    expect(container).toBeEmptyDOMElement();
  });
});
