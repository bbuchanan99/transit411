"""AI editorial triage for the review queue.

The collector fills `collected_items` faster than anyone can read it (262 pending as of this
writing). This ranks that queue for a professional transit audience and says why, so review starts
at the top instead of at whatever arrived last.

It RECOMMENDS ONLY. Nothing here changes an item's status, publishes, or skips - it writes a score,
a reason, an action and flags onto the row, and the Command Center sorts by them. Approve/skip/
publish stay exactly where they were: with a human.

Cost is controlled three ways: it runs on demand (a button, never on page load), it sends the queue
in a few batched calls rather than one call per item, and the result is cached on the row until the
button is pressed again.

What the model is NOT asked to do, because a deterministic pass does it better and for free:
  - finding duplicate candidates (headline similarity, computed here and passed in as a hint)
  - spotting a tie-in to our own CIG/NTD data (matched against the agency reference table)
  - assembling the balanced publish set (chosen from the scores, so it is reproducible)
"""

import json
import os
import re
from datetime import datetime, timezone

# Items per model call. The whole queue in one call would work, but one truncated or malformed
# response would then cost the entire run; a bad batch here costs a batch.
BATCH = 40
MODEL_DEFAULT = "claude-haiku-4-5"

# Roughly what one run costs, for the report in the UI. Per million tokens.
PRICES = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-opus-5-5": (15.00, 75.00),
}

ACTIONS = ("publish", "hold", "skip")
FLAGS = ("likely_duplicate", "lead_candidate")

# Second pass: how many of the best items get read in full, and how much of each we send.
DEEP_N = 40
DEEP_CHARS = 6000
MIN_DEEP_CHARS = 600    # below this it's a consent page or a stub, not the story
# The second pass may lower a score freely and raise it only this far. The asymmetry is deliberate:
# a demotion is evidence-based ("the text shows this is a press release", "this is the weaker of
# two"), while the batch it sees contains only strong items, so it drifts upward by comparing them
# with each other. Left uncapped it promoted a highway-bridge story to 68 while its own reason read
# "non-transit infrastructure; stale".
DEEP_MAX_RAISE = 15
# Wrappers that return a redirect shell rather than the article. Fetching one yields text, but it
# is the aggregator's furniture - and an item judged on that gets marked down for our failure to
# read it rather than for anything about the story.
AGGREGATORS = ("news.google.com", "google.com/url", "flipboard.com", "news.yahoo.com/rss")
HOST_INTERVAL = 1.0     # seconds between requests to the same publisher
FETCH_WORKERS = 4

RUBRIC = """You are the editor of Transit411, a trade publication for people who work in public
transit: agency staff, consultants, contractors and vendors in capital funding, procurement,
project delivery and policy. You are triaging a review queue. You recommend; a human decides.

Score each item 0-100 for how much it deserves to be published, weighing:

1. AUDIENCE RELEVANCE (heaviest). Transit capital funding, FTA Capital Investment Grants,
   procurement and RFP/RFQ/contract awards, agency leadership moves, federal policy and
   reauthorization. This is a niche trade audience: a story that a general news reader would find
   interesting but a transit professional would not is LOW. Operations incidents, crime, single
   service changes, fare-evasion and consumer-interest stories are LOW unless they drive a funding,
   procurement or leadership consequence.
2. IMPORTANCE. A major funding decision, a large solicitation or award, a CEO appointment at a
   large agency outrank a minor local note. Size and irreversibility matter.
3. TIMELINESS. Use the freshness score given for each item. A decaying story needs to be more
   important to earn the same score. An open deadline that has not passed keeps its value.
4. UNIQUENESS. Items sharing a dupe_group are about the same underlying event; recommend at most
   the best one and mark the others likely_duplicate. An item whose already_published flag is set
   covers something we have already run - it is not a second story.
5. DATA TIE-IN. Items with data_tie set touch an agency or project in our own CIG pipeline or NTD
   data, so we can pair the story with figures nobody else has. That pairing is our differentiator:
   treat it as a real bonus, not a tiebreaker.
6. SOURCE CREDIBILITY. An original report from a reputable outlet, an agency or a federal source
   outranks an aggregator or a press release rewritten by a content farm.

Pillar balance is handled outside your answer. Do NOT adjust a score to balance pillars - score each
item on its own merit and let the selection step balance them.

Actions: "publish" = worth running now. "hold" = real but not yet, or needs the human to check
something. "skip" = not for this audience, a duplicate, or too stale to matter.

Return ONLY a JSON array, one object per input item, in the same order:
[{"id":<the item's id>,"score":0-100,"action":"publish|hold|skip",
  "reason":"one line, under 20 words, saying WHY - specific to this item, not generic",
  "flags":["likely_duplicate"|"lead_candidate"]}]
Two budgets, because a recommendation that flags everything recommends nothing:
- Be selective with "publish". In a working queue most items are not worth running now; a score
  above 75 with action "publish" should mean you would defend it to the editor.
- Mark lead_candidate on AT MOST TWO of the items in this input, and only if they could genuinely
  carry the top of the homepage. Often the right answer is none.
Items sharing a dupe_group are in this same input so you can compare them directly: pick the single
best one and mark every other member of that group likely_duplicate.

Use [] when no flag applies. No prose, no markdown fence."""


