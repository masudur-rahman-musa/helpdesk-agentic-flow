import { existsSync, readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

/**
 * An earlier change: `web/README.md` stays true to the code. It has four sections in
 * order, every path it names exists, every feature folder is named, the request
 * flow names its steps in order, every pnpm script and make target it names is
 * real, and it names no private working file. Reads files by path, no render,
 * no service.
 *
 * input validation: §5 n/a — no input here shapes a key, identifier, path or
 * stored value.
 */

const WEB_ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const README_PATH = join(WEB_ROOT, "README.md");
const MAKEFILE_PATH = join(WEB_ROOT, "..", "Makefile");
const FEATURES_ROOT = join(WEB_ROOT, "src", "features");

const SECTIONS = ["Folder layout", "How a request flows", "Run it", "Test it"] as const;
const PNPM_EXTRA = ["install", "exec"] as const;
const PRIVATE_NAMES = ["dev/notes", "REQUIREMENTS", "his.md", "dev/plans"] as const;

/** Throws (ENOENT) when the README is missing, so every README test fails for that reason. */
function readReadme(): string {
  return readFileSync(README_PATH, "utf8");
}

/** Backticked paths that start with src/, tests/ or e2e/, as written. */
function findPaths(text: string): string[] {
  return [...text.matchAll(/`((?:src|tests|e2e)\/[^`\s]*)`/g)].map((m) => m[1] ?? "");
}

/** The named paths that do not resolve to a file or folder under web/. */
function missingPaths(text: string): string[] {
  return findPaths(text).filter((p) => !existsSync(join(WEB_ROOT, p.replace(/\/+$/, ""))));
}

/** Line index of a heading with exactly this text, or -1. */
function headingIndex(lines: string[], heading: string): number {
  return lines.findIndex((l) => new RegExp(`^#{1,6}\\s+${heading}\\s*$`).test(l));
}

/** The text under a heading, up to the next heading of the same or a higher level; "" if absent. */
function section(text: string, heading: string): string {
  const lines = text.split("\n");
  const start = headingIndex(lines, heading);
  if (start === -1) return "";
  const level = (lines[start] ?? "").match(/^#+/)?.[0].length ?? 1;
  const body: string[] = [];
  for (const line of lines.slice(start + 1)) {
    const m = line.match(/^(#+)\s/);
    if (m && (m[1] ?? "").length <= level) break;
    body.push(line);
  }
  return body.join("\n");
}

/** Names after `pnpm`, skipping a `-C <dir>` option. */
function pnpmScripts(text: string): string[] {
  return [...text.matchAll(/\bpnpm\s+(?:-C\s+\S+\s+)?([a-zA-Z0-9:_-]+)/g)].map((m) => m[1] ?? "");
}

/** Names after `make`. */
function makeTargets(text: string): string[] {
  return [...text.matchAll(/\bmake\s+([a-zA-Z0-9_-]+)/g)].map((m) => m[1] ?? "");
}

/** Targets defined in a Makefile: `name:` lines plus the names on the .PHONY line. */
function makefileTargets(text: string): Set<string> {
  const found = new Set<string>();
  for (const line of text.split("\n")) {
    const phony = line.match(/^\.PHONY\s*:(.*)$/);
    if (phony) {
      for (const name of (phony[1] ?? "").split(/\s+/).filter(Boolean)) found.add(name);
      continue;
    }
    const target = line.match(/^([a-zA-Z0-9_-]+):/);
    if (target) found.add(target[1] ?? "");
  }
  return found;
}

function featureFolders(): string[] {
  return readdirSync(FEATURES_ROOT, { withFileTypes: true })
    .filter((e) => e.isDirectory())
    .map((e) => e.name);
}

describe("web README (plan 79)", () => {
  it("test_the_readme_has_the_four_sections_in_order", () => {
    /** Catches a README that drops, renames or reorders one of the four sections. */
    const lines = readReadme().split("\n");
    const positions = SECTIONS.map((s) => headingIndex(lines, s));
    expect(positions.every((p) => p >= 0)).toBe(true);
    expect([...positions].sort((a, b) => a - b)).toEqual(positions);
  });

  it("test_every_path_the_readme_names_exists", () => {
    /** Catches a README that names a src/, tests/ or e2e/ path that is not on disk, or names none at all. */
    const text = readReadme();
    expect(findPaths(text).length).toBeGreaterThan(0);
    expect(missingPaths(text)).toEqual([]);
  });

  it("test_the_path_check_reports_a_path_that_does_not_exist", () => {
    /** Catches a path checker that passes every path, which would let the README name dead files. */
    const text = "See `src/lib/no-such-file.ts` and `src/lib/agent-api.ts` for details.";
    expect(missingPaths(text)).toEqual(["src/lib/no-such-file.ts"]);
  });

  it("test_the_layout_names_every_feature_folder", () => {
    /** Catches a Folder layout section that leaves out a folder under src/features/. */
    const layout = section(readReadme(), "Folder layout");
    const features = featureFolders();
    expect(features.length).toBeGreaterThan(0);
    const unnamed = features.filter(
      (f) => !new RegExp(`(^|[^a-zA-Z0-9_-])${f}([^a-zA-Z0-9_-]|$)`).test(layout),
    );
    expect(unnamed).toEqual([]);
  });

  it("test_the_flow_names_its_steps_in_order", () => {
    /** Catches a flow section that skips a step or lists browser, API route, agent-api.ts and agent-api out of order. */
    const flow = section(readReadme(), "How a request flows");
    const lower = flow.toLowerCase();
    const browser = lower.indexOf("browser");
    const route = flow.indexOf("src/app/api/");
    const lib = flow.indexOf("src/lib/agent-api.ts");
    const libEnd = lib + "src/lib/agent-api.ts".length;
    const agentApi = lib === -1 ? -1 : flow.indexOf("agent-api", libEnd);
    expect([browser, route, lib, agentApi].every((p) => p >= 0)).toBe(true);
    expect(browser).toBeLessThan(route);
    expect(route).toBeLessThan(lib);
    expect(lib).toBeLessThan(agentApi);
  });

  it("test_every_pnpm_script_the_readme_names_is_in_package_json", () => {
    /** Catches a README that tells the reader to run a pnpm script that does not exist, or gives no pnpm command. */
    const names = pnpmScripts(readReadme());
    expect(names.length).toBeGreaterThan(0);
    const pkg = JSON.parse(readFileSync(join(WEB_ROOT, "package.json"), "utf8")) as {
      scripts: Record<string, string>;
    };
    const known = new Set<string>([...Object.keys(pkg.scripts), ...PNPM_EXTRA]);
    expect(names.filter((n) => !known.has(n))).toEqual([]);
  });

  it("test_every_make_target_the_readme_names_is_in_the_makefile", () => {
    /** Catches a README that tells the reader to run a make target the root Makefile does not define. */
    const names = makeTargets(readReadme());
    const known = makefileTargets(readFileSync(MAKEFILE_PATH, "utf8"));
    expect(names.filter((n) => !known.has(n))).toEqual([]);
  });

  it("test_the_readme_names_no_private_working_file", () => {
    /** Catches a README that leaks a private working file name into a public repo. */
    const text = readReadme();
    expect(PRIVATE_NAMES.filter((n) => text.includes(n))).toEqual([]);
  });
});
