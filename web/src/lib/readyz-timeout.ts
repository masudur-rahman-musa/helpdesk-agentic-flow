/**
 * The readiness check's timeout. It sits apart because a Next route module may
 * not export extra names, and a test needs to import it.
 */

/**
 * Two seconds. A probe with no timeout hangs past the kubelet's deadline
 * instead of answering "not ready", so fail fast.
 */
export const READYZ_TIMEOUT_MS = 2_000;
