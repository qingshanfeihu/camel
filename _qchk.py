import json, pathlib, warnings
warnings.filterwarnings("ignore")
meta_path = pathlib.Path.home() / "AppData/Local/INAGENT/vector_store/qdrant/rag_meta.json"
print(f"exists: {meta_path.exists()}")
if meta_path.exists():
    print(meta_path.read_text(encoding="utf-8"))
from qdrant_client import QdrantClient
c = QdrantClient(path=str(meta_path.parent))
i = c.get_collection("workflow_rag")
print(f"points: {i.points_count}")
print(f"status: {i.status}")
c.close()
