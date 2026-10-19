/**
 * Tidies the Sources box under a reply. Passages from the same document,
 * version and section merge into one line that keeps the first passage's
 * place and key, with their texts joined by a blank line. A line whose best
 * score is below half the box's best is dropped: a
 * passage both search arms found scores about 0.033, one only a single arm
 * found at most about 0.016. When every score is 0 nothing is dropped, so the
 * box never goes empty for missing scores.
 */

import type { Citation } from "@/features/chat/components/types";

function lineKey(citation: Citation): string {
  return JSON.stringify([citation.document, citation.version, citation.headingPath]);
}

export function tidySources(citations: Citation[]): Citation[] {
  const lines = new Map<string, Citation>();
  for (const citation of citations) {
    const key = lineKey(citation);
    const line = lines.get(key);
    if (line === undefined) {
      lines.set(key, { ...citation });
    } else {
      line.text = `${line.text}\n\n${citation.text}`;
      line.score = Math.max(line.score, citation.score);
    }
  }

  const merged = [...lines.values()];
  const best = Math.max(0, ...merged.map((line) => line.score));
  if (best === 0) {
    return merged;
  }
  return merged.filter((line) => line.score >= best / 2);
}