# ---- deterministic pre-pass -----------------------------------------------------------------

STOP = set("a an the of in on at to for and or with by from as is are was were be been this that "
           "its his her their new more than after before over under into out up down not no "
           "us u.s. transit agency agencies plan plans project projects".split())


def _tokens(text):
    """Content words of a headline, for comparing two headlines without a model."""
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {w for w in words if len(w) > 2 and w not in STOP}


MIN_SHARED = 3          # below this, a high ratio is coincidence rather than the same story


def _similar(a, b):
    """How much of the shorter headline the two share: 1.0 when one contains the other's content
    words, 0.0 when they share none.

    Deliberately not Jaccard. Two reports of the same event are rarely the same length - "FTA awards
    $200M to Austin Light Rail Phase 1" against "Austin Light Rail Phase 1 wins $200 million federal
    grant from FTA" scores 0.50 on Jaccard, under any threshold that isn't also full of false
    positives, but 0.75 here.
    """
    if not a or not b:
        return 0.0
    shared = len(a & b)
    if shared < MIN_SHARED:
        return 0.0
    return shared / min(len(a), len(b))


DUPE_AT = 0.7           # two headlines this alike are nearly always the same event
PUBLISHED_AT = 0.65     # slightly looser against things we already ran


def dupe_groups(items, published_titles=(), threshold=DUPE_AT, extra_pairs=(), seen_before=()):
    """Cluster items describing the same event, and flag ones we have already run.

    Two signals, unioned. Headline overlap catches rewordings. `extra_pairs` carries the pairs the
    embedding model called near-identical (see embed.py), which is what catches "CTA breaks ground
    on Red Line Extension" against "Chicago Transit Authority begins construction on Red Line
    Extension" - almost no shared words, obviously one story. `seen_before` is the same signal
    against what we have already published.

    Returns {item_id: {"group": int|None, "already_published": bool}}. Single-item groups get None:
    a group of one is not a duplicate of anything and only adds noise to the prompt.
    """
    toks = {it["id"]: _tokens(it.get("headline")) for it in items}
    state = {it["id"]: (it.get("state") or "").strip().upper() for it in items}
    pub = [_tokens(t) for t in published_titles]
    group_of, groups = {}, 0
    ids = [it["id"] for it in items]
    known = set(ids)
    pairs = [(a, b) for i, a in enumerate(ids) for b in ids[i + 1:]
             if _similar(toks[a], toks[b]) >= threshold]
    pairs += [(a, b) for a, b in extra_pairs if a in known and b in known and a != b]
    for a, b in pairs:
        # "Seattle voters approve transit sales tax" and "Denver voters approve transit sales
        # tax" are near-identical by words AND by meaning, and are different stories. Two items
        # we have placed in different states are never the same event - so this guard applies
        # to the embedding signal just as much as the lexical one.
        if state.get(a) and state.get(b) and state[a] != state[b]:
            continue
        ga, gb = group_of.get(a), group_of.get(b)
        if ga is None and gb is None:
            groups += 1
            group_of[a] = group_of[b] = groups
        elif ga is None:
            group_of[a] = gb
        elif gb is None:
            group_of[b] = ga
        elif ga != gb:                                  # merge two clusters
            for k, v in list(group_of.items()):
                if v == gb:
                    group_of[k] = ga
    seen = set(seen_before)
    return {
        it["id"]: {
            "group": group_of.get(it["id"]),
            "already_published": it["id"] in seen
            or any(_similar(toks[it["id"]], p) >= PUBLISHED_AT for p in pub),
        }
        for it in items
    }


