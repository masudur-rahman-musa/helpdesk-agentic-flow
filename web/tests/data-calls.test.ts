import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative, sep } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

/**
 * Components only show data. The scan reads `web/src` off disk.
 */

const SRC = join(dirname(fileURLToPath(import.meta.url)), "..", "src");

const CALL = /\b(?:fetch|readWire|useQuery|useMutation|useQueryClient)\s*\(/;
const KEY_LITERAL = /\bqueryKey\s*:\s*\[/;

function stripComments(text: string): string {
  return text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|\s)\/\/.*$/gm, "$1");
}

function hasDataCall(text: string): boolean {
  const code = stripComments(text);
  return CALL.test(code) || KEY_LITERAL.test(code);
}

function sourceFiles(dir: string): string[] {
  const found: string[] = [];
  for (const name of readdirSync(dir)) {
    const full = join(dir, name);
    if (statSync(full).isDirectory()) found.push(...sourceFiles(full));
    else if (/\.tsx?$/.test(name)) found.push(full);
  }
  return found;
}

function rel(file: string): string {
  return relative(SRC, file).split(sep).join("/");
}

describe("data calls stay in api.ts", () => {
  it("the scan catches a data call in a sample", () => {
    // Kill: catches a matcher that misses a call shape, which would let the
    // real scan below pass over a component that still fetches.
    expect(hasDataCall("const r = await fetch(x);")).toBe(true);
    expect(hasDataCall("const r = readWire(p, s);")).toBe(true);
    expect(hasDataCall("const q = useQuery({ a: 1 });")).toBe(true);
    expect(hasDataCall("const m = useMutation({ a: 1 });")).toBe(true);
    expect(hasDataCall('const k = { queryKey: ["a"] };')).toBe(true);
    expect(hasDataCall("const q = useQueryClient();")).toBe(true);
    expect(hasDataCall("const a = useMyAccess();")).toBe(false);
    expect(hasDataCall("// we used to fetch(x) here")).toBe(false);
  });

  it("no component makes a data call", () => {
    // Kill: catches a component that still calls fetch, readWire, useQuery,
    // useMutation or useQueryClient itself instead of using a feature hook.
    const files = sourceFiles(SRC).filter((file) => {
      const path = rel(file);
      return !(
        path.endsWith("/api.ts") ||
        path === "api.ts" ||
        path.startsWith("lib/") ||
        path.endsWith("/route.ts")
      );
    });
    expect(files.length).toBeGreaterThan(20);
    const offenders = files.filter((file) => CALL.test(stripComments(readFileSync(file, "utf8"))));
    expect(offenders.map(rel)).toEqual([]);
  });

  it("every query key comes from the list", () => {
    // Kill: catches a hand-written key array anywhere but lib/query-keys.ts, so
    // two screens could drift apart on the same data.
    expect(existsSync(join(SRC, "lib", "query-keys.ts"))).toBe(true);
    const files = sourceFiles(SRC).filter((file) => rel(file) !== "lib/query-keys.ts");
    expect(files.length).toBeGreaterThan(20);
    const offenders = files.filter((file) =>
      KEY_LITERAL.test(stripComments(readFileSync(file, "utf8"))),
    );
    expect(offenders.map(rel)).toEqual([]);
  });

  it("every feature folder has an api.ts", () => {
    // Kill: catches a feature with components but no api.ts, whose data calls
    // would then sit inside the components.
    const withComponents: string[] = [];
    const walk = (dir: string): void => {
      for (const name of readdirSync(dir)) {
        const full = join(dir, name);
        if (!statSync(full).isDirectory()) continue;
        if (name === "components") withComponents.push(dir);
        else walk(full);
      }
    };
    walk(join(SRC, "features"));
    expect(withComponents.length).toBeGreaterThanOrEqual(10);
    const missing = withComponents.filter((dir) => !existsSync(join(dir, "api.ts")));
    expect(missing.map(rel)).toEqual([]);
  });
});
