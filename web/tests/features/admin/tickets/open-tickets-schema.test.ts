import { describe, expect, it } from "vitest";

import { openTicketsSchema, solveTicketResultSchema } from "@/features/admin/tickets/schema";

/**
 * `open-tickets-schema.test.ts`: the zod shape of `/api/admin/tickets`. No DOM,
 * no fetch.
 *
 * Shapes this file fixes (the plan left them open): the export is
 * `openTicketsSchema` in `web/src/features/admin/tickets/schema.ts`, wrapping
 * `{ tickets: [...] }`, each row holding exactly the tool's six fields:
 * `ticket_id`, `ticket_number`, `kind`, `title`, `opened_by`, `created_at`.
 * There is no `status` field: every row is open.
 */

const ROW = {
  ticket_id: "88888888-8888-5888-8888-888888888888",
  ticket_number: "TCK-0042",
  kind: "general",
  title: "Obviously fake ticket title.",
  opened_by: "Fake Person One",
  created_at: "2026-06-02T10:15:00Z",
};

describe("openTicketsSchema", () => {
  it("accepts a full row", () => {
    // Kill: catches a schema that refuses the shape the server really sends
    // (which would turn a good board into an error alert), and keeps the
    // refusal cases below honest: each differs from this row by one field.
    expect(openTicketsSchema.safeParse({ tickets: [ROW] }).success).toBe(true);
  });

  it("refuses a row without opened_by", () => {
    // Kill: catches an optional `opened_by`, which lets a dropped column draw
    // a blank opener instead of failing the parse. The name is the only field
    // that differs from the accepted row above.
    const withoutOpener: Record<string, unknown> = { ...ROW };
    delete withoutOpener.opened_by;
    expect(openTicketsSchema.safeParse({ tickets: [withoutOpener] }).success).toBe(false);
  });

  it.each(["ticket_id", "ticket_number", "kind", "title", "created_at"])(
    "refuses a row without %s",
    (field) => {
      // Kill: catches any other field made optional, so a dropped column
      // would render blank instead of failing the parse.
      const without: Record<string, unknown> = { ...ROW };
      delete without[field];
      expect(openTicketsSchema.safeParse({ tickets: [without] }).success).toBe(false);
    },
  );
});

const SOLVED = {
  ticket_id: "88888888-8888-5888-8888-888888888888",
  ticket_number: "TCK-0042",
  status: "solved",
  solved_at: "2026-06-04T09:30:00Z",
  solved_by_user_id: "22222222-2222-5222-8222-222222222222",
};

describe("solveTicketResultSchema", () => {
  it("accepts a full solve answer", () => {
    // Kill: catches a schema that refuses the shape the server really sends
    // (every good solve would read as a failure), and keeps the refusal case
    // below honest: it differs from this answer by one field.
    expect(solveTicketResultSchema.safeParse(SOLVED).success).toBe(true);
  });

  it("refuses a solve answer without solved_at", () => {
    // Kill: catches an optional `solved_at`, which lets an answer with no time
    // pass as a solve. It is the only field that differs from the accepted
    // answer above.
    const without: Record<string, unknown> = { ...SOLVED };
    delete without.solved_at;
    expect(solveTicketResultSchema.safeParse(without).success).toBe(false);
  });
});