def data_tie(item, agency_names, cig_sponsors):
    """Does this item touch something in our own data? Reuses the agency reference table and the
    CIG pipeline's sponsor list rather than asking the model to guess."""
    names = {(a or "").strip().lower() for a in (item.get("agencies") or [])}
    if names & cig_sponsors:
        return "cig"
    if any(p.startswith("CIG ") for p in (item.get("programs") or [])):
        return "cig"
    if names & agency_names:
        return "ntd"
    return None


# ---- the model call -------------------------------------------------------------------------

def batches(items, dupes, size=BATCH):
    """Split the queue into model calls, keeping every member of a duplicate group in the SAME call.

    This is the difference between the model being able to answer and not: asked "is this a
    duplicate?" about one of five near-identical MTA stories with the other four in a different
    call, it can only say "there is a group" - it cannot pick which one to keep. Together, it can.
    """
    groups, loose = {}, []
    for it in items:
        g = dupes.get(it["id"], {}).get("group")
        if g is None:
            loose.append(it)
        else:
            groups.setdefault(g, []).append(it)

    out, cur = [], []
    # Groups first, whole; then the singletons fill the remaining space.
    for members in sorted(groups.values(), key=len, reverse=True):
        if cur and len(cur) + len(members) > size:
            out.append(cur)
            cur = []
        cur.extend(members)
        if len(cur) >= size:
            out.append(cur)
            cur = []
    for it in loose:
        cur.append(it)
        if len(cur) >= size:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def _payload(item, dupe, tie):
    """The compact view of one item the model scores. Summaries are trimmed: the headline plus a
    couple of sentences is enough to triage, and the whole queue has to fit in one budget."""
    return {
        "id": item["id"],
        "headline": item.get("headline") or "",
        "summary": (item.get("summary") or "")[:400],
        "pillar": item.get("pillar") or "",
        "source": item.get("source_name") or "",
        "published": item.get("published") or "",
        "freshness": item.get("fresh_score"),
        "relevance_at_collection": item.get("relevance") or "",
        "agencies": (item.get("agencies") or [])[:4],
        "programs": (item.get("programs") or [])[:4],
        "state": item.get("state") or "",
        "dupe_group": dupe.get("group"),
        "already_published": dupe.get("already_published"),
        "data_tie": tie,
    }


def _parse(text, expected_ids):
    """The model's reply -> {id: {...}}, keeping only ids we actually asked about."""
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        rows = json.loads(text)
    except Exception:
        start, end = text.find("["), text.rfind("]")
        if start < 0 or end <= start:
            return {}
        try:
            rows = json.loads(text[start:end + 1])
        except Exception:
            return {}
    out = {}
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        try:
            rid = int(r.get("id"))
        except Exception:
            continue
        if rid not in expected_ids:
            continue
        try:
            score = max(0, min(100, int(round(float(r.get("score", 0))))))
        except Exception:
            score = 0
        action = str(r.get("action", "")).strip().lower()
        flags = [f for f in (r.get("flags") or []) if f in FLAGS] if isinstance(r.get("flags"), list) else []
        out[rid] = {
            "score": score,
            "action": action if action in ACTIONS else "hold",
            "reason": str(r.get("reason") or "").strip()[:200],
            "flags": flags,
        }
    return out


