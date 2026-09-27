// The NTD landing pages read a committed snapshot, not the API. See tools/ntd_seo_export.py for
// why: the public API exposes no raw NTD data, only the model-backed /api/ask, and NTD publishes
// once a year - so a snapshot is both cheaper and more honest than a live fetch.
import agenciesData from "../data/ntd-agencies.json";
import rankingsData from "../data/ntd-rankings.json";

export const NTD_YEAR = agenciesData.meta.year;
export const NTD_META = agenciesData.meta;
export const agencies = agenciesData.agencies;
export const rankings = rankingsData.rankings;

export const agencyBySlug = (slug) => agencies.find((a) => a.slug === slug);
export const rankingBySlug = (slug) => rankings.find((r) => r.slug === slug);

// The citation that appears on every one of these pages. It is permanent on purpose: these pages
// make quantitative claims about real organisations, and a number without its source is a rumour.
export const SOURCE_NOTE =
  `Figures are from the Federal Transit Administration's National Transit Database (NTD) annual ` +
  `reporting for ${NTD_YEAR}, the most recent published report year. Cost per rider is operating ` +
  `expense divided by unlinked passenger trips. Farebox recovery is fare revenue divided by ` +
  `operating expense. Ten-year trends are adjusted for inflation to ${NTD_YEAR} dollars; ` +
  `single-year figures are as reported. Agencies self-report to the NTD, and reporting practice ` +
  `varies, so figures are best read as comparable in magnitude rather than to the cent.`;

// ---- formatting ---------------------------------------------------------------------------------
export const int = (n) => (n == null ? "—" : Math.round(n).toLocaleString("en-US"));
export const usd = (n, dp = 2) =>
  n == null ? "—" : "$" + n.toLocaleString("en-US", { minimumFractionDigits: dp, maximumFractionDigits: dp });
export const pct = (n, dp = 1) => (n == null ? "—" : (n * 100).toFixed(dp) + "%");

export function bigUsd(n) {
  if (n == null) return "—";
  if (n >= 1e9) return "$" + (n / 1e9).toFixed(n >= 1e10 ? 0 : 2) + " billion";
  if (n >= 1e6) return "$" + (n / 1e6).toFixed(n >= 1e8 ? 0 : 1) + " million";
  return usd(n, 0);
}
export function bigNum(n) {
  if (n == null) return "—";
  if (n >= 1e9) return (n / 1e9).toFixed(2) + " billion";
  if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e8 ? 0 : 1) + " million";
  return int(n);
}
const ordinal = (n) => {
  const s = ["th", "st", "nd", "rd"], v = n % 100;
  return n + (s[(v - 20) % 10] || s[v] || s[0]);
};
// Prose reads better - and matches how people search - with the state written out.
const STATES = {AL:"Alabama",AK:"Alaska",AZ:"Arizona",AR:"Arkansas",CA:"California",CO:"Colorado",
  CT:"Connecticut",DE:"Delaware",DC:"the District of Columbia",FL:"Florida",GA:"Georgia",HI:"Hawaii",
  ID:"Idaho",IL:"Illinois",IN:"Indiana",IA:"Iowa",KS:"Kansas",KY:"Kentucky",LA:"Louisiana",ME:"Maine",
  MD:"Maryland",MA:"Massachusetts",MI:"Michigan",MN:"Minnesota",MS:"Mississippi",MO:"Missouri",
  MT:"Montana",NE:"Nebraska",NV:"Nevada",NH:"New Hampshire",NJ:"New Jersey",NM:"New Mexico",
  NY:"New York",NC:"North Carolina",ND:"North Dakota",OH:"Ohio",OK:"Oklahoma",OR:"Oregon",
  PA:"Pennsylvania",PR:"Puerto Rico",RI:"Rhode Island",SC:"South Carolina",SD:"South Dakota",
  TN:"Tennessee",TX:"Texas",UT:"Utah",VT:"Vermont",VA:"Virginia",WA:"Washington",WV:"West Virginia",
  WI:"Wisconsin",WY:"Wyoming"};
export const stateName = (ab) => STATES[ab] || ab;
const SMALL = ["zero","one","two","three","four","five","six","seven","eight","nine","ten"];
const spell = (n) => (n >= 0 && n <= 10 ? SMALL[n] : String(n));

const list = (xs) =>
  xs.length <= 1 ? (xs[0] || "") : xs.slice(0, -1).join(", ") + " and " + xs[xs.length - 1];

