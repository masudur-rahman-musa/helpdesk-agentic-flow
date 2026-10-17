/**
 * The sentence a failed `/admin/killswitch` read shows.
 *
 * The switch instant's renderer moved to `lib/date-format.ts`: the
 * policies board and the kill-switch board both use it.
 *
 * Pinned by `tests/features/admin/killswitch/killswitch-format.test.ts`.
 */

/**
 * The sentence for a read that did not come back as a board (decision E).
 *
 * Its own string rather than `FAILURE_MESSAGES.default`, which says only that
 * something went wrong and nothing about WHICH thing could not be read — on a
 * page whose alternative reading is "the platform is clean". Pinned verbatim by
 * `tests/features/admin/killswitch/killswitch-format.test.ts::KILL_SWITCH_READ_MESSAGE`.
 */
export const KILL_SWITCH_READ_MESSAGE =
  "The kill switches could not be read. Try again in a moment.";
