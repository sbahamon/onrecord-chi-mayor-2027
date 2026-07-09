import { defineConfig } from "astro/config";

// For GitHub Pages project sites the app is served under /<repo>/.
// Set SITE_URL and BASE_PATH in the deploy workflow; both default to root
// so `npm run dev` and local builds Just Work.
// GitHub's configure-pages provides base_path WITHOUT a trailing slash
// (e.g. "/onrecord-chi-mayor-2027"); normalize so templates can safely do
// `import.meta.env.BASE_URL + "feed"` and get one slash, not zero.
let base = process.env.BASE_PATH || "/";
if (!base.endsWith("/")) base += "/";

export default defineConfig({
  site: process.env.SITE_URL || "http://localhost:4321",
  base,
  trailingSlash: "ignore",
});
