import { defineConfig } from 'astro/config';
import sitemap from '@astrojs/sitemap';

// Public site for Transit411. Deploys to Cloudflare Pages (output: static).
//
// `site` is the production address and nothing else. It is what canonical URLs, Open Graph tags
// and the sitemap are built from, so pointing it at the Pages subdomain - as it was while the
// root domain was dark - would have told Google the real site lives at transit411.pages.dev.
export default defineConfig({
  site: 'https://transit411.net',
  integrations: [
    sitemap({
      // The 404 is the only page that should never appear in search results.
      filter: (page) => !page.includes('/404'),
    }),
  ],
});