// ---- prose, composed from the numbers themselves -------------------------------------------------
// Not a template with the values dropped in: which sentences appear, and what they say, depends on
// what the data actually shows. An agency whose ridership is above 2019 gets a different sentence
// from one still below it, and a single-mode operator does not get a multi-mode sentence at all.
export function agencyProse(a) {
  const out = [];
  const modes = a.modes.map((m) => m.mode.toLowerCase());
  const where = [a.city, a.state].filter(Boolean).join(", ");

  const place = a.national_rank_by_upt === 1
    ? "the busiest transit operator in the United States"
    : `the ${ordinal(a.national_rank_by_upt)}-busiest transit operator in the United States`;
  const inState = a.state_rank_by_upt === 1 ? `, and the largest in ${stateName(a.state)}` : "";
  out.push(
    `${a.agency} carries ${bigNum(a.total_upt)} passenger trips a year` +
    (modes.length > 1 ? ` across ${spell(modes.length)} modes — ${list(modes)}` : ` on ${modes[0]} service`) +
    ` — making it ${place}${inState}.` + (where ? ` It is based in ${where}.` : ""));

  const top = a.modes[0];
  let s2 = `It spends ${bigUsd(a.total_opex)} a year running that service, or ${usd(a.cost_per_rider)} per trip.`;
  if (top && top.vs_mode_median_pct != null) {
    const d = top.vs_mode_median_pct;
    s2 += ` Its ${top.mode.toLowerCase()} service, the largest of its modes, costs ${usd(top.cost_per_rider)} `
      + `per rider — ${Math.abs(d)}% ${d < 0 ? "below" : "above"} the ${usd(top.mode_median_cost_per_rider)} `
      + `median for the ${int(top.mode_agencies)} US agencies reporting ${top.mode.toLowerCase()}.`;
  }
  out.push(s2);

  // Trend, but only when there is enough history for the comparison to mean anything.
  const t = a.trend || [];
  const latest = t[t.length - 1];
  const y2019 = t.find((x) => x.year === 2019);
  const prior = t[t.length - 2];
  if (latest && (y2019 || prior)) {
    const bits = [];
    if (y2019 && y2019.upt) {
      const d = Math.round((latest.upt / y2019.upt - 1) * 100);
      bits.push(d >= 0
        ? `ridership is ${d}% above its 2019 level, the last full year before the pandemic`
        : `ridership remains ${Math.abs(d)}% below its 2019 level, the last full year before the pandemic`);
    }
    if (prior && prior.upt) {
      const d = Math.round((latest.upt / prior.upt - 1) * 100);
      bits.push(`${d === 0 ? "flat on" : d > 0 ? `up ${d}% on` : `down ${Math.abs(d)}% on`} ${prior.year}`);
    }
    let s3 = `Over the ${t.length} years reported, ${bits.join(", and ")}.`;
    if (a.fare_recovery != null) {
      s3 += ` Fares cover ${pct(a.fare_recovery, 0)} of operating cost.`;
    }
    out.push(s3);
  }
  return out;
}

export function rankingProse(r) {
  const out = [];
  const first = r.rows[0], last = r.rows[r.rows.length - 1];
  const mode = r.mode.toLowerCase();
  const unit = r.metric === "cost_per_rider" ? "cost per rider"
    : r.metric === "fare_recovery" ? "farebox recovery" : "annual ridership";
  const val = (row) => r.metric === "cost_per_rider" ? usd(row.cost_per_rider)
    : r.metric === "fare_recovery" ? pct(row.fare_recovery, 0) : bigNum(row.upt) + " trips";

  out.push(
    `This table ranks ${r.rows.length} US ${mode} operators by ${unit} for ${r.year}` +
    (r.floor ? `, limited to systems carrying at least ${bigNum(r.floor)} trips a year so that a very small operator cannot top the list on a handful of rides` : "") +
    `. ${int(r.mode_agencies)} agencies reported ${mode} service in total, carrying ${bigNum(r.mode_total_upt)} trips between them.`);

  if (r.metric === "cost_per_rider" && first.cost_per_rider && last.cost_per_rider) {
    const lo = r.order === "asc" ? first : last, hi = r.order === "asc" ? last : first;
    const ratio = hi.cost_per_rider / lo.cost_per_rider;
    out.push(
      `${lo.agency} runs the cheapest ${mode} service on this list at ${usd(lo.cost_per_rider)} per rider, ` +
      `against ${usd(hi.cost_per_rider)} at ${hi.agency} — a spread of ${ratio.toFixed(1)}×. ` +
      `The national median for ${mode} is ${usd(r.mode_median_cost_per_rider)} per rider.`);
    out.push(
      `A high cost per rider is not by itself a sign of waste: a system covering long distances, ` +
      `running at low density, or carrying fewer riders than the service it operates was built for ` +
      `will show a higher figure than a dense, heavily used one. The number is a starting point for ` +
      `a question, not the answer to it.`);
  } else {
    out.push(`${first.agency} leads at ${val(first)}.`);
  }
  return out;
}

// A tiny inline SVG trend line. Server-rendered so it is in the HTML a crawler sees, and so the
// page needs no JavaScript to make its point.
export function trendSvg(trend, key, { w = 640, h = 170, pad = 34 } = {}) {
  const pts = trend.filter((t) => t[key] != null);
  if (pts.length < 3) return null;
  const xs = pts.map((t) => t.year);
  const ys = pts.map((t) => t[key]);
  const minY = Math.min(...ys), maxY = Math.max(...ys);
  const span = maxY - minY || 1;
  const x = (i) => pad + (i * (w - pad * 2)) / (pts.length - 1);
  const y = (v) => h - pad - ((v - minY) / span) * (h - pad * 2);
  const d = pts.map((t, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(t[key]).toFixed(1)}`).join("");
  return {
    d, w, h,
    dots: pts.map((t, i) => ({ cx: x(i).toFixed(1), cy: y(t[key]).toFixed(1), year: t.year, value: t[key] })),
    first: { year: xs[0], value: ys[0] },
    last: { year: xs[xs.length - 1], value: ys[ys.length - 1] },
    min: minY, max: maxY,
  };
}

// Which ranked lists an agency actually appears in - the interlink, computed rather than guessed.
export function rankingsFor(slug) {
  return rankings
    .map((r) => {
      const row = r.rows.find((x) => x.slug === slug);
      return row ? { slug: r.slug, title: r.title, rank: row.rank, of: r.rows.length } : null;
    })
    .filter(Boolean);
}
