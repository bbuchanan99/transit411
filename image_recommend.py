"""Which library picture suits a story.

Same shape as recommend.py: the deterministic work is done here and the model is asked only where
judgement actually helps - and it RECOMMENDS ONLY. Nothing in this file changes what a post
carries; the Publish tab does that, when a human clicks.

The cascade, in order:

  1. a picture somebody already chose for this post   (never overridden)
  2. the APPROVED logo of an agency the story is about
  3. an APPROVED stock image matching the story's topics
  4. nothing - and the site's clean no-image state takes over

Only approved assets are ever offered. A candidate is invisible here no matter how good it looks.

Most stories need no model call at all: an agency story with one approved logo has exactly one
right answer, and computing it costs nothing. The model is asked only to choose between several
plausible stock images, which is the one step where taste beats a rule.
"""

import json
import os

import image_library as L

MODEL_DEFAULT = "claude-haiku-4-5"

RUBRIC = """You choose the picture for a story in Transit411, a trade publication read by transit
agency staff, consultants and contractors.

You are given a story and a short list of stock images that are already approved for use. Pick the
one that best suits the story, or none of them if none fits.

Prefer an image that shows the thing the story is actually about - the mode, the setting, the kind
of work. Reject one that would mislead: a light-rail photo on a bus story, a construction site on
a leadership appointment, a picture of another country's system on a US federal funding story.
A generic but honest image beats a specific but wrong one, and NO image beats a misleading one.

Return ONLY JSON: {"image_id": <id or null>, "reason": "under 15 words", "confidence": "high|medium|low"}
No prose, no markdown fence."""


def agencies_in(article):
    """Agency names this story is about, from the tags the collector already applied."""
    return [a for a in (article.get("agencies") or []) if a]


def topics_in(article):
    text = " ".join([article.get("title") or article.get("headline") or "",
                     article.get("summary") or "", " ".join(article.get("tags") or [])])
    return L.topics_for(text, article.get("mode"), article.get("pillar"))


def recommend(cur, article, client=None, model=None, use_model=True):
    """-> {choice, kind, image, reason, source, considered}. Never writes anything."""
    # 1. Somebody already chose. Nothing to recommend.
    if (article.get("image_url") or "").strip() and article.get("image_source") != "none":
        return {"choice": "existing", "kind": None, "image": None,
                "reason": "This post already carries a chosen picture.", "considered": 0}

    # 2. An agency logo, if the story is about an agency that has one approved.
    for name in agencies_in(article):
        logo = L.approved_logo(cur, name)
        if logo:
            return {"choice": "logo", "kind": "logo", "image": logo,
                    "reason": "Story is about %s, which has an approved logo." % name,
                    "considered": 1}

    # 3. Approved stock on the story's topics.
    tags = topics_in(article)
    pool = L.approved_stock(cur, tags, limit=6)
    if not pool:
        return {"choice": "none", "kind": None, "image": None,
                "reason": "No approved image matches this story (topics: %s)."
                          % (", ".join(tags) or "none detected"),
                "considered": 0, "topics": tags}
    if len(pool) == 1 or not use_model:
        best = pool[0]
        return {"choice": "stock", "kind": "stock", "image": best,
                "reason": "Only approved image matching %s." % ", ".join(best.get("topic_tags") or tags),
                "considered": len(pool), "topics": tags}

    picked = _ask_model(article, pool, client=client, model=model)
    if picked and picked.get("image_id"):
        chosen = next((p for p in pool if p["id"] == picked["image_id"]), None)
        if chosen:
            return {"choice": "stock", "kind": "stock", "image": chosen,
                    "reason": picked.get("reason") or "Best match for this story.",
                    "confidence": picked.get("confidence"), "considered": len(pool), "topics": tags}
    # The model declined, or answered with something not on the list. Fall back to the best
    # topic overlap rather than to nothing: the pool is already approved and already on-topic.
    best = pool[0]
    return {"choice": "stock", "kind": "stock", "image": best,
            "reason": (picked or {}).get("reason") or "Best topic overlap.",
            "considered": len(pool), "topics": tags}


def rank(cur, article, limit=5, client=None, model=None, use_model=True):
    """The best few library assets for this story, best first, each saying why.

    This is what the picker shows at the top. It is a SHORTLIST, not a decision - the whole point
    is that a person looks at five pictures and chooses one. Only approved assets appear.
    """
    out, seen = [], set()

    def add(img, why, kind):
        if not img or img.get("id") in seen or not img.get("url"):
            return
        seen.add(img["id"])
        out.append(dict(img, why=why, kind=kind))

    # An agency's own logo is the strongest answer there is, so it leads.
    for name in agencies_in(article):
        logo = L.approved_logo(cur, name)
        if logo:
            add(logo, "%s's own logo" % name, "logo")

    tags = topics_in(article)
    pool = L.approved_stock(cur, tags, limit=max(limit * 3, 12))
    for p in pool:
        shared = [t for t in (p.get("topic_tags") or []) if t in tags]
        add(p, "matches " + (", ".join(shared) or "this story's topic"), "stock")

    # Let the model put the best stock image first. It only reorders what is already approved and
    # already on-topic, so a failed call costs nothing but the original order.
    stock = [o for o in out if o["kind"] == "stock"]
    if use_model and len(stock) > 1:
        picked = _ask_model(article, stock, client=client, model=model)
        best_id = (picked or {}).get("image_id")
        if best_id:
            for o in out:
                if o["id"] == best_id:
                    o["why"] = (picked.get("reason") or o["why"])
                    o["ai_top"] = True
                    out.remove(o)
                    # after any logo, before the other stock
                    at = sum(1 for x in out if x["kind"] == "logo")
                    out.insert(at, o)
                    break
    return {"items": out[:limit], "topics": tags, "considered": len(out)}


def _ask_model(article, pool, client=None, model=None):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key and client is None:
        return None
    try:
        if client is None:
            import anthropic
            client = anthropic.Anthropic(api_key=key)
        payload = {
            "story": {
                "headline": article.get("title") or article.get("headline") or "",
                "summary": (article.get("summary") or "")[:400],
                "pillar": article.get("pillar") or "",
                "agencies": agencies_in(article)[:4],
                "mode": article.get("mode") or [],
            },
            "images": [{"id": p["id"], "topics": p.get("topic_tags") or [],
                        "description": (p.get("notes") or "")[:120]} for p in pool],
        }
        msg = client.messages.create(
            model=model or os.environ.get("RECOMMEND_MODEL", MODEL_DEFAULT),
            max_tokens=200, system=RUBRIC,
            messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}])
        text = "".join(b.text for b in msg.content if b.type == "text").strip()
        text = text[text.find("{"):text.rfind("}") + 1]
        d = json.loads(text)
        return {"image_id": d.get("image_id"), "reason": str(d.get("reason") or "")[:160],
                "confidence": d.get("confidence")}
    except Exception:
        return None       # a failed recommendation falls back to the rule, never to an error


def for_post(cur, post_id, **kw):
    """Recommend for one published post, by id."""
    cur.execute("SELECT id, title, body, pillar, agencies, mode, tags, state, image_url, image_source "
                "FROM content_posts WHERE id=%s", (post_id,))
    row = cur.fetchone()
    if not row:
        return None
    cols = ("id", "title", "body", "pillar", "agencies", "mode", "tags", "state",
            "image_url", "image_source")
    art = dict(zip(cols, row))
    art["summary"] = (art.get("body") or "").split("\n\nSource:")[0]
    return recommend(cur, art, **kw)
