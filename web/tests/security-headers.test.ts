import { describe, expect, it } from "vitest";

import nextConfig from "../next.config";

/**
 * `security-headers.test.ts`: the six response headers the web app
 * must answer on every route, plus the two config flags that were already true
 * and must stay true. (No basename collision in the tree.)
 *
 * The unit under test is `web/next.config.ts`'s `headers()`, read as
 * CONFIGURATION rather than off a rendered page: a policy which
 * breaks a real script still goes green here, and `make e2e` is what catches
 * that. What this file does catch is every way the configuration can be wrong.
 *
 * Every expectation is a literal, character for character. Rebuilding the CSP here from the same
 * parts the implementation uses
 * would pass for whatever policy it assembled, including one with
 * `default-src *`.
 */

const EVERY_ROUTE = "/(.*)";

const CONTENT_SECURITY_POLICY =
  "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'self'; form-action 'self'";

const PLANNED_HEADERS: ReadonlyArray<{ key: string; value: string }> = [
  { key: "Content-Security-Policy", value: CONTENT_SECURITY_POLICY },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
  { key: "Strict-Transport-Security", value: "max-age=63072000; includeSubDomains" },
];

type HeaderPair = { key: string; value: string };
type HeaderBlock = { source: string; headers: ReadonlyArray<HeaderPair> };

async function headerBlocks(): Promise<ReadonlyArray<HeaderBlock>> {
  const blocks = await nextConfig.headers?.();
  expect(blocks).toBeDefined();
  return blocks as ReadonlyArray<HeaderBlock>;
}

function onlyBlock(blocks: ReadonlyArray<HeaderBlock>): HeaderBlock {
  const block = blocks[0];
  if (!block) {
    throw new Error("next.config.ts returned no header block at all");
  }
  return block;
}

function answeredHeaders(block: HeaderBlock): Map<string, string> {
  return new Map(block.headers.map((header) => [header.key, header.value]));
}

describe("web security headers", () => {
  it("applies one header block to every route", async () => {
    // Kills a config with no headers() at all, and one that scopes the block to
    // a single page or to "/" — a source of "/" matches the home route only, so
    // /chat, /requests and every API proxy route would answer with no CSP at
    // all while a membership-only assertion on the values stayed green.
    const blocks = await headerBlocks();

    expect(blocks).toHaveLength(1);
    expect(onlyBlock(blocks).source).toBe(EVERY_ROUTE);
  });

  it("answers the six security headers with exactly the expected values", async () => {
    // Kills every weakened value, one by one: a CSP with
    // default-src * or without frame-ancestors, X-Frame-Options SAMEORIGIN
    // instead of DENY, a Referrer-Policy of unsafe-url, a Permissions-Policy
    // that grants camera=(self), an HSTS max-age of 0 or one without
    // includeSubDomains. Each is a policy a browser accepts happily, so nothing
    // else in the suite or in a rendered page would report it.
    const blocks = await headerBlocks();
    const answered = answeredHeaders(onlyBlock(blocks));

    expect(answered.size).toBe(PLANNED_HEADERS.length);
    for (const { key, value } of PLANNED_HEADERS) {
      expect(answered.get(key)).toBe(value);
    }
  });

  it("never widens frame-ancestors or object-src", async () => {
    // Kills a later edit that rewrites the CSP string and drops the two clauses
    // that carry this app's clickjacking and plugin-embedding defence. The
    // exact case above fails on any edit; this one names WHICH clauses may
    // never be lost, so the fix for that red cannot be to paste the new string
    // into the constant.
    const blocks = await headerBlocks();
    const answered = answeredHeaders(onlyBlock(blocks));
    const policy = answered.get("Content-Security-Policy") ?? "";

    expect(policy).toContain("frame-ancestors 'none'");
    expect(policy).toContain("object-src 'none'");
  });

  it("keeps the powered-by header off and strict mode on", async () => {
    // Kills a diff that adds headers() by rewriting the config object and drops
    // the two flags that were already there: poweredByHeader true names the
    // framework version to anyone scanning, and reactStrictMode false silently
    // stops surfacing the double-render bugs the app is developed against.
    expect(nextConfig.poweredByHeader).toBe(false);
    expect(nextConfig.reactStrictMode).toBe(true);
  });
});
