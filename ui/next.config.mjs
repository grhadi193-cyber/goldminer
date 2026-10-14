/**
 * The Mission Control UI is exported as STATIC HTML and served by the worker itself (one Render
 * web service, one origin, no CORS and no server-side data reads):
 *
 *   npm run build            -> ui/out/   (the worker serves this directory at `/`)
 *
 * In development `next dev` serves the same files and proxies `/api/*` to the locally running
 * worker, so the panel behaves identically in both environments.
 */
const workerUrl = (process.env.MISHKAH_WORKER_URL ?? "http://127.0.0.1:8787").replace(/\/$/, "");
const isDev = process.env.NODE_ENV === "development";

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  output: "export",
  images: { unoptimized: true },
  trailingSlash: true,
  ...(isDev
    ? {
        // Dev-only. A static export has no server, and on Render the worker IS the origin, so the
        // browser's relative `/api/...` calls land on the worker without any proxy in between.
        async rewrites() {
          return [{ source: "/api/:path*", destination: `${workerUrl}/api/:path*` }];
        },
        async headers() {
          return [
            {
              source: "/data/:path*",
              headers: [{ key: "Cache-Control", value: "no-store, max-age=0" }],
            },
          ];
        },
      }
    : {}),
};
export default nextConfig;
