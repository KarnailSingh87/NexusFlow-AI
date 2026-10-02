import type { NextConfig } from 'next'

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // Required for the slim production image in Dockerfile: emits a standalone
  // server plus a minimal server.js entrypoint.
  output: 'standalone',
  outputFileTracingRoot: process.cwd(),
  poweredByHeader: false,
  productionBrowserSourceMaps: false,
  // Note: there is no `eslint` key — Next 16 dropped the built-in lint step.
  // Run `npm run lint` separately.
  typescript: {
    // Type errors are caught by `npm run typecheck`; keep the build honest too.
    ignoreBuildErrors: false,
  },
}

export default nextConfig
