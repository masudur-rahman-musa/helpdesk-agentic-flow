import { describe, expect, it } from "vitest";

import {
  EMAIL_MAX_LENGTH,
  SIGN_IN_FAILED_MESSAGE,
  SLUG_MAX_LENGTH,
  SLUG_PATTERN,
  loginSchema,
} from "@/lib/schemas";

/**
 * Boundary table for the login schema.
 * Every case here names the wrong implementation it kills; a case that would
 * also fail with the control under test removed but a neighbour left in place
 * is not written.
 */

const VALID_PASSWORD = "correct-horse-battery-staple";

function slugOf(length: number): string {
  return "a".repeat(length);
}

/** A well-formed address whose TOTAL length is exactly `length`. */
function emailOfLength(length: number): string {
  const domain = "@e.com";
  return "a".repeat(length - domain.length) + domain;
}

function parse(fields: {
  tenant_slug?: string;
  email?: string;
  password?: string;
}): ReturnType<typeof loginSchema.safeParse> {
  return loginSchema.safeParse({
    tenant_slug: fields.tenant_slug ?? "nordlicht",
    email: fields.email ?? "ada@e.com",
    password: fields.password ?? VALID_PASSWORD,
  });
}

describe("login schema — pinned widths", () => {
  it("pins the tenant slug width at exactly 64, the width of tenants.slug", () => {
    // Kills a schema that caps the slug at any other number (60, 100, uncapped):
    // a band would admit the wrong value, this pins it.
    expect(SLUG_MAX_LENGTH).toBe(64);
  });

  it("pins the email width at exactly 320, the width of users.email", () => {
    // Kills a schema that reuses some other cap (255 is the tempting wrong one).
    expect(EMAIL_MAX_LENGTH).toBe(320);
  });
});

describe("login schema — slug length boundary, both sides", () => {
  it("accepts a slug of exactly 64 characters", () => {
    // Kills an exclusive bound (`< 64` / `max(63)`): the inclusive edge is the
    // half a one-off error lands on, and only this side of the table sees it.
    expect(parse({ tenant_slug: slugOf(SLUG_MAX_LENGTH) }).success).toBe(true);
  });

  it("refuses a slug of 65 characters", () => {
    // Kills a missing or off-by-one max: with no cap at all this is the only
    // case that turns red, since 64 still parses.
    expect(parse({ tenant_slug: slugOf(SLUG_MAX_LENGTH + 1) }).success).toBe(
      false,
    );
  });
});

describe("login schema — email length boundary, both sides", () => {
  it("accepts an email of exactly 320 characters", () => {
    // Kills an exclusive bound on the email cap. The address is well formed and
    // only its LENGTH is at the bound, so a shape rule cannot explain a failure.
    const email = emailOfLength(EMAIL_MAX_LENGTH);
    expect(email).toHaveLength(EMAIL_MAX_LENGTH);
    expect(parse({ email }).success).toBe(true);
  });

  it("refuses an email of 321 characters", () => {
    // Kills a missing email cap: same shape as the accepted case, one char more,
    // so nothing but the cap can decide it.
    const email = emailOfLength(EMAIL_MAX_LENGTH + 1);
    expect(email).toHaveLength(EMAIL_MAX_LENGTH + 1);
    expect(parse({ email }).success).toBe(false);
  });
});

describe("login schema — slug pattern anchoring", () => {
  it("anchors SLUG_PATTERN at both ends", () => {
    // Kills an unanchored regex directly at the source, so a later edit that
    // drops ^ or $ is red here as well as in the rejection table below.
    expect(SLUG_PATTERN.source.startsWith("^")).toBe(true);
    expect(SLUG_PATTERN.source.endsWith("$")).toBe(true);
  });

  const rejected: ReadonlyArray<{ label: string; slug: string }> = [
    { label: "a trailing newline", slug: "nordlicht\n" },
    { label: "a trailing CRLF", slug: "nordlicht\r\n" },
    { label: "a trailing space", slug: "nordlicht " },
    { label: "an uppercase letter", slug: "Nordlicht" },
    { label: "an underscore", slug: "nord_licht" },
    { label: "an empty string", slug: "" },
  ];

  it.each(rejected)("refuses a slug with $label", ({ slug }) => {
    // Kills an unanchored pattern (a project rule's three rows): every one of
    // the whitespace rows PASSES a substring match and fails a full match, and
    // the slug is otherwise valid and well under 64, so length cannot decide it.
    expect(parse({ tenant_slug: slug }).success).toBe(false);
  });

  const accepted: ReadonlyArray<{ label: string; slug: string }> = [
    { label: "lowercase letters only", slug: "nordlicht" },
    { label: "hyphens and digits", slug: "nord-licht-2" },
  ];

  it.each(accepted)("accepts a slug of $label", ({ slug }) => {
    // Kills a pattern tightened past the spec (e.g. letters only, or no
    // hyphen), which the rejection table alone would not notice.
    expect(parse({ tenant_slug: slug }).success).toBe(true);
  });
});

describe("login schema — required fields", () => {
  it("refuses a body with no password", () => {
    // Kills a schema that made password optional, letting an empty credential
    // reach agent-api; the slug and email rows are all valid here.
    const result = loginSchema.safeParse({
      tenant_slug: "nordlicht",
      email: "ada@e.com",
    });
    expect(result.success).toBe(false);
  });
});

describe("the generic failure message", () => {
  it("exports one non-empty generic message with no field-level hint", () => {
    // Kills a message that names which of the three inputs was wrong, which is
    // exactly what an earlier change §1's 401 criterion forbids.
    expect(SIGN_IN_FAILED_MESSAGE.length).toBeGreaterThan(0);
    expect(SIGN_IN_FAILED_MESSAGE.toLowerCase()).not.toContain("password");
    expect(SIGN_IN_FAILED_MESSAGE.toLowerCase()).not.toContain("email");
    expect(SIGN_IN_FAILED_MESSAGE.toLowerCase()).not.toContain("tenant");
  });
});
