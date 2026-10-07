import type { NextConfig } from "next";

/**
 * The Content Security Policy every route answers with.
 *
 * Static rather than a per-request nonce (decision B1): a nonce would defeat
 * Next's static optimization for every page and put a code branch on the
 * request path. `'unsafe-inline'` on script-src is the admitted cost of that,
 * recorded in the plan rather than hidden; the clauses that actually carry
 * this app's defence are `frame-ancestors 'none'` (clickjacking),
 * `object-src 'none'` (plugin embedding), `base-uri 'self'` (base-tag
 * injection) and `form-action 'self'` (form exfiltration).
 *
 * Pinned character for character by
 * `tests/security-headers.test.ts`, "answers the six planned security headers
 * with exactly the planned values", and the two clauses above are pinned
 * separately by "never widens frame-ancestors or object-src".
 */
// `next dev` only: Fast Refresh evaluates updated modules with `eval`, which
// the browser refuses without `'unsafe-eval'`. A build never runs as
// development, so the image and the test above see the production string.
const SCRIPT_SRC =
  process.env.NODE_ENV === "development"
    ? "script-src 'self' 'unsafe-inline' 'unsafe-eval'"
    : "script-src 'self' 'unsafe-inline'";

const CONTENT_SECURITY_POLICY = [
  "default-src 'self'",
  SCRIPT_SRC,
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data:",
  "font-src 'self'",
  "connect-src 'self'",
  "frame-ancestors 'none'",
  "object-src 'none'",
  "base-uri 'self'",
  "form-action 'self'",
].join("; ");

/** Every route, so no page or proxy route is served without the block above. */
const EVERY_ROUTE = "/(.*)";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // Next traces the files the server actually imports and emits a self-contained
  // `.next/standalone` tree, which is what `web/Dockerfile` copies (an earlier change
  // decision E1). Without it the image carries the whole dependency tree for a
  // server that needs a fraction of it. It changes no header, so 27a's
  // `tests/security-headers.test.ts` reads the same config object; the pairing
  // of this line with the Dockerfile's COPY is pinned by
  // `packages/helpdesk/tests/test_container_build.py::test_the_web_image_is_built_from_the_standalone_output`.
  output: "standalone",
  // The app never needs a powered-by header and it names the framework
  // version to anyone scanning.
  poweredByHeader: false,
  async headers() {
    return [
      {
        source: EVERY_ROUTE,
        headers: [
          { key: "Content-Security-Policy", value: CONTENT_SECURITY_POLICY },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          {
            key: "Permissions-Policy",
            value: "camera=(), microphone=(), geolocation=()",
          },
          // Two years with subdomains, the value a preload list expects. Served
          // over plain http locally, where browsers ignore it entirely.
          {
            key: "Strict-Transport-Security",
            value: "max-age=63072000; includeSubDomains",
          },
        ],
      },
    ];
  },
};

export default nextConfig;
