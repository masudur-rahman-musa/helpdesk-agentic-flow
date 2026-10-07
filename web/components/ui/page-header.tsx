/**
 * The top of every page: the page's one level-one heading, a one-line
 * description, and room on the right for the page's own actions
 * (`tests/ui-primitives.test.tsx::renders the page header title as the
 * level-one heading`).
 */
export function PageHeader({
  title,
  description,
  actions,
}: {
  title: React.ReactNode;
  description?: React.ReactNode;
  actions?: React.ReactNode;
}) {
  return (
    <div className="mb-section flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
      <div className="min-w-0 space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
        {description !== undefined ? (
          <p className="text-sm text-muted-foreground">{description}</p>
        ) : null}
      </div>
      {actions !== undefined ? (
        <div className="flex shrink-0 items-center gap-2">{actions}</div>
      ) : null}
    </div>
  );
}
