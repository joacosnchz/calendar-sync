import type { NextConfig } from "next";

// The browser only ever talks to this origin. /api and /auth are proxied to the Supabase
// Edge Functions of the same names, so session cookies are first-party and OAuth redirect
// URIs live on the web URL. Locally: `supabase start` serves them on :54321. Hosted:
// https://<project-ref>.supabase.co/functions/v1 (set at build time).
const functionsUrl = (process.env.SUPABASE_FUNCTIONS_URL ?? "http://localhost:54321/functions/v1").replace(/\/+$/, "");

const nextConfig: NextConfig = {
  output: "standalone",
  poweredByHeader: false,
  async rewrites() {
    return [
      { source: "/api/:path*", destination: `${functionsUrl}/api/:path*` },
      { source: "/auth/:path*", destination: `${functionsUrl}/auth/:path*` },
    ];
  },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Frame-Options", value: "DENY" },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
        ],
      },
    ];
  },
};

export default nextConfig;
