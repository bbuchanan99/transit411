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

// API post -> what the pages render. `body` is Transit411's own paraphrase (never the source's
// article text); the "Source: name - url" line the API appends is stripped and shown as a link.
function toPost(p) {
  const summary = p.summary ?? (p.body || "").split("\n\nSource:")[0].trim();
  return {
    id: p.id,
    slug: p.slug,
    href: p.slug ? `/article/${p.slug}` : null,
    paragraphs: (summary || "").split(/\n{2,}/).map((s) => s.trim()).filter(Boolean),
    mode: Array.isArray(p.mode) ? p.mode : [],
    programs: Array.isArray(p.programs) ? p.programs : [],
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

// One post by slug (its article page), or null.
export async function getPost(slug) {
  return (await allPosts()).find((p) => p.slug === slug) || null;
}

// Other posts worth reading next to this one: same agency first, then same pillar.
export async function getRelated(post, limit = 4) {
  const posts = (await allPosts()).filter((p) => p.slug && p.slug !== post.slug);
  const score = (p) =>
    (p.agencies.some((a) => post.agencies.includes(a)) ? 4 : 0) +
    (p.programs.some((x) => post.programs.includes(x)) ? 2 : 0) +
    (p.pillar === post.pillar ? 1 : 0) + (p.state && p.state === post.state ? 1 : 0);
  return posts.map((p) => [score(p), p]).filter(([s]) => s > 0)
    .sort((a, b) => b[0] - a[0]).slice(0, limit).map(([, p]) => p);
}

// Latest CIG pipeline snapshot ({summary, projects}) from /api/cig; null if unavailable.
let cigPromise;
export function getCig() {
  cigPromise ??= getJson("/api/cig").then((d) =>
    d && d.summary && d.summary.projects && Array.isArray(d.projects) ? d : null);
  return cigPromise;
}

// What changed between the latest dashboard and the previous one (/api/cig/changes); null if unavailable.
export function getCigChanges() {
  return getJson("/api/cig/changes").then((d) => (d && Array.isArray(d.snapshots) ? d : null));
}

// Just the summary, for the homepage stats; null if unavailable.
export async function getCigSummary() {
  return (await getCig())?.summary ?? null;
}
