"""Local sentence embeddings for semantic duplicate detection.

Headline overlap catches rewordings; it cannot catch "CTA breaks ground on Red Line Extension" and
"Chicago Transit Authority begins construction on Red Line Extension", which share almost no words
and are the same story. This fills `collected_items.embedding` and `content_posts.embedding` so the
recommender can compare meaning instead of vocabulary.

The model runs ON THE NAS. Nothing about the review queue is sent to a third party, which is the
same rule the rest of the stack follows - and there is no per-item cost, so every item can be
embedded, not just the ones we are about to look at.

It lives in its own image (Dockerfile.embed) because PyTorch is ~300MB and the web containers,
including the public-facing read-only API, have no business carrying it.

  docker compose run --rm embed --once      # embed anything missing, then exit
  docker compose up -d embed                # keep up; embeds new items as they arrive
"""

import os
import sys
import time

MODEL_NAME = os.environ.get("EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
EMBED_DIM = int(os.environ.get("EMBED_DIM", "384"))
BATCH = 64
INTERVAL = int(os.environ.get("EMBED_INTERVAL", "600"))     # seconds between sweeps when running up

_model = None


def model():
    """Loaded once per process: first use pays a few seconds, the rest are free."""
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(MODEL_NAME)
        got = (getattr(_model, "get_embedding_dimension", None) or
               _model.get_sentence_embedding_dimension)()
        if got != EMBED_DIM:
            raise SystemExit(
                f"{MODEL_NAME} produces {got}-dim vectors but the columns are {EMBED_DIM}. "
                f"Set EMBED_DIM={got} and re-run migrate so the stored vectors match.")
    return _model


def encode(texts):
    """-> list of float lists, L2-normalised so cosine similarity is a dot product."""
    if not texts:
        return []
    return [v.tolist() for v in model().encode(list(texts), batch_size=BATCH,
                                               normalize_embeddings=True, show_progress_bar=False)]


def text_of(headline, summary=None):
    """What gets embedded. The headline carries the event; a little summary disambiguates two
    stories with similar headlines. Longer text mostly adds noise for a dedupe comparison."""
    parts = [(headline or "").strip()]
    if summary:
        parts.append(" ".join((summary or "").split()[:60]))
    return ". ".join(p for p in parts if p)[:1000]


def _vec(v):
    """pgvector accepts its own literal form: '[0.1,0.2,...]'."""
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def migrate(conn):
    """Make both embedding columns the current model's width. Vectors of a different width are
    dropped rather than kept: a mix of two models in one column compares nonsense to nonsense."""
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE content_posts ADD COLUMN IF NOT EXISTS embedding VECTOR(%s)" % EMBED_DIM)
        for table in ("collected_items", "content_posts"):
            cur.execute(
                "SELECT atttypmod FROM pg_attribute "
                "WHERE attrelid = %s::regclass AND attname = 'embedding'", (table,))
            row = cur.fetchone()
            if row and row[0] != EMBED_DIM:
                print(f"  {table}.embedding is {row[0]}-dim, model is {EMBED_DIM}-dim: resetting")
                cur.execute(f"UPDATE {table} SET embedding = NULL WHERE embedding IS NOT NULL")
                cur.execute(f"ALTER TABLE {table} ALTER COLUMN embedding TYPE VECTOR({EMBED_DIM})")
    conn.commit()


def fill(conn, table, limit=None):
    """Embed every row in `table` that has no vector yet. Returns how many were written."""
    cols = "id, headline, summary" if table == "collected_items" else "id, title, body"
    done = 0
    while True:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {cols} FROM {table} WHERE embedding IS NULL "
                        f"ORDER BY id DESC LIMIT {BATCH}")
            rows = cur.fetchall()
        if not rows:
            break
        vecs = encode([text_of(r[1], r[2]) for r in rows])
        with conn.cursor() as cur:
            for (rid, _h, _s), v in zip(rows, vecs):
                cur.execute(f"UPDATE {table} SET embedding = %s WHERE id = %s", (_vec(v), rid))
        conn.commit()
        done += len(rows)
        print(f"  {table}: {done} embedded", flush=True)
        if limit and done >= limit:
            break
    return done


def sweep(dsn, limit=None):
    import psycopg
    with psycopg.connect(dsn) as conn:
        migrate(conn)
        total = sum(fill(conn, t, limit) for t in ("collected_items", "content_posts"))
    return total


def main():
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise SystemExit("DATABASE_URL is not set")
    once = "--once" in sys.argv
    print(f"embed: {MODEL_NAME} ({EMBED_DIM}-dim), {'one pass' if once else f'every {INTERVAL}s'}",
          flush=True)
    while True:
        try:
            n = sweep(dsn)
            print(f"embed: {n} new vector(s)", flush=True)
        except SystemExit:
            raise
        except Exception as e:
            print(f"embed: sweep failed: {e}", flush=True)
        if once:
            return
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
