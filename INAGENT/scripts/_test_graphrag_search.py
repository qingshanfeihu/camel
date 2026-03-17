"""Quick test: verify GraphRAG local_search no longer raises KeyError: 'attributes'."""
import asyncio
from pathlib import Path
from INAGENT.rag.graphrag_integration import GraphRAGRetriever

WORKSPACE = Path(__file__).resolve().parent.parent / "graphrag_index"


async def main():
    r = GraphRAGRetriever(workspace_dir=WORKSPACE)
    results = await r.local_search("内容健康检查 health request health response", top_k=3)
    print(f"local_search 返回 {len(results)} 条结果")
    for i, doc in enumerate(results):
        snippet = doc.text[:150] if hasattr(doc, "text") else str(doc)[:150]
        print(f"  [{i+1}] {snippet}...")


asyncio.run(main())
