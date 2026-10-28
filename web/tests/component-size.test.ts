import { readFileSync, readdirSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

/**
 * Every `.tsx` file under `web/src` is 250 lines or fewer, and the
 * five big components were split into smaller parts. Reads files by
 * path, no render, no service.
 */

const WEB_ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const SRC_ROOT = join(WEB_ROOT, "src");
const LIMIT = 250;

/** The file's lines; one final newline does not add a line. */
function countLines(text: string): number {
  if (text === "") return 0;
  const lines = text.split("\n");
  return text.endsWith("\n") ? lines.length - 1 : lines.length;
}

function tsxFiles(dir: string): string[] {
  const found: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) found.push(...tsxFiles(full));
    else if (entry.name.endsWith(".tsx")) found.push(full);
  }
  return found;
}

/** [new part, the old file it was split from], paths under web/. */
const SPLITS: ReadonlyArray<readonly [string, string]> = [
  [
    "src/features/admin/audit/components/audit-filter-form.tsx",
    "src/features/admin/audit/components/audit-search.tsx",
  ],
  [
    "src/features/admin/audit/components/audit-event-table.tsx",
    "src/features/admin/audit/components/audit-search.tsx",
  ],
  [
    "src/features/admin/policies/components/policy-versions-table.tsx",
    "src/features/admin/policies/components/policies-board.tsx",
  ],
  [
    "src/features/reviews/components/review-items.tsx",
    "src/features/reviews/components/review-queue.tsx",
  ],
  [
    "src/features/approvals/components/approval-details.tsx",
    "src/features/approvals/components/approval-queue.tsx",
  ],
];

const OLD_FILES: readonly string[] = [
  "src/features/admin/audit/components/audit-search.tsx",
  "src/features/admin/policies/components/policies-board.tsx",
  "src/features/reviews/components/review-queue.tsx",
  "src/features/approvals/components/approval-queue.tsx",
  "src/features/admin/killswitch/components/kill-switch-toggle.tsx",
];

describe("component size guard", () => {
  it("test_every_component_is_at_most_250_lines", () => {
    /** Catches a split that leaves any .tsx file over 250 lines (today five are). */
    const over = tsxFiles(SRC_ROOT)
      .map((file) => ({
        file: relative(WEB_ROOT, file),
        lines: countLines(readFileSync(file, "utf8")),
      }))
      .filter((f) => f.lines > LIMIT)
      .map((f) => `${f.file}: ${f.lines} lines`);
    expect(over).toEqual([]);
  });

  it("test_the_line_count_takes_the_limit_itself", () => {
    /** Catches a counter that is off by one at 250/251 or counts the final newline as a line. */
    const lines = (n: number) => Array.from({ length: n }, () => "x").join("\n");
    expect(countLines(lines(250))).toBe(LIMIT);
    expect(countLines(lines(250) + "\n")).toBe(LIMIT);
    expect(countLines(lines(251))).toBe(LIMIT + 1);
    expect(countLines(lines(251) + "\n")).toBe(LIMIT + 1);
  });

  it("test_the_guard_finds_the_split_files", () => {
    /** Catches a file walk that finds nothing or misses a named part, which would let the size check pass empty. */
    const found = new Set(tsxFiles(SRC_ROOT).map((f) => relative(WEB_ROOT, f)));
    const named = [...SPLITS.map(([part]) => part), ...OLD_FILES];
    expect(named).toHaveLength(10);
    const missing = named.filter((path) => !found.has(path));
    expect(missing).toEqual([]);
  });

  it.each(SPLITS)("test_the_new_parts_do_not_import_their_parent: %s", (part, parent) => {
    /** Catches a new part that imports the file it was split from, which forms an import loop. */
    const parentName = parent.replace(/^.*\//, "").replace(/\.tsx$/, "");
    const source = readFileSync(join(WEB_ROOT, part), "utf8");
    const specifiers = [...source.matchAll(/from\s+["']([^"']+)["']/g)].map((m) => m[1] ?? "");
    const importsParent = specifiers.filter(
      (s) => s === `./${parentName}` || s.endsWith(`/${parentName}`),
    );
    expect(importsParent).toEqual([]);
  });
});
