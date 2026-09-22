// Central content source for the public site.
// Pages are static: posts are fetched ONCE at build time (Cloudflare Pages build) from the
// read-only API. If the API is unreachable or slow, the build still succeeds and pages show an
// empty "no items yet" state. New posts appear on the site after the next build/deploy.

const DEFAULT_API_BASE = "https://api.transit411.net";
export const API_BASE = (import.meta.env.PUBLIC_API_BASE || DEFAULT_API_BASE).replace(/\/+$/, "");
const TIMEOUT_MS = 8000;

export const SECTIONS = {
  news:        { label: "News",              pillar: null,          blurb: "Everything moving across transit funding, procurement, people, and policy." },
  funding:     { label: "Funding & Finance", pillar: "Funding",     blurb: "FTA grants, ballot measures, appropriations, and the money behind transit capital projects." },
  procurement: { label: "Procurement",       pillar: "Procurement", blurb: "RFPs, RFQs, and contract awards for program management, design-build, and delivery." },
  people:      { label: "People",            pillar: "People",      blurb: "Leadership moves and appointments across agencies and firms." },
  policy:      { label: "Policy",            pillar: "Policy",      blurb: "Reauthorization, federal rules, and the policy shaping transit investment." },
};

async function getJson(path) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), TIMEOUT_MS);
  try {
    const r = await fetch(API_BASE + path, { signal: ctl.signal, headers: { accept: "application/json" } });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return await r.json();
  } catch (e) {
    console.warn(`[content] ${API_BASE}${path} unavailable (${e.name === "AbortError" ? "timeout" : e.message}); rendering without it.`);
    return null;
  } finally {
    clearTimeout(timer);
  }
}

// Only http(s) links from the API become hrefs.
function safeUrl(u) {
  try {
    const x = new URL(String(u));
    return x.protocol === "http:" || x.protocol === "https:" ? x.href : null;
  } catch {
    return null;
  }
}

function formatDate(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  return isNaN(d) ? null : d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "America/New_York" });
}

// API post -> what the pages render.
function toPost(p) {
  const summary = p.summary ?? (p.body || "").split("\n\nSource:")[0].trim();
  return {
    id: p.id,
    slug: p.slug,
    pillar: p.pillar || "News",
    title: p.title,
    deck: summary || null,
    date: formatDate(p.publish_at),
    publishedAt: p.publish_at || null,
    source: p.source_name || null,
    url: safeUrl(p.source_url),
    agencies: Array.isArray(p.agencies) ? p.agencies : [],
    tags: Array.isArray(p.tags) ? p.tags : [],
    state: p.state || null,
    featured: !!p.featured,
    sponsor: p.sponsor || null,
  };
}

// One request per build, shared by every page that asks.
let postsPromise;
function allPosts() {
  postsPromise ??= getJson("/api/posts").then((d) =>
    (d && Array.isArray(d.posts) ? d.posts : []).filter((p) => p && p.title).map(toPost));
  return postsPromise;
}

// Published posts, newest first (featured placements first), optionally for one pillar.
export async function getPosts(pillar) {
  const posts = await allPosts();
  return pillar ? posts.filter((p) => p.pillar === pillar) : posts;
}

// Latest CIG pipeline summary for the homepage stats; null if unavailable.
let cigPromise;
export function getCigSummary() {
  cigPromise ??= getJson("/api/cig").then((d) => (d && d.summary && d.summary.projects ? d.summary : null));
  return cigPromise;
}
