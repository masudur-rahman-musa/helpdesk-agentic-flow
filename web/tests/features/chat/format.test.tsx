import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Conversation } from "@/features/chat/components/conversation";
import type { ChatMessage } from "@/features/chat/components/types";
import { FormattedText } from "@/features/chat/format";

/**
 * `format.test.tsx`: the small renderer that draws an assistant reply
 * (paragraphs, bullet and numbered lists, bold) and nothing else.
 * Plain render, no fetch.
 *
 * Shapes this file fixes:
 * - `FormattedText({ text })` is exported from `@/features/chat/format`.
 * - it renders into the surrounding container (no wrapper role), so tests
 *   read the container's elements.
 * - `Conversation` draws assistant messages (past and live) through it and the
 *   person's own messages as plain text.
 */

function draw(text: string): HTMLElement {
  return render(<FormattedText text={text} />).container;
}

function texts(nodes: Iterable<Element>): string[] {
  return Array.from(nodes).map((node) => node.textContent ?? "");
}

describe("FormattedText", () => {
  it("test_a_blank_line_makes_two_paragraphs", () => {
    // Catches a renderer that leaves a blank line inside one paragraph, or
    // drops the split so two thoughts run together.
    const box = draw("First.\n\nSecond.");

    expect(texts(box.querySelectorAll("p"))).toEqual(["First.", "Second."]);
  });

  it("test_a_single_line_break_stays", () => {
    // Catches a renderer that turns a single line break into a new paragraph
    // or swallows it so "One" and "Two" run together.
    const box = draw("One\nTwo");

    const paragraphs = box.querySelectorAll("p");
    expect(paragraphs).toHaveLength(1);
    const kids = Array.from(paragraphs[0]?.childNodes ?? []);
    expect(kids.map((n) => n.nodeName)).toEqual(["#text", "BR", "#text"]);
    expect(kids[0]?.textContent).toBe("One");
    expect(kids[2]?.textContent).toBe("Two");
  });

  it("test_dash_lines_make_one_bullet_list", () => {
    // Catches a renderer that makes one list per line, loses the lead-in
    // paragraph, keeps the "- " in the text, or handles "- " but not "* ".
    const dash = draw("Roles:\n- CRM viewer\n- Datadog viewer\n- GitHub developer");

    expect(texts(dash.querySelectorAll("p"))).toEqual(["Roles:"]);
    expect(dash.querySelectorAll("ul")).toHaveLength(1);
    expect(texts(dash.querySelectorAll("ul > li"))).toEqual([
      "CRM viewer",
      "Datadog viewer",
      "GitHub developer",
    ]);

    const star = draw("* CRM viewer\n* Datadog viewer");
    expect(star.querySelectorAll("ul")).toHaveLength(1);
    expect(texts(star.querySelectorAll("ul > li"))).toEqual(["CRM viewer", "Datadog viewer"]);
  });

  it("test_number_lines_make_one_numbered_list", () => {
    // Catches a renderer that draws steps as a bullet list, splits them into
    // several lists, or keeps the "1. " text so the numbers show twice.
    const box = draw("1. Ask here\n2. Say yes\n3. Wait for approval");

    expect(box.querySelectorAll("ol")).toHaveLength(1);
    expect(box.querySelectorAll("ul")).toHaveLength(0);
    expect(texts(box.querySelectorAll("ol > li"))).toEqual([
      "Ask here",
      "Say yes",
      "Wait for approval",
    ]);
  });

  it("test_double_stars_make_bold", () => {
    // Catches a renderer that leaves the stars in, bolds the whole line, or
    // loses the text after the closing pair.
    const box = draw("**REQ-0022** is waiting");

    const strong = box.querySelectorAll("strong");
    expect(texts(strong)).toEqual(["REQ-0022"]);
    expect(strong[0]?.nextSibling?.textContent).toBe(" is waiting");
    expect(box.textContent).toBe("REQ-0022 is waiting");
  });

  it("test_an_unclosed_bold_shows_as_typed", () => {
    // Catches a streaming reply whose half-typed "**REQ-00" bolds everything
    // after it or hides the stars, so the live text jumps when the pair closes.
    const box = draw("**REQ-00");

    expect(box.querySelectorAll("strong")).toHaveLength(0);
    expect(box.textContent).toBe("**REQ-00");
  });

  it("test_html_shows_as_text", () => {
    // Catches a renderer that builds an HTML string (innerHTML) so model text
    // becomes real elements and can run script.
    const text = "<img src=x onerror=alert(1)> and <script>x</script>";
    const box = draw(text);

    expect(box.querySelectorAll("img")).toHaveLength(0);
    expect(box.querySelectorAll("script")).toHaveLength(0);
    expect(box.textContent).toContain("<img src=x onerror=alert(1)>");
    expect(box.textContent).toBe(text);
  });

  it("test_links_are_never_made", () => {
    // Catches a renderer that turns a markdown link or a bare URL into an
    // anchor, which would let the model send people to any address.
    const text = "[click](http://evil.example) or http://evil.example";
    const box = draw(text);

    expect(box.querySelectorAll("a")).toHaveLength(0);
    expect(box.textContent).toBe(text);
  });

  it("test_headings_tables_and_code_stay_plain", () => {
    // Catches a renderer that grows into a markdown engine and draws a heading,
    // a table or a code block when the prompt only allows the small set.
    const text = "# Title\n\n| a | b |\n\n```\nrun this\n```";
    const box = draw(text);

    for (const tag of ["h1", "table", "code", "pre"]) {
      expect(box.querySelectorAll(tag), tag).toHaveLength(0);
    }
    expect(box.querySelectorAll("p").length).toBeGreaterThan(0);
    expect(box.textContent).toContain("# Title");
    expect(box.textContent).toContain("| a | b |");
    expect(box.textContent).toContain("```");
  });
});

describe("Conversation formatting", () => {
  it("test_assistant_lists_draw_in_past_and_live_messages", () => {
    // Catches a Conversation that formats only the finished replies (or only
    // the streaming one), so lists look right in one place and raw in the other.
    const messages: ChatMessage[] = [
      { role: "user", content: "List please" },
      { role: "assistant", content: "- A\n- B" },
    ];
    render(<Conversation messages={messages} live={"- C\n- D"} working />);

    const articles = screen.getAllByRole("article");
    const past = articles.find(
      (a) => a.dataset.role === "assistant" && a.textContent?.includes("A"),
    );
    const live = articles.find((a) => a.textContent?.includes("C"));
    expect(past?.querySelectorAll("ul")).toHaveLength(1);
    expect(live?.querySelectorAll("ul")).toHaveLength(1);
    expect(screen.getByRole("log").querySelectorAll("ul")).toHaveLength(2);
  });

  it("test_the_persons_own_message_stays_plain", () => {
    // Catches a Conversation that formats every message, so what the person
    // typed is reshaped into a list or bold instead of shown as typed.
    const messages: ChatMessage[] = [{ role: "user", content: "- A\n**B**" }];
    render(<Conversation messages={messages} live="" working={false} />);

    const log = screen.getByRole("log");
    expect(log.querySelectorAll("ul")).toHaveLength(0);
    expect(log.querySelectorAll("strong")).toHaveLength(0);
    expect(log.textContent).toContain("- A");
    expect(log.textContent).toContain("**B**");
  });
});
