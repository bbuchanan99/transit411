// Central content source for the public site.
// PREVIEW: returns sample posts so pages build before the API is connected.
// AT LAUNCH: replace getPosts() with a fetch of published posts, e.g.
//   const base = import.meta.env.PUBLIC_API_BASE;
//   const q = pillar ? `?pillar=${encodeURIComponent(pillar)}` : "";
//   const r = await fetch(`${base}/api/posts${q}`); return (await r.json()).posts;

export const SECTIONS = {
  news:        { label: "News",              pillar: null,          blurb: "Everything moving across transit funding, procurement, people, and policy." },
  funding:     { label: "Funding & Finance", pillar: "Funding",     blurb: "FTA grants, ballot measures, appropriations, and the money behind transit capital projects." },
  procurement: { label: "Procurement",       pillar: "Procurement", blurb: "RFPs, RFQs, and contract awards for program management, design-build, and delivery." },
  people:      { label: "People",            pillar: "People",      blurb: "Leadership moves and appointments across agencies and firms." },
  policy:      { label: "Policy",            pillar: "Policy",      blurb: "Reauthorization, federal rules, and the policy shaping transit investment." },
};

const SAMPLE = [
  { pillar: "Funding",     title: "FTA finalizes updated Capital Investment Grants policy guidance", deck: "Sample preview item. At launch, real published posts appear here, tagged by agency and program." },
  { pillar: "Funding",     title: "Regional sales-tax measure qualifies for the November ballot",     deck: "Sample preview item describing a transit funding measure headed to voters." },
  { pillar: "Funding",     title: "Agency advances to the Engineering phase of its CIG project",       deck: "Sample preview item on a project moving through the FTA pipeline." },
  { pillar: "Procurement", title: "Transit authority releases progressive design-build RFQ for a rail extension", deck: "Sample preview procurement notice for a major delivery contract." },
  { pillar: "Procurement", title: "RFP issued for program management services on a BRT corridor",      deck: "Sample preview item for a consulting/PM opportunity." },
  { pillar: "Procurement", title: "Vehicle procurement solicitation opens for a bus fleet replacement", deck: "Sample preview procurement item." },
  { pillar: "People",      title: "New chief program officer named at a major transit authority",       deck: "Sample preview people-on-the-move item." },
  { pillar: "People",      title: "Agency selects a permanent CEO after an interim tenure",             deck: "Sample preview leadership item." },
  { pillar: "Policy",      title: "Surface transportation reauthorization advances out of committee",   deck: "Sample preview policy item on federal transit funding." },
  { pillar: "Policy",      title: "Continuing resolution extends surface programs at current levels",   deck: "Sample preview policy item." },
];

export function getPosts(pillar) {
  return pillar ? SAMPLE.filter((p) => p.pillar === pillar) : SAMPLE;
}
