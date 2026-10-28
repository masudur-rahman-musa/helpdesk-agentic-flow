import { readFileSync, readdirSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

import ts from "typescript";
import { describe, expect, it } from "vitest";

/**
 * Guards for plain comments in the web app. Two sweeps read files from disk
 * (no render, no service): one finds internal references in comments, one caps
 * how much of `web/src` is comment. Comments come from the TypeScript scanner
 * (so strings, regex and JSX text are never mistaken for comments) and, for
 * `.css`, from a block-comment match. The finder is one function, driven by
 * one table.
 *
 * input validation: n/a, no input here shapes a key, identifier, path or
 * stored value.
 */

const SELF = fileURLToPath(import.meta.url);
const WEB_ROOT = join(dirname(SELF), "..");
const SKIP_DIRS = new Set([
  "node_modules",
  ".next",
  "dist",
  "coverage",
  "generated",
  "__generated__",
  ".git",
]);
const SKIP_FILES = new Set(["next-env.d.ts"]);
const EXTENSIONS = [".ts", ".tsx", ".mjs", ".css"];
const MAX_SHARE_PERCENT = 25;

/** [kind, pattern, ignore case, production code only] */
const REFERENCE_KINDS: readonly [string, string, boolean, boolean][] = [
  ["plan number", String.raw`\bplans?\s+\d`, true, false],
  ["section sign", String.raw`§\s*\d`, true, false],
  ["adr number", String.raw`\badr[\s-]?\d`, true, false],
  ["row number", String.raw`\brows?\s+\d`, true, false],
  ["rule number", String.raw`\brules?\s+\d`, true, false],
  ["numbered item", String.raw`\b(?:requirements|item|req)\s*#\d`, true, false],
  ["decision letter", String.raw`\b[Dd]ecision\s+[A-Z]\b`, false, false],
  ["dev path", String.raw`(?<![\w/.])de` + "v/", true, false],
  ["moved from", String.raw`\bmoved from\b`, true, false],
  ["used to live", String.raw`\bused to live\b`, true, false],
  ["lived in", String.raw`\blived in\b`, true, false],
  ["formerly", String.raw`\bformerly\b`, true, false],
  ["now lives in", String.raw`\bnow lives in\b`, true, false],
  ["was called", String.raw`\bwas called\b`, true, false],
  ["renamed from", String.raw`\brenamed from\b`, true, false],
  ["test name", String.raw`\btest_\w+`, true, true],
  ["test path", String.raw`\btests/`, true, true],
];

const DIRECTIVE =
  /^(?:noqa\b|type:\s*ignore|pragma\b|fmt:|-\*-|eslint-disable|@ts-expect-error|@ts-ignore|!|<reference)/i;

/** A tool directive: a lint switch, a type switch, a shebang or a triple-slash reference. */
function isDirective(comment: string): boolean {
  return DIRECTIVE.test(comment.replace(/^[#/*\s]+/, "").trim());
}

/** Every internal reference in `text`, case-insensitive. Test names and test paths count only in production code. */
function findReferences(text: string, production: boolean): { kind: string; offset: number }[] {
  const hits: { kind: string; offset: number }[] = [];
  for (const [kind, pattern, ignoreCase, productionOnly] of REFERENCE_KINDS) {
    if (productionOnly && !production) continue;
    for (const match of text.matchAll(new RegExp(pattern, ignoreCase ? "gi" : "g"))) {
      hits.push({ kind, offset: match.index });
    }
  }
  return hits;
}

interface Comment {
  line: number;
  endLine: number;
  ownLine: boolean;
  text: string;
}

function lineOf(text: string, pos: number): number {
  let line = 1;
  for (let i = text.indexOf("\n"); i !== -1 && i < pos; i = text.indexOf("\n", i + 1)) line += 1;
  return line;
}

function isOwnLine(text: string, pos: number): boolean {
  return text.slice(text.lastIndexOf("\n", pos - 1) + 1, pos).trim() === "";
}

function scriptKind(file: string): ts.ScriptKind {
  if (file.endsWith(".tsx")) return ts.ScriptKind.TSX;
  if (file.endsWith(".mjs")) return ts.ScriptKind.JS;
  return ts.ScriptKind.TS;
}

/**
 * Comments of a script file. A comment is either in the leading trivia of the
 * next token or in the trailing trivia of the previous one, so both are read at
 * every token. JSX text is never read as trivia, because `//` inside it is text.
 */
function scriptComments(text: string, file: string): Comment[] {
  const source = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, scriptKind(file));
  const tokens: ts.Node[] = [];
  const jsxTextStarts = new Set<number>();
  const visit = (node: ts.Node): void => {
    if (node.kind >= ts.SyntaxKind.FirstJSDocNode && node.kind <= ts.SyntaxKind.LastJSDocNode) {
      return;
    }
    const children = node.getChildren(source);
    if (children.length > 0) {
      for (const child of children) visit(child);
    } else if (node.kind === ts.SyntaxKind.JsxText) {
      jsxTextStarts.add(node.getFullStart());
    } else if (node.kind !== ts.SyntaxKind.SyntaxList) {
      tokens.push(node);
    }
  };
  visit(source);

  const found = new Map<number, Comment>();
  const add = (ranges: ts.CommentRange[] | undefined): void => {
    for (const range of ranges ?? []) {
      if (found.has(range.pos)) continue;
      const body = text.slice(range.pos, range.end);
      if (isDirective(body)) continue;
      found.set(range.pos, {
        line: lineOf(text, range.pos),
        endLine: lineOf(text, range.end - 1),
        ownLine: isOwnLine(text, range.pos),
        text: body,
      });
    }
  };
  for (const token of tokens) {
    add(ts.getLeadingCommentRanges(text, token.getFullStart()));
    if (!jsxTextStarts.has(token.getEnd())) add(ts.getTrailingCommentRanges(text, token.getEnd()));
  }
  return [...found.values()].sort((a, b) => a.line - b.line);
}

function styleComments(text: string): Comment[] {
  const found: Comment[] = [];
  for (const match of text.matchAll(/\/\*[\s\S]*?\*\//g)) {
    if (isDirective(match[0])) continue;
    found.push({
      line: lineOf(text, match.index),
      endLine: lineOf(text, match.index + match[0].length - 1),
      ownLine: isOwnLine(text, match.index),
      text: match[0],
    });
  }
  return found;
}

function commentsOf(text: string, file: string): Comment[] {
  return file.endsWith(".css") ? styleComments(text) : scriptComments(text, file);
}

/** True when comment lines are at most 25% of all lines (25 in 100 passes). */
function withinShareLimit(commentLines: number, totalLines: number): boolean {
  return commentLines * 100 <= totalLines * MAX_SHARE_PERCENT;
}

function countLines(text: string): number {
  if (text === "") return 0;
  const lines = text.split("\n");
  return text.endsWith("\n") ? lines.length - 1 : lines.length;
}

function commentLineNumbers(comments: Comment[]): Set<number> {
  const numbers = new Set<number>();
  for (const comment of comments) {
    if (!comment.ownLine) continue;
    for (let n = comment.line; n <= comment.endLine; n += 1) numbers.add(n);
  }
  return numbers;
}

function walk(dir: string): string[] {
  const found: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      if (!SKIP_DIRS.has(entry.name)) found.push(...walk(full));
    } else if (EXTENSIONS.some((ext) => entry.name.endsWith(ext)) && !SKIP_FILES.has(entry.name)) {
      found.push(full);
    }
  }
  return found;
}

function rootFiles(): string[] {
  return readdirSync(WEB_ROOT, { withFileTypes: true })
    .filter((e) => e.isFile() && (e.name.endsWith(".ts") || e.name.endsWith(".mjs")))
    .filter((e) => !SKIP_FILES.has(e.name))
    .map((e) => join(WEB_ROOT, e.name));
}

function sourceFiles(dirs: string[], withRoot: boolean): string[] {
  const all = dirs.flatMap((d) => walk(join(WEB_ROOT, d)));
  if (withRoot) all.push(...rootFiles());
  return all.filter((file) => file !== SELF).sort();
}

const FLAGGED: [text: string, kind: string, production: boolean][] = [
  ["see plan 42", "plan number", false],
  ["plans 60-63", "plan number", false],
  ["plan 11b", "plan number", false],
  ["SPEC §4.3", "section sign", false],
  ["§19", "section sign", false],
  ["ADR 0031", "adr number", false],
  ["ADR-0012", "adr number", false],
  ["row 21", "row number", false],
  ["rows 30ba", "row number", false],
  ["standing rule 38", "rule number", false],
  ["hard rule 4", "rule number", false],
  ["rule 62", "rule number", false],
  ["REQUIRE" + "MENTS #41", "numbered item", false],
  ["item #41", "numbered item", false],
  ["decision C", "decision letter", false],
  ["de" + "v/plans/80-plain-comments.md", "dev path", false],
  ["moved from shared/rag", "moved from", false],
  ["used to live in core", "used to live", false],
  ["it lived in core", "lived in", false],
  ["formerly called", "formerly", false],
  ["now lives in", "now lives in", false],
  ["was called helper", "was called", false],
  ["renamed from old_name", "renamed from", false],
  ["PLAN 42 UPPER", "plan number", false],
  ["pinned by test_seed_is_fake", "test name", true],
  ["covered in tests/shared/x.py", "test path", true],
];

const PLAIN: [text: string, production: boolean][] = [
  ["step 2 of 3", true],
  ["HTTP 404", true],
  ["retries 3 times", true],
  ["one approval row per seat", true],
  ["the tool spec is mutating", true],
  ["rules run twice", true],
  ["plan_tools picks the tool", true],
  ["/dev/null is fine", true],
  ["a decision is made here", true],
  ["test_seed_is_fake", false],
  ["see tests/shared/x.py", false],
];

describe("flags each kind of internal reference", () => {
  it.each(FLAGGED)("flags %s", (text, kind) => {
    // Catches a finder that drops this one kind of reference: each row asserts
    // the kind's own name, so a neighbouring pattern cannot cover for it.
    expect(findReferences(text, true).map((hit) => hit.kind)).toContain(kind);
  });

  it.each(FLAGGED.filter(([, , production]) => production))(
    "does not flag %s in a test file",
    (text) => {
      // Catches a finder that flags test names inside test files too, which
      // may name a sibling test.
      expect(findReferences(text, false)).toEqual([]);
    },
  );

  it.each(PLAIN)("passes plain text %s", (text, production) => {
    // Catches a finder so greedy it flags plain words: a tool name with
    // `plan`, the word `rules`, a bare `row`, an unlabelled number, or a test
    // name inside a test file.
    expect(findReferences(text, production)).toEqual([]);
  });
});

describe("the comment extractor", () => {
  it("reads real comments and never strings, regex, templates or JSX text", () => {
    // Catches an extractor that scans raw lines: `//` inside a string, a
    // regex, a template or JSX text is not a comment, while a JSX braces
    // comment, a block comment and a doc comment are.
    const source = [
      'const a = "// in a string";',
      "const b = /\\/\\/ in a regex/;",
      "const c = `// in a ${a} template`;",
      "// real line",
      "const d = (",
      "  <p>",
      "    see // in jsx text",
      "    {/* real jsx */}",
      "  </p>",
      ");",
      "/** real doc */",
      "export const e = 1; // real trailing",
      "/* real block */",
    ].join("\n");
    expect(scriptComments(source, "x.tsx").map((c) => c.text)).toEqual([
      "// real line",
      "/* real jsx */",
      "/** real doc */",
      "// real trailing",
      "/* real block */",
    ]);
  });

  it("reports line, end line and own-line for each comment", () => {
    // Catches an extractor that miscounts lines or counts a trailing comment
    // as a whole comment line, which would skew the share and the file:line list.
    const source = "// one\nconst x = 1; // two\n/* three\n   four */\n";
    expect(scriptComments(source, "x.ts").map((c) => [c.line, c.endLine, c.ownLine])).toEqual([
      [1, 1, true],
      [2, 2, false],
      [3, 4, true],
    ]);
  });

  it.each([
    "// eslint-disable-next-line no-console",
    "/* eslint-disable */",
    "// @ts-expect-error because",
    "// @ts-ignore",
    '/// <reference types="node" />',
  ])("treats the directive %s as no comment", (directive) => {
    // Catches an extractor that counts a tool directive, which would flag it
    // and add to the share for no reason.
    expect(scriptComments(`${directive}\nconst x = 1;\n`, "x.ts")).toEqual([]);
  });

  it("reads a shebang as no comment", () => {
    // Catches an extractor that counts a first-line shebang as a comment.
    expect(scriptComments("#!/usr/bin/env node\nconst x = 1;\n", "x.mjs")).toEqual([]);
  });

  it("reads css block comments and skips none inside code", () => {
    // Catches a css reader that misses multi-line blocks or their line span.
    const css = "/* one */\na { color: red; }\n/* two\n   three */\n";
    expect(styleComments(css).map((c) => [c.line, c.endLine, c.ownLine])).toEqual([
      [1, 1, true],
      [3, 4, true],
    ]);
  });
});

describe("the comment share limit", () => {
  it.each([
    [24, 100, true],
    [25, 100, true],
    [26, 100, false],
    [1, 4, true],
    [2, 7, false],
  ])("%i comment lines in %i is within the limit: %s", (comments, total, passes) => {
    // Catches a limit written as `<` instead of `<=` (25 in 100 would fail) and
    // a limit that drifts upward (26 in 100 would pass); pins the cap at 25.
    expect(withinShareLimit(comments, total)).toBe(passes);
  });
});

describe("the web sweeps", () => {
  it("web comments carry no internal references", () => {
    // Catches any comment left naming a plan, section, decision, row, rule,
    // requirement, private-folder path, folder history or (in production) a test.
    // Lists every file:line at once.
    const hits = new Set<string>();
    for (const file of sourceFiles(["src", "tests", "e2e"], true)) {
      const name = relative(WEB_ROOT, file);
      const production = name.startsWith("src") || !name.includes("/");
      const text = readFileSync(file, "utf-8");
      for (const comment of commentsOf(text, file)) {
        for (const { kind, offset } of findReferences(comment.text, production)) {
          const line = comment.line + (comment.text.slice(0, offset).match(/\n/g) ?? []).length;
          hits.add(`${name}:${line} (${kind})`);
        }
      }
    }
    expect([...hits].sort()).toEqual([]);
  });

  it("web comment share is at most 25 percent", () => {
    // Catches production code that explains too much: comment lines over all
    // lines of web/src must stay within 25%. The failure names the ten files
    // with the highest share.
    const perFile: [number, string][] = [];
    let commentTotal = 0;
    let lineTotal = 0;
    for (const file of sourceFiles(["src"], false)) {
      const text = readFileSync(file, "utf-8");
      const total = countLines(text);
      const commented = commentLineNumbers(commentsOf(text, file)).size;
      commentTotal += commented;
      lineTotal += total;
      if (total > 0) perFile.push([commented / total, relative(WEB_ROOT, file)]);
    }
    const worst = perFile
      .sort((a, b) => b[0] - a[0])
      .slice(0, 10)
      .map(([share, name]) => `${name} ${Math.round(share * 100)}%`);
    expect(
      withinShareLimit(commentTotal, lineTotal),
      `${commentTotal} of ${lineTotal} lines are comments (${((commentTotal / lineTotal) * 100).toFixed(1)}%); highest ten: ${worst.join(", ")}`,
    ).toBe(true);
  });
});
