/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // This app lives deep inside a larger directory tree that may hold other
  // package.json / lockfiles. Turbopack infers its root from the nearest
  // lockfile it finds walking *up*, and a wrong guess makes it resolve
  // `tailwindcss` and postcss.config.js from outside this app -- the dev
  // server then dies compiling app/globals.css. Pin both roots here (Next
  // requires them to match).
  turbopack: {
    root: __dirname,
  },
  outputFileTracingRoot: __dirname,
};

module.exports = nextConfig;
