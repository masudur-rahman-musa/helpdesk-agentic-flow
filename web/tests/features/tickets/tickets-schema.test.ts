import { describe, expect, it } from "vitest";

import { myTicketsSchema } from "@/features/tickets/schema";

/**
 * `tickets-schema.test.ts`: the zod shape of `/api/me/tickets`. No DOM, no
 * fetch.
 *
 * Shapes this file fixes: the export is `myTicketsSchema` (the same naming as
 * `myRequestsSchema`), wrapping `{ tickets: [...] }`, each row holding
 * `ticket_id`, `ticket_number`, `kind`, `title`, `status` and `created_at`.
 */

const ROW = {
  ticket_id: "88888888-8888-5888-8888-888888888888",
  ticket_number: "TCK-0042",
  kind: "general",
  title: "Obviously fake ticket title.",
  status: "open",
  created_at: "2026-06-02T10:15:00Z",
};

describe("myTicketsSchema", () => {
  it.each(["open", "solved"])("accepts the status %s", (status) => {
    // Kill: catches a schema that refuses one of the two words the server
    // sends, which would turn a good list into an error alert.
    expect(myTicketsSchema.safeParse({ tickets: [{ ...ROW, status }] }).success).toBe(true);
  });

  it("refuses a status other than open or solved", () => {
    // Kill: catches `status: z.string()`, which lets a word the page has no
    // label for through, so a row would draw with a blank or raw status.
    // The status is the only field that differs from the accepted row above.
    expect(myTicketsSchema.safeParse({ tickets: [{ ...ROW, status: "closed" }] }).success).toBe(
      false,
    );
  });

  it("refuses a row that lacks a field", () => {
    // Kill: catches an optional field, which would let a dropped column render
    // blank instead of failing the parse.
    const withoutNumber: Record<string, unknown> = { ...ROW };
    delete withoutNumber.ticket_number;
    expect(myTicketsSchema.safeParse({ tickets: [withoutNumber] }).success).toBe(false);
  });
});
