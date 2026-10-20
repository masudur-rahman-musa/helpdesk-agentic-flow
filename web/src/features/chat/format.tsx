/**
 * Draws an assistant reply with the small formatting set the reply prompt
 * allows, and nothing else. A blank line starts a new paragraph, a single line
 * break stays a line break, `- ` or `* ` lines make one bullet list, `1. `
 * lines make one numbered list, and `**text**` is bold.
 *
 * Only React elements are built, never an HTML string, so tags in the text
 * show as typed (`test_html_shows_as_text`). Links, headings, tables and code
 * are never made (`test_links_are_never_made`,
 * `test_headings_tables_and_code_stay_plain`). An unclosed `**` shows as
 * typed until its pair arrives, so a streaming reply never hides words
 * (`test_an_unclosed_bold_shows_as_typed`).
 */

import { Fragment, type ReactNode } from "react";

const BULLET = /^[-*] (.*)$/;
const NUMBER = /^\d+\. (.*)$/;
const BOLD = /\*\*(.+?)\*\*/g;

type Group =
  | { kind: "text"; lines: string[] }
  | { kind: "bullets"; items: string[] }
  | { kind: "numbers"; items: string[] };

function inline(text: string): ReactNode[] {
  const parts: ReactNode[] = [];
  let last = 0;
  for (const match of text.matchAll(BOLD)) {
    const start = match.index;
    if (start > last) parts.push(text.slice(last, start));
    parts.push(<strong key={start}>{match[1]}</strong>);
    last = start + match[0].length;
  }
  if (last < text.length) parts.push(text.slice(last));
  return parts;
}

function group(block: string): Group[] {
  const groups: Group[] = [];
  for (const line of block.split("\n")) {
    const bullet = BULLET.exec(line);
    const number = bullet ? null : NUMBER.exec(line);
    const tail = groups[groups.length - 1];
    if (bullet) {
      if (tail?.kind === "bullets") tail.items.push(bullet[1] ?? "");
      else groups.push({ kind: "bullets", items: [bullet[1] ?? ""] });
    } else if (number) {
      if (tail?.kind === "numbers") tail.items.push(number[1] ?? "");
      else groups.push({ kind: "numbers", items: [number[1] ?? ""] });
    } else if (tail?.kind === "text") {
      tail.lines.push(line);
    } else {
      groups.push({ kind: "text", lines: [line] });
    }
  }
  return groups;
}

function drawGroup(item: Group, key: string): ReactNode {
  if (item.kind === "bullets") {
    return (
      <ul key={key} className="list-disc pl-5">
        {item.items.map((text, index) => (
          <li key={index}>{inline(text)}</li>
        ))}
      </ul>
    );
  }
  if (item.kind === "numbers") {
    return (
      <ol key={key} className="list-decimal pl-5">
        {item.items.map((text, index) => (
          <li key={index}>{inline(text)}</li>
        ))}
      </ol>
    );
  }
  return (
    <p key={key}>
      {item.lines.map((line, index) => (
        <Fragment key={index}>
          {index > 0 ? <br /> : null}
          {inline(line)}
        </Fragment>
      ))}
    </p>
  );
}

export function FormattedText({ text }: { text: string }) {
  const blocks = text.split(/\n[ \t]*\n/).filter((block) => block.trim().length > 0);
  return (
    <>{blocks.flatMap((block, b) => group(block).map((item, g) => drawGroup(item, `${b}-${g}`)))}</>
  );
}
