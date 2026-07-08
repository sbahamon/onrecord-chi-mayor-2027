import { defineConfig } from "astro/config";

// For GitHub Pages project sites the app is served under /<repo>/.
// Set SITE_URL and BASE_PATH in the deploy workflow; both default to root
// so `npm run dev` and local builds Just Work.
export default defineConfig({
  site: process.env.SITE_URL || "http://localhost:4321",
  base: process.env.BASE_PATH || "/",
  trailingSlash: "ignore",
});
