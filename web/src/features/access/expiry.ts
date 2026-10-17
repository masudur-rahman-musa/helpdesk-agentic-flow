/**
 * The grant countdown. Days round down, so access that ended two hours ago
 * reads -1, never "ends today". The clock is an argument so tests can fix it.
 */

const MILLISECONDS_PER_DAY = 86_400_000;

export function daysUntil(expiresAt: string, now: Date): number | null {
  const expiry = Date.parse(expiresAt);
  if (Number.isNaN(expiry)) {
    return null;
  }
  return Math.floor((expiry - now.getTime()) / MILLISECONDS_PER_DAY);
}
