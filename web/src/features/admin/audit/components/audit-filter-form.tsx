"use client";

import type { Dispatch, SetStateAction } from "react";

import type { AppliedFilters } from "@/features/admin/audit/api";
import {
  AUDIT_FILTER_MAX_LENGTHS,
  fromInstant,
  PAGE_SIZES,
  toInstant,
} from "@/features/admin/audit/filters";
import { Button } from "@/components/ui/button";
import { Input, Label } from "@/components/ui/field";

/** The four text controls, each with the wire parameter it fills and its cap. */
export const TEXT_FILTERS = [
  { label: "Event type", param: "event_type" },
  { label: "Subject type", param: "subject_type" },
  { label: "Subject id", param: "subject_id" },
  { label: "Actor", param: "actor_ref" },
] as const;

/** What the controls hold right now, keyed the same way plus the two dates. */
export type Draft = Record<string, string>;

export const EMPTY_DRAFT: Draft = {
  event_type: "",
  subject_type: "",
  subject_id: "",
  actor_ref: "",
  from: "",
  to: "",
};

/**
 * The filter fields, the page size and `Search`. It holds no state of its own:
 * the draft and the page size live in `AuditSearch`, which owns what a commit
 * and a page-size change clear.
 */
export function AuditFilterForm({
  draft,
  setDraft,
  pageSize,
  onPageSize,
  onCommit,
}: {
  draft: Draft;
  setDraft: Dispatch<SetStateAction<Draft>>;
  pageSize: number;
  onPageSize: (value: number) => void;
  onCommit: () => void;
}) {
  return (
    <form
      className="flex flex-wrap items-end gap-3"
      onSubmit={(event) => {
        event.preventDefault();
        onCommit();
      }}
    >
      {TEXT_FILTERS.map((filter) => (
        <div key={filter.param} className="flex w-56 flex-col gap-1">
          <Label htmlFor={`audit-${filter.param}`}>{filter.label}</Label>
          <Input
            id={`audit-${filter.param}`}
            type="text"
            value={draft[filter.param] ?? ""}
            maxLength={AUDIT_FILTER_MAX_LENGTHS[filter.param]}
            onChange={(event) =>
              setDraft((current) => ({ ...current, [filter.param]: event.target.value }))
            }
          />
        </div>
      ))}

      {(
        [
          { key: "from", label: "From" },
          { key: "to", label: "To" },
        ] as const
      ).map((end) => (
        <div key={end.key} className="flex w-44 flex-col gap-1">
          <Label htmlFor={`audit-${end.key}`}>{end.label}</Label>
          <Input
            id={`audit-${end.key}`}
            type="date"
            value={draft[end.key] ?? ""}
            onChange={(event) =>
              setDraft((current) => ({ ...current, [end.key]: event.target.value }))
            }
          />
        </div>
      ))}

      <div className="flex w-32 flex-col gap-1">
        <Label htmlFor="audit-limit">Page size</Label>
        <select
          id="audit-limit"
          value={pageSize}
          onChange={(event) => onPageSize(Number(event.target.value))}
          className="rounded-md border border-border bg-background px-3 py-2 text-sm focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {PAGE_SIZES.map((size) => (
            <option key={size} value={size}>
              {size}
            </option>
          ))}
        </select>
      </div>

      <Button type="submit">Search</Button>
    </form>
  );
}

/**
 * The applied filters, as wire parameters, out of what the controls hold.
 *
 * A control left empty contributes NOTHING. The two dates are composed into
 * instants here rather than sent raw, so a `timestamptz` column never meets a
 * value whose meaning depends on the server's zone (decision C).
 */
export function filtersOf(draft: Draft): AppliedFilters {
  const filters: Record<string, string> = {};
  for (const filter of TEXT_FILTERS) {
    const value = draft[filter.param] ?? "";
    if (value !== "") {
      filters[filter.param] = value;
    }
  }
  if ((draft.from ?? "") !== "") {
    filters.occurred_from = fromInstant(draft.from ?? "");
  }
  if ((draft.to ?? "") !== "") {
    filters.occurred_to = toInstant(draft.to ?? "");
  }
  return filters;
}
