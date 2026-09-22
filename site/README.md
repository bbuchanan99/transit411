# Transit411 public site (Astro)

The public read layer. Deploys to Cloudflare Pages.

## Cloudflare Pages build settings
- Root directory: `site`
- Build command: `npm install && npm run build`
- Build output directory: `dist`

## Local dev
```
cd site && npm install && npm run dev
```

## Live data
Pages read the NAS's read-only public API **at build time** (`src/lib/content.js`):
- `PUBLIC_API_BASE` — defaults to `https://api.transit411.net`; override in a local `.env` (see
  `.env.example`) or in Cloudflare Pages' environment variables.
- Published posts come from `${PUBLIC_API_BASE}/api/posts`; the homepage's CIG figures from `/api/cig`.
- If the API is down or slow (8 s timeout), the build still succeeds and pages show a
  "no stories yet" state, so a NAS outage never breaks a deploy.
- Because the output is static, **newly published posts appear after the next Pages build**
  (a push, a manual redeploy, or a Pages deploy hook).