def score_items(items, published_titles=(), agency_names=frozenset(), cig_sponsors=frozenset(),
                model=None, client=None, extra_pairs=(), seen_before=()):
    """Score the whole queue. Returns (results_by_id, usage) where usage counts tokens actually
    billed, so the UI can show what the run cost."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key and client is None:
        raise RuntimeError("ANTHROPIC_API_KEY is not set on this server")
    if client is None:
        import anthropic
        client = anthropic.Anthropic(api_key=key)
    model = model or os.environ.get("RECOMMEND_MODEL", MODEL_DEFAULT)

    dupes = dupe_groups(items, published_titles, extra_pairs=extra_pairs, seen_before=seen_before)
    results, usage = {}, {"input_tokens": 0, "output_tokens": 0, "calls": 0, "failed_batches": 0}

    for chunk in batches(items, dupes):
        ids = {it["id"] for it in chunk}
        payload = [_payload(it, dupes.get(it["id"], {}), data_tie(it, agency_names, cig_sponsors))
                   for it in chunk]
        try:
            msg = client.messages.create(
                model=model,
                max_tokens=min(8000, 120 * len(chunk) + 500),
                system=RUBRIC,
                messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}])
            usage["calls"] += 1
            usage["input_tokens"] += getattr(msg.usage, "input_tokens", 0) or 0
            usage["output_tokens"] += getattr(msg.usage, "output_tokens", 0) or 0
            text = "".join(b.text for b in msg.content if b.type == "text")
            got = _parse(text, ids)
        except Exception:
            usage["failed_batches"] += 1
            got = {}
        results.update(got)

    # An item the model skipped or a batch that failed still needs a row, or it would silently
    # vanish from a queue sorted by score. It keeps its place with no recommendation.
    for it in items:
        results.setdefault(it["id"], {"score": None, "action": None, "reason": "", "flags": []})
    for it in items:
        d = dupes.get(it["id"], {})
        r = results[it["id"]]
        if d.get("already_published") and "likely_duplicate" not in r["flags"]:
            r["flags"] = r["flags"] + ["likely_duplicate"]
        r["dupe_group"] = d.get("group")
    return results, usage


# ---- reading the story itself ----------------------------------------------------------------
#
# A collector summary is two sentences: enough to tell a funding story from a fare-evasion story,
# not enough to tell a $5B award from a press release about one. The best items get read properly.
#
# The article text is used to score and then thrown away. It is never stored and never published -
# Transit411 publishes its own summary, the metadata and a link, and nothing else.
# A publisher that refuses automated requests is left alone: no retry, no second user agent.

_DROP = re.compile(r"(?is)<(script|style|noscript|svg|form|nav|header|footer|aside)\b.*?</\1>")
_TAGS = re.compile(r"(?s)<[^>]+>")
_MAIN = re.compile(r"(?is)<(article|main)\b[^>]*>(.*?)</\1>")
_WS = re.compile(r"[ \t\r\f\v]+")
_BLANK = re.compile(r"\n{3,}")


def html_to_text(html, limit=DEEP_CHARS):
    """Readable text from article markup. The <article>/<main> body when the page marks one up,
    otherwise the whole document with the furniture stripped out."""
    if not html:
        return ""
    body = _MAIN.search(html)
    chunk = body.group(2) if body else html
    chunk = _DROP.sub(" ", chunk)
    chunk = re.sub(r"(?i)</(p|div|li|h[1-6]|tr)>", "\n", chunk)
    text = _TAGS.sub(" ", chunk)
    for ent, ch in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                    ("&quot;", '"'), ("&#39;", "'"), ("&rsquo;", "'"), ("&ldquo;", '"'),
                    ("&rdquo;", '"'), ("&mdash;", "-"), ("&ndash;", "-")):
        text = text.replace(ent, ch)
    text = _WS.sub(" ", text)
    text = _BLANK.sub("\n\n", "\n".join(line.strip() for line in text.split("\n")))
    return text.strip()[:limit]


def fetch_article_text(url, limit=DEEP_CHARS):
    """The story's own words, or "" if the publisher doesn't serve us. Never raises."""
    try:
        import images
        return html_to_text(images.fetch_html(url), limit)
    except Exception:
        return ""       # 403, timeout, a PDF, a paywall - the item keeps its first-pass score


def fetch_many(urls, workers=FETCH_WORKERS, limit=DEEP_CHARS):
    """Fetch a shortlist of articles, at most one request per second to any one publisher."""
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor
    from urllib.parse import urlparse

    guard, next_free = threading.Lock(), {}

    def one(url):
        host = (urlparse(url).netloc or "").lower()
        while True:                                  # wait our turn for this publisher
            with guard:
                now = time.monotonic()
                ready = next_free.get(host, 0.0)
                if now >= ready:
                    next_free[host] = now + HOST_INTERVAL
                    break
                wait = ready - now
            time.sleep(min(wait, HOST_INTERVAL))
        return url, fetch_article_text(url, limit)

    urls = [u for u in urls if u]
    if not urls:
        return {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return {u: t for u, t in pool.map(one, urls)}


DEEP_RUBRIC = RUBRIC + """

