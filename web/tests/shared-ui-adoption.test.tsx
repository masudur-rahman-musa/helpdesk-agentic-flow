import { existsSync, readFileSync, readdirSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

/**
 * An earlier change: the shared Alert, TextArea, Input, date format and the moved
 * label and error modules are used everywhere, and nothing copies them by
 * hand. Reads source files only; jsdom, no service.
 */

const WEB_ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const SRC = join(WEB_ROOT, "src");

const NEUTRAL_BOX = "rounded-md border border-border px-3 py-2 text-sm";
const RED_BOX =
  "rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive";

function sourceFiles(): string[] {
  const out: string[] = [];
  const walk = (dir: string): void => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const full = join(dir, entry.name);
      if (entry.isDirectory()) {
        walk(full);
      } else if (/\.tsx?$/.test(entry.name)) {
        out.push(full);
      }
    }
  };
  walk(SRC);
  return out;
}

function read(path: string): string {
  return readFileSync(join(WEB_ROOT, path), "utf8");
}

function importsNameFrom(source: string, name: string, module: string): boolean {
  const escaped = module.replace(/[/@.]/g, "\\$&");
  return new RegExp(
    String.raw`import\s*\{[^}]*\b${name}\b[^}]*\}\s*from\s*["']${escaped}["']`,
  ).test(source);
}

const NEUTRAL_SITES = [
  "src/features/access/components/grant-list.tsx",
  "src/features/admin/audit/components/audit-verify.tsx",
  "src/features/admin/policies/components/policies-board.tsx",
  "src/features/admin/audit/components/audit-search.tsx",
  "src/features/admin/killswitch/components/kill-switch-toggle.tsx",
  "src/features/admin/killswitch/components/kill-switch-board.tsx",
  "src/features/admin/rules/components/rules-board.tsx",
  "src/features/approvals/components/approval-queue.tsx",
  "src/features/requests/components/request-list.tsx",
  "src/features/notifications/components/notification-list.tsx",
  "src/features/reviews/components/review-queue.tsx",
];
const RED_SITES = ["src/features/chat/components/chat-panel.tsx"];

describe("shared Alert", () => {
  it("test_no_feature_hand_copies_the_boxed_alert", () => {
    // Catches a site left with its own boxed banner classes (neutral or red)
    // instead of <Alert>, so the banner look can drift again.
    const offenders = sourceFiles()
      .filter((p) => !p.endsWith(join("components", "ui", "alert.tsx")))
      .filter((p) => {
        const text = readFileSync(p, "utf8");
        return text.includes(NEUTRAL_BOX) || text.includes(RED_BOX);
      })
      .map((p) => relative(WEB_ROOT, p));
    expect(offenders).toEqual([]);
  });

  it.each([...NEUTRAL_SITES, ...RED_SITES])("test_alert_sites_import_shared_alert %s", (path) => {
    // Catches a site that deleted its copy but never used the shared Alert
    // (banner gone), or imported it from somewhere else.
    expect(importsNameFrom(read(path), "Alert", "@/components/ui/alert")).toBe(true);
  });

  it.each(RED_SITES)("the red site %s asks for the destructive tone", (path) => {
    // Catches the chat error banner turning neutral: it must say
    // tone="destructive".
    expect(read(path)).toMatch(/<Alert[^>]*tone=\{?["']destructive["']/);
  });
});

describe("shared TextArea and Input", () => {
  it.each([
    "src/features/admin/killswitch/components/kill-switch-toggle.tsx",
    "src/features/approvals/components/approval-queue.tsx",
  ])("test_textarea_sites_import_shared_textarea %s", (path) => {
    // Catches a site still writing its own textarea or importing TextArea
    // from the wrong place.
    expect(importsNameFrom(read(path), "TextArea", "@/components/ui/field")).toBe(true);
  });

  it("test_composer_imports_shared_input", () => {
    // Catches the chat composer keeping its own hand-written input element.
    expect(
      importsNameFrom(
        read("src/features/chat/components/composer.tsx"),
        "Input",
        "@/components/ui/field",
      ),
    ).toBe(true);
  });

  it("test_no_raw_textarea_outside_field", () => {
    // Catches a hand-copied textarea left in a site.
    const offenders = sourceFiles()
      .filter((p) => !p.endsWith(join("components", "ui", "field.tsx")))
      .filter((p) => readFileSync(p, "utf8").includes("<textarea"))
      .map((p) => relative(WEB_ROOT, p));
    expect(offenders).toEqual([]);
  });
});

describe("moved modules", () => {
  it("test_chat_no_longer_holds_shared_modules", () => {
    // Catches a move that copies instead of moving: the old files must be
    // gone and nothing may import them by alias or by relative path.
    expect(existsSync(join(SRC, "features/chat/errors.ts"))).toBe(false);
    expect(existsSync(join(SRC, "features/chat/decision-labels.ts"))).toBe(false);
    expect(existsSync(join(SRC, "lib/failure-messages.ts"))).toBe(true);
    expect(existsSync(join(SRC, "lib/decision-labels.ts"))).toBe(true);
    const aliasImport = /@\/features\/chat\/(errors|decision-labels)["']/;
    const relativeImport = /["']\.\.?\/(errors|decision-labels)["']/;
    const offenders = sourceFiles()
      .filter((p) => {
        const text = readFileSync(p, "utf8");
        if (aliasImport.test(text)) {
          return true;
        }
        return p.includes(join("features", "chat")) && relativeImport.test(text);
      })
      .map((p) => relative(WEB_ROOT, p));
    expect(offenders).toEqual([]);
  });

  it("test_switch_instant_imported_from_lib", () => {
    // Catches a user of formatSwitchInstant still reading it from the
    // kill-switch format module.
    const users = sourceFiles().filter((p) => {
      const text = readFileSync(p, "utf8");
      return /\bformatSwitchInstant\b/.test(text) && !p.endsWith(join("lib", "date-format.ts"));
    });
    expect(users.length).toBeGreaterThan(0);
    for (const p of users) {
      const text = readFileSync(p, "utf8");
      expect(importsNameFrom(text, "formatSwitchInstant", "@/lib/date-format"), p).toBe(true);
      expect(text).not.toMatch(/import[^;]*formatSwitchInstant[^;]*killswitch\/format/);
    }
  });
});
