import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // The app never needs a powered-by header and it names the framework
  // version to anyone scanning.
  poweredByHeader: false,
};

export default nextConfig;
