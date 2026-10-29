"use client";

import Link from "next/link";

import { usePolicyRead } from "@/features/policies/api";
import type { PolicyListRow, PolicyText } from "@/features/policies/schema";
import { FormattedText } from "@/features/chat/format";
import { failureMessage } from "@/lib/failure-messages";
import { WireRefusedError } from "@/lib/wire-read";
import { Alert } from "@/components/ui/alert";
import { EmptyState } from "@/components/ui/empty-state";
import { PageHeader } from "@/components/ui/page-header";

const READ_FAILED_TEXT = "The access rules could not be loaded. Try again in a moment.";
const NO_DOCUMENTS_TEXT = "There are no access rules to show yet.";

/**
 * The company's access rules, for every signed-in person. The list of live
 * documents on one side, the chosen one in full on the other. `doc` is the
 * slug from `?doc=`; with none, the first document in the list opens. The text
 * goes through `FormattedText`, so lists and bold show and nothing is ever
 * drawn as HTML.
 */
export function PoliciesReader({ doc }: { doc: string | null }) {
  const list = usePolicyRead(null);
  const documents = list.isSuccess && list.data !== null ? list.data.documents : null;
  const chosen = doc ?? documents?.[0]?.slug ?? null;
  const read = usePolicyRead(chosen, chosen !== null);
  const document = read.isSuccess && read.data !== null ? read.data.document : null;

  const failed = list.isError ? list.error : read.isError ? read.error : null;

  return (
    <section aria-label="Access rules" className="flex max-w-5xl flex-col gap-6">
      <PageHeader title="Access rules" />

      {failed !== null ? <Alert>{errorMessage(failed)}</Alert> : null}

      {documents !== null && documents.length === 0 ? (
        <EmptyState title={NO_DOCUMENTS_TEXT} />
      ) : null}

      {documents !== null && documents.length > 0 ? (
        <div className="flex flex-col gap-6 md:flex-row">
          <DocumentList documents={documents} chosen={chosen} />
          {document !== null ? <DocumentText document={document} /> : null}
        </div>
      ) : null}
    </section>
  );
}

function DocumentList({
  documents,
  chosen,
}: {
  documents: PolicyListRow[];
  chosen: string | null;
}) {
  return (
    <nav aria-label="Policy documents" className="md:w-60 md:shrink-0">
      <ul className="flex flex-col gap-1">
        {documents.map((row) => (
          <li key={row.slug}>
            <Link
              href={`/policies?doc=${encodeURIComponent(row.slug)}`}
              aria-current={row.slug === chosen ? "page" : undefined}
              className="block rounded-md px-2 py-1 text-sm hover:bg-muted aria-[current=page]:bg-muted aria-[current=page]:font-medium"
            >
              {row.title}
            </Link>
          </li>
        ))}
      </ul>
    </nav>
  );
}

/**
 * Each section draws the headings of its path that the one before it did not,
 * so a parent heading with no text of its own still shows. The document's own
 * title is already the article heading, so it is not drawn again.
 */
function DocumentText({ document }: { document: PolicyText }) {
  let shown: string[] = [];
  const blocks = document.sections.map((section, index) => {
    const path =
      section.heading_path[0] === document.title
        ? section.heading_path.slice(1)
        : section.heading_path;
    const same = sharedDepth(shown, path);
    shown = path;
    return (
      <div key={index} className="flex flex-col gap-2">
        {path.slice(same).map((heading, offset) => (
          <Heading key={`${same + offset}-${heading}`} depth={same + offset} text={heading} />
        ))}
        <div className="flex flex-col gap-2 text-sm leading-relaxed">
          <FormattedText text={section.text} />
        </div>
      </div>
    );
  });

  return (
    <article aria-label={document.title} className="flex min-w-0 flex-1 flex-col gap-4">
      <header className="flex flex-col gap-1">
        <h2 className="text-xl font-semibold">{document.title}</h2>
        <p className="text-xs text-muted-foreground">Version {document.version}</p>
      </header>
      {blocks}
    </article>
  );
}

function sharedDepth(previous: string[], next: string[]): number {
  let depth = 0;
  while (depth < previous.length && depth < next.length && previous[depth] === next[depth]) {
    depth += 1;
  }
  return depth;
}

/** Depth 0 sits just under the document title. */
function Heading({ depth, text }: { depth: number; text: string }) {
  if (depth === 0) {
    return <h3 className="text-base font-semibold">{text}</h3>;
  }
  if (depth === 1) {
    return <h4 className="text-sm font-semibold">{text}</h4>;
  }
  return <h5 className="text-sm font-medium">{text}</h5>;
}

function errorMessage(error: unknown): string {
  if (error instanceof WireRefusedError) {
    return failureMessage(error.code);
  }
  return READ_FAILED_TEXT;
}
