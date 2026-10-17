/**
 * One GET of one proxy route, shared by every client page. The order of the
 * checks is the point:
 * 1. `redirected` first: the browser already followed the redirect to `/login`
 *    and holds a 200 login page, which would look like a broken read.
 * 2. Status before body: the proxy's 502 `UPSTREAM_UNAVAILABLE` is valid JSON
 *    and must not pass as a payload.
 * 3. A code outside the typed error list counts as a failed read.
 * 4. `safeParse`, never a cast, so a bad payload fails instead of showing blank.
 */

import type { z } from "zod";

import { FAILURE_MESSAGES } from "@/lib/failure-messages";

/**
 * A read the server refused with a typed code. Callers tell it from a failed
 * read by its class.
 */
export class WireRefusedError extends Error {
  readonly code: string;

  constructor(code: string) {
    super("the read was refused");
    this.code = code;
  }
}

/**
 * Whether `code` is a real typed error code. `default` is only the fallback key
 * in `FAILURE_MESSAGES`, never a wire value.
 */
export function isTypedCode(code: unknown): code is string {
  return typeof code === "string" && code !== "default" && code in FAILURE_MESSAGES;
}

/**
 * The parsed payload, or `null` while the browser goes to `/login`, so the page
 * shows nothing while it leaves. Everything else throws, so an empty state and
 * an alert never show together.
 */
export async function readWire<T>(path: string, schema: z.ZodType<T>): Promise<T | null> {
  const response = await fetch(path, { headers: { accept: "application/json" } });

  if (response.redirected) {
    window.location.assign(response.url);
    return null;
  }
  if (!response.ok) {
    const code = await typedCodeOf(response);
    if (code !== null) {
      throw new WireRefusedError(code);
    }
    throw new Error(`the read of ${path} failed (${response.status})`);
  }

  const parsed = schema.safeParse(await readJson(response));
  if (!parsed.success) {
    throw new Error(`the payload of ${path} did not match what this page reads`);
  }
  return parsed.data;
}

/**
 * The typed code of a refused response, or null. Also null for a body that is
 * not JSON.
 */
async function typedCodeOf(response: Response): Promise<string | null> {
  const body = await readJson(response);
  if (typeof body !== "object" || body === null) {
    return null;
  }
  const code = (body as { code?: unknown }).code;
  return isTypedCode(code) ? code : null;
}

/** The body as JSON, or `undefined` when it is not JSON at all. */
async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return undefined;
  }
}
