/**
 * Every page says the data is invented. Seed data is obviously fake by rule,
 * and a reader landing on a grant or an approval should not have to guess
 * whether a name is someone's.
 *
 * The text is exported so tests assert the constant rather than a literal:
 * rewording it stays green, dropping it goes red (`tests/shell.test.tsx`,
 * "renders the demo banner text on the shell"). Styled quietly as of an earlier change:
 * a thin muted strip, still `role="note"` and still on every page.
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
