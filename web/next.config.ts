import type { NextConfig } from "next";

/**
 * The Content Security Policy every route answers with.
 *
 * Static rather than a per-request nonce: a nonce would defeat
 * Next's static optimization for every page and put a code branch on the
 * request path. `'unsafe-inline'` on script-src is the admitted cost of that;
 * the clauses that actually carry
 * this app's defence are `frame-ancestors 'none'` (clickjacking),
 * `object-src 'none'` (plugin embedding), `base-uri 'self'` (base-tag
 * injection) and `form-action 'self'` (form exfiltration).
 */
// `next dev` only: Fast Refresh evaluates updated modules with `eval`, which
// the browser refuses without `'unsafe-eval'`. A build never runs as
// development, so the image sees the production string.
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
  // `.next/standalone` tree, which is what `web/Dockerfile` copies. Without it the image carries the whole dependency tree for a
  // server that needs a fraction of it.
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
