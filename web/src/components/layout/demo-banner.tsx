/**
 * Every page says the data is invented, so a reader never wonders whether a
 * name is someone's. Exported so tests check the constant, not a literal.
 */
export const DEMO_BANNER_TEXT =
  "Demo data, invented company. Nothing on this site describes a real organisation or person.";

export function DemoBanner() {
  return (
    <div
      role="note"
      className="bg-muted/60 px-4 py-1 text-center text-[11px] leading-5 text-muted-foreground"
    >
      {DEMO_BANNER_TEXT}
    </div>
  );
}