You are now re-scoring a SHORTLIST, and this time you have each story's own text, not just the
collector's two-sentence summary. Use it to correct the first pass: confirm the numbers, the stage
of the project and who is actually involved. Common corrections, in both directions:
- A headline that sounded like a major award turns out to be a press release, a re-announcement of
  money already reported, or a study - score it DOWN.
- A flat-sounding summary turns out to carry a real figure, a named contract or a decision with a
  date - score it UP.
- The text shows the story is really about an operations or consumer matter - score it DOWN.

Keep the SAME absolute 0-100 scale as the first pass. You are seeing a handful of items, all of
which scored well; do not re-rank them against each other and do not drift upward because this
batch is strong. first_pass_score is a prior to correct when the text contradicts it, not a
baseline to nudge.

Your score and your reason must agree. If your reason says the item is out of scope for a US
transit trade audience, not about transit at all (a highway or bridge project with no transit
component), an opinion or advocacy piece, a press release, or a weaker retelling of another item,
then the score must be below 40 and the action must not be "publish"."""


def deep_score(items, results, model=None, client=None, top_n=DEEP_N, usage=None):
    """Re-score the best items with their full text. Returns the ids that were re-scored.

    Only the shortlist is fetched and re-sent: reading all 262 would mean 262 requests to
    publishers and several times the tokens, to change the ranking of items nobody will reach.
    """
    ranked = sorted((it for it in items if results.get(it["id"], {}).get("score") is not None),
                    key=lambda it: results[it["id"]]["score"], reverse=True)
    keep = [it for it in ranked if results[it["id"]].get("action") != "skip"]
    # The shortlist is the best items we can actually READ. Most of the queue arrives as Google
    # News redirect links, whose target is only resolvable by running their JavaScript - so those
    # are left on their first-pass score rather than spending the budget on 40 consent pages.
    short = [it for it in keep
             if it.get("source_url") and not any(a in it["source_url"] for a in AGGREGATORS)][:top_n]
    if not short:
        return set()

    texts = fetch_many([it["source_url"] for it in short])
    # Only items we genuinely read go to the second pass. An item we could not read keeps its
    # first-pass score untouched - never a penalty for a publisher that declined us.
    have = [it for it in short
            if len(texts.get(it.get("source_url") or "", "")) >= MIN_DEEP_CHARS]
    if not have:
        return set()

    if client is None:
        import anthropic
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    model = model or os.environ.get("RECOMMEND_MODEL", MODEL_DEFAULT)
    usage = usage if usage is not None else {"input_tokens": 0, "output_tokens": 0, "calls": 0,
                                             "failed_batches": 0}
    deepened = set()
    per_call = 8                       # full text is bulky; small batches keep each reply complete
    for start in range(0, len(have), per_call):
        chunk = have[start:start + per_call]
        payload = [{"id": it["id"], "headline": it.get("headline") or "",
                    "pillar": it.get("pillar") or "", "source": it.get("source_name") or "",
                    "freshness": it.get("fresh_score"),
                    "first_pass_score": results[it["id"]]["score"],
                    "article_text": texts[it["source_url"]]} for it in chunk]
        ids = {it["id"] for it in chunk}
        try:
            msg = client.messages.create(
                model=model, max_tokens=200 * len(chunk) + 500, system=DEEP_RUBRIC,
                messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}])
            usage["calls"] += 1
            usage["input_tokens"] += getattr(msg.usage, "input_tokens", 0) or 0
            usage["output_tokens"] += getattr(msg.usage, "output_tokens", 0) or 0
            got = _parse("".join(b.text for b in msg.content if b.type == "text"), ids)
        except Exception:
            usage["failed_batches"] += 1
            got = {}
        for rid, r in got.items():
            prior = results[rid].get("score")
            if prior is not None and r.get("score") is not None:
                r["score"] = min(r["score"], prior + DEEP_MAX_RAISE)
            # The first pass found the duplicate groups; a re-read of one story must not lose them.
            r["dupe_group"] = results[rid].get("dupe_group")
            flags = set(r.get("flags") or [])
            if "likely_duplicate" in (results[rid].get("flags") or []):
                flags.add("likely_duplicate")
            r["flags"] = sorted(flags)
            results[rid] = r
            deepened.add(rid)
    return deepened


def run_cost(usage, model):
    """Dollar cost of a run, from the tokens the API reported."""
    pin, pout = PRICES.get(model, PRICES[MODEL_DEFAULT])
    return round(usage["input_tokens"] / 1e6 * pin + usage["output_tokens"] / 1e6 * pout, 4)


# ---- selection (deterministic, so the same scores always give the same set) --------------------

PILLARS = ("Funding", "Procurement", "People", "Policy")


PILLAR_CAP = 2          # of a 6-story set, no pillar may take more than this


def balanced_set(items, results, size=6, cap=PILLAR_CAP):
    """A publish set that spans the pillars instead of being five funding stories.

    One per pillar first (best available), then the highest scores left over - but no pillar takes
    more than `cap` slots, because funding always outnumbers everything else in the queue and a
    homepage of six funding stories is the thing this is meant to prevent. Duplicates collapse to
    the best of their group, and anything the model said to skip is out.
    """
    def score(it):
        s = results.get(it["id"], {}).get("score")
        return -1 if s is None else s

    # A group is only collapsed when the model agreed at least one of its members is a duplicate.
    # Headline similarity is a hint sent to the model, not a verdict - two genuinely separate
    # stories that happen to be worded alike must not silently lose one of themselves here.
    confirmed = set()
    for it in items:
        r = results.get(it["id"], {})
        if "likely_duplicate" in (r.get("flags") or []) and r.get("dupe_group") is not None:
            confirmed.add(r["dupe_group"])

    eligible, seen_groups = [], set()
    for it in sorted(items, key=score, reverse=True):
        r = results.get(it["id"], {})
        if r.get("action") == "skip" or score(it) < 0:
            continue
        g = r.get("dupe_group")
        if g is not None and g in confirmed:
            if g in seen_groups:
                continue                       # keep only the best of each duplicate cluster
            seen_groups.add(g)
        if "likely_duplicate" in (r.get("flags") or []):
            continue
        eligible.append(it)

    chosen, used, per = [], set(), {}
    for p in PILLARS:
        for it in eligible:
            if it["id"] not in used and (it.get("pillar") or "") == p:
                chosen.append(it)
                used.add(it["id"])
                per[p] = 1
                break
    for it in eligible:                       # fill by score, respecting the cap
        if len(chosen) >= size:
            break
        p = it.get("pillar") or ""
        if it["id"] in used or per.get(p, 0) >= cap:
            continue
        chosen.append(it)
        used.add(it["id"])
        per[p] = per.get(p, 0) + 1
    for it in eligible:                       # a small queue may not fill under the cap; then relax
        if len(chosen) >= size:
            break
        if it["id"] not in used:
            chosen.append(it)
            used.add(it["id"])
    chosen.sort(key=score, reverse=True)
    return chosen[:size]


def lead_story(items, results, among=None):
    """The one story to lead with: the best item the model flagged lead_candidate, else the best
    item overall. Restricted to `among` (the balanced set) when given, so the lead is in the set."""
    pool = among if among else items
    flagged = [it for it in pool if "lead_candidate" in (results.get(it["id"], {}).get("flags") or [])]
    pool = flagged or pool
    best, best_score = None, -1
    for it in pool:
        s = results.get(it["id"], {}).get("score")
        if s is not None and s > best_score:
            best, best_score = it, s
    return best


def recommendation_summary(items, results):
    """Counts for the UI header: how many of each action, and how many look like duplicates."""
    out = {"scored": 0, "publish": 0, "hold": 0, "skip": 0, "duplicates": 0, "lead_candidates": 0}
    for it in items:
        r = results.get(it["id"], {})
        if r.get("score") is None:
            continue
        out["scored"] += 1
        if r.get("action") in ACTIONS:
            out[r["action"]] += 1
        if "likely_duplicate" in (r.get("flags") or []):
            out["duplicates"] += 1
        if "lead_candidate" in (r.get("flags") or []):
            out["lead_candidates"] += 1
    return out


def now_iso():
    return datetime.now(timezone.utc).isoformat()
