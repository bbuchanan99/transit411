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
Interactive pieces (CIG pipeline, Ask NTD/CIG) read the NAS API. Set the base URL:
- env var `PUBLIC_API_BASE` (e.g. https://api.transit411.com) — read in code via import.meta.env.PUBLIC_API_BASE.
Published articles will be fetched from `${PUBLIC_API_BASE}/api/posts` at build time; for now the
homepage renders sample content so the site builds and looks right before the API is connected.
