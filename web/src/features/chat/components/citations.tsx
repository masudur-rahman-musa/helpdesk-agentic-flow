"use client";

import { useState } from "react";

import type { Citation } from "@/features/chat/components/types";

/**
 * The answer's sources. Text is hidden until asked for, since it can be long.
 * It renders as plain text: retrieved content is data, never markup.
 */
export function Citations({ citations }: { citations: Citation[] }) {
  if (citations.length === 0) {
    return null;
  }

  return (
    <section className="rounded-md bg-muted p-4">
      <h2 className="text-sm font-semibold">Sources</h2>
      <ul className="mt-2 flex flex-col gap-2">
        {citations.map((citation) => (
          <CitationRow key={citation.chunkId} citation={citation} />
        ))}
      </ul>
    </section>
  );
}

function CitationRow({ citation }: { citation: Citation }) {
  const [expanded, setExpanded] = useState(false);

  return (
    <li className="flex flex-col gap-1 text-sm">
      <button
        type="button"
        aria-expanded={expanded}
        onClick={() => setExpanded((open) => !open)}
        className="self-start text-left font-medium underline focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        {citation.title}
      </button>
      <span className="flex flex-wrap gap-2 text-xs text-muted-foreground">
        <span>{citation.version}</span>
        <span>{citation.headingPath.join(" › ")}</span>
        <span>{citation.score}</span>
      </span>
      {expanded ? <p className="whitespace-pre-wrap text-sm">{citation.text}</p> : null}
    </li>
  );
}
