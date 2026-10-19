import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Conversation } from "@/features/chat/components/conversation";
import type { ChatMessage } from "@/features/chat/components/types";

/**
 * `typing-dots.test.tsx`: the three pulsing dots `Conversation` shows while the
 * assistant is working and no reply text has arrived. Plain render, no fetch.
 *
 * Shapes this file fixes:
 * - the bubble is `role="status"` named "Assistant is working".
 * - it holds three dots; each dot is an element whose class carries
 *   `animate-pulse`, found here by that class fragment.
 * - `Conversation` takes `working: boolean`.
 */

const SENT: ChatMessage[] = [{ role: "user", content: "Hello there" }];

function dotsOf(status: HTMLElement): Element[] {
  return Array.from(status.querySelectorAll('[class*="animate-pulse"]'));
}

describe("Conversation typing dots", () => {
  it("shows a status named 'Assistant is working' with three dots when working and no text yet", () => {
    // Catches a Conversation that ignores `working` (no dots, the chat looks
    // stuck), one that renders the bubble without the screen-reader name, and
    // one that draws a wrong number of dots.
    render(<Conversation messages={SENT} live="" working />);

    const status = screen.getByRole("status", { name: "Assistant is working" });
    expect(dotsOf(status)).toHaveLength(3);
  });

  it("hides the dots once live text arrives", () => {
    // Catches dots that stay beside the streamed text because they follow
    // `working` alone and forget that the first token replaces them.
    render(<Conversation messages={SENT} live="Hello" working />);

    expect(screen.getByText("Hello")).toBeInTheDocument();
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("hides the dots when not working", () => {
    // Catches dots that show whenever the live text is empty, which is also
    // the resting state of an idle chat, so they would pulse for ever.
    render(<Conversation messages={SENT} live="" working={false} />);

    expect(screen.queryByRole("status")).toBeNull();
  });

  it("pulses only under motion-safe, so each dot is still under reduced motion", () => {
    // Catches a dot with a bare `animate-pulse`, which pulses for people who
    // asked their system for less motion. The check reads whole class names,
    // because "motion-safe:animate-pulse" contains "animate-pulse" as text.
    render(<Conversation messages={SENT} live="" working />);

    const dots = dotsOf(screen.getByRole("status", { name: "Assistant is working" }));
    expect(dots).toHaveLength(3);
    for (const dot of dots) {
      const names = Array.from(dot.classList);
      expect(names).toContain("motion-safe:animate-pulse");
      expect(names).not.toContain("animate-pulse");
    }
  });
});
