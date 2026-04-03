"""Quick hybrid retrieval test for ircookie."""
import asyncio, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from INAGENT.utils.env_utils import load_inagent_env
load_inagent_env()

from INAGENT.web.deps import get_unified_rag

async def _run():
    rag = get_unified_rag()
    gr = rag.graphrag_retriever

    q = "ircookie"
    print(f"Query: {q}")
    print("=" * 70)

    # GraphRAG
    print("\n[GraphRAG Local Search]")
    g_results = await gr.local_context_build(q, top_k=5)
    if g_results:
        for i, r in enumerate(g_results):
            title = r.title if hasattr(r, 'title') else "?"
            desc = str(r.description if hasattr(r, 'description') else "")[:150].replace("\n", " ")
            score = r.score if hasattr(r, 'score') else "?"
            rtype = r.type if hasattr(r, 'type') else "?"
            print(f"  #{i+1} [{rtype}] {title} (score={score})")
            print(f"       {desc}")
    else:
        print("  (no results)")

    # Vector (Qdrant) only
    print("\n[Vector Search (Qdrant) - cli/reference only]")
    ctx, _, _ = rag.retrieve(q, category_whitelist=["cli/reference"], top_k_final=5)
    if ctx:
        chunks = [c.strip() for c in ctx.split("\n\n") if c.strip()]
        for i, chunk in enumerate(chunks[:5]):
            preview = chunk[:200].replace("\n", " ")
            print(f"  #{i+1} {preview}")
    else:
        print("  (no results)")

    rag.hybrid_retriever.vr.storage.close_client()

asyncio.run(_run())

asyncio.run(_run())
