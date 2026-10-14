import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * The design tokens in `src/app/globals.css`: the same
 * colour names in both themes, every text pair readable, the focus ring
 * visible, and the layout tokens the shell and the chat column read.
 *
 * The file is parsed here rather than rendered: jsdom computes no cascade from
 * Tailwind, so the only honest source of a token's value is the CSS itself.
 * Colour values are `H S% L%` (the shadcn idiom), converted to sRGB and scored
 * with the WCAG 2.x relative-luminance formula below.
 */

const CSS = readFileSync(resolve(__dirname, "../src/app/globals.css"), "utf8");

const COLOUR_TOKENS = [
  "--background",
  "--foreground",
  "--card",
  "--card-foreground",
  "--muted",
  "--muted-foreground",
  "--border",
  "--ring",
  "--primary",
  "--primary-foreground",
  "--secondary",
  "--secondary-foreground",
  "--accent",
  "--accent-foreground",
  "--destructive",
  "--destructive-foreground",
  "--success",
  "--success-foreground",
  "--warning",
  "--warning-foreground",
  "--info",
  "--info-foreground",
  "--danger",
  "--danger-foreground",
] as const;

const TEXT_PAIRS = [
  ["background", "foreground"],
  ["background", "muted-foreground"],
  ["muted", "muted-foreground"],
  ["card", "card-foreground"],
  ["card", "muted-foreground"],
  ["primary", "primary-foreground"],
  ["secondary", "secondary-foreground"],
  ["destructive", "destructive-foreground"],
  ["accent", "accent-foreground"],
  ["success", "success-foreground"],
  ["warning", "warning-foreground"],
  ["info", "info-foreground"],
  ["danger", "danger-foreground"],
] as const;

const THEMES = ["light", "dark"] as const;
type ThemeName = (typeof THEMES)[number];

const HSL = /^(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)%\s+(\d+(?:\.\d+)?)%$/;

function block(selector: RegExp, what: string): string {
  const match = selector.exec(CSS);
  if (match === null || match[1] === undefined) {
    throw new Error(`globals.css has no ${what} block`);
  }
  return match[1];
}

function declarations(body: string): Map<string, string> {
  const out = new Map<string, string>();
  for (const m of body.matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) {
    if (m[1] !== undefined && m[2] !== undefined) {
      out.set(m[1], m[2].trim());
    }
  }
  return out;
}

const BLOCKS: Record<ThemeName, Map<string, string>> = {
  light: declarations(block(/:root\s*\{([^}]*)\}/, ":root")),
  dark: declarations(block(/(?:^|\s)\.dark\s*\{([^}]*)\}/m, ".dark")),
};

/** Names whose value is an `H S% L%` triple: the colour tokens as written. */
function colourNames(theme: ThemeName): Set<string> {
  const names = new Set<string>();
  for (const [name, value] of BLOCKS[theme]) {
    if (HSL.test(value)) {
      names.add(name);
    }
  }
  return names;
}

function rgb(theme: ThemeName, token: string): [number, number, number] {
  const value = BLOCKS[theme].get(`--${token}`);
  if (value === undefined) {
    throw new Error(`--${token} is not declared in the ${theme} block`);
  }
  const m = HSL.exec(value);
  if (m === null) {
    throw new Error(`--${token} in ${theme} is "${value}", not "H S% L%"`);
  }
  const h = Number(m[1]) / 360;
  const s = Number(m[2]) / 100;
  const l = Number(m[3]) / 100;
  const q = l < 0.5 ? l * (1 + s) : l + s - l * s;
  const p = 2 * l - q;
  const channel = (t0: number): number => {
    let t = t0;
    if (t < 0) t += 1;
    if (t > 1) t -= 1;
    if (t < 1 / 6) return p + (q - p) * 6 * t;
    if (t < 1 / 2) return q;
    if (t < 2 / 3) return p + (q - p) * (2 / 3 - t) * 6;
    return p;
  };
  return s === 0 ? [l, l, l] : [channel(h + 1 / 3), channel(h), channel(h - 1 / 3)];
}

function luminance([r, g, b]: [number, number, number]): number {
  const lin = (c: number): number => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
}

function contrast(theme: ThemeName, a: string, b: string): number {
  const la = luminance(rgb(theme, a));
  const lb = luminance(rgb(theme, b));
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
}

describe("design tokens — colour names", () => {
  it("declares the same colour tokens in light and dark", () => {
    // Catches a theme that defines a token only in one block (a `--success`
    // added to `:root` and forgotten in `.dark` silently inherits the light
    // value in dark mode), and a colour token written as hex or rgb, which
    // Tailwind's `hsl(var(--name))` mapping cannot read.
    expect(colourNames("light")).toEqual(new Set(COLOUR_TOKENS));
    expect(colourNames("dark")).toEqual(new Set(COLOUR_TOKENS));
  });
});

describe("design tokens — text contrast", () => {
  const CASES = THEMES.flatMap((theme) =>
    TEXT_PAIRS.map(([bg, fg]) => [`${bg} / ${fg}`, theme, bg, fg] as const),
  );

  it.each(CASES)("meets 4.5:1 for %s in %s", (_pair, theme, bg, fg) => {
    // Catches a token pair too faint to read in one theme only, such as a
    // pale amber `warning-foreground` on a pale amber `warning`, or a dark
    // theme that copied the light `muted-foreground` onto a dark `muted`.
    expect(contrast(theme, bg, fg)).toBeGreaterThanOrEqual(4.5);
  });

  it.each(THEMES)("meets 3:1 for the focus ring against the background in %s", (theme) => {
    // Catches a focus ring too close to the page colour to see (WCAG 1.4.11),
    // for example a light-grey `--ring` on a white background, which leaves
    // keyboard users with no visible focus.
    expect(contrast(theme, "ring", "background")).toBeGreaterThanOrEqual(3);
  });
});

describe("design tokens — layout", () => {
  it("declares the layout tokens in :root", () => {
    // Catches a Tailwind mapping to `var(--space-gutter)` or a chat column
    // reading `var(--shell-offset)` whose variable was never declared, which
    // resolves to nothing and collapses the padding or the min-height.
    for (const name of [
      "--radius",
      "--font-sans",
      "--space-gutter",
      "--space-section",
      "--shell-offset",
    ]) {
      expect(BLOCKS.light.has(name), `${name} in :root`).toBe(true);
    }
  });
});
