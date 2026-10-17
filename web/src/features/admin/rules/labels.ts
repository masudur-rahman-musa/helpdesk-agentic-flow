/**
 * The two sentences `/admin/rules` renders that are not data (an earlier change
 * decision E).
 *
 * Constants rather than literals buried in JSX, the way
 * `features/admin/killswitch/format.ts` holds `KILL_SWITCH_READ_MESSAGE`: a string a test
 * has to find by rendering it is a string nobody can reword without reading the
 * component. No test file of its own — this module holds no function and no
 * boundary, so a separate file would assert that a string equals itself — and
 * both values are pinned verbatim where they are rendered, in
 * `tests/rules-board.test.tsx`.
 */

/**
 * The sentence for a read that did not come back as a board.
 *
 * Its own string rather than `FAILURE_MESSAGES.default`, which says only that
 * something went wrong and nothing about WHICH thing could not be read — on a
 * page whose alternative reading is "this tenant has no separation of duties
 * control at all". Pinned verbatim by `tests/rules-board.test.tsx::$name
 * renders the read message, no table and no empty state`.
 */
export const RULES_READ_MESSAGE =
  "The separation of duties rules could not be read. Try again in a moment.";

/**
 * What a side the tool could not resolve shows beside its id (decision F).
 *
 * Nullability is 24ea decision B's marker that a role lookup answered nothing,
 * and this board is the only surface it can be seen from: a blank cell is
 * indistinguishable from a cell that failed to draw, and `Unknown` alone leaves
 * an admin no handle to go looking with. Pinned verbatim by
 * `tests/rules-board.test.tsx::renders an unresolved role_a as its id and the
 * marker, with role_b untouched`.
 */
export const UNRESOLVED_ROLE_TEXT = "Role not found";
