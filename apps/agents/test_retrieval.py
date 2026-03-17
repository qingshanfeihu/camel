import json
import os

from dotenv import load_dotenv

from camel.embeddings import OpenAIEmbedding
from camel.retrievers import VectorRetriever
from camel.storages import QdrantStorage, VectorRecord
from camel.types import EmbeddingModelType

# Load environment variables
load_dotenv(r"C:\SynologyDrive\INFOAGEN\INAGENT\.env")

def main():
    # 1. Configuration
    openai_api_key = os.environ.get("OPENAI_API_KEY")
    openai_api_base = os.environ.get("OPENAI_API_BASE_URL", "https://api.siliconflow.cn/v1")
    
    if not openai_api_key:
        print("Error: OPENAI_API_KEY not found in environment variables.")
        return

    print("Initializing SiliconFlow Embedding Model (BAAI/bge-m3)...")
    try:
        embedding_model = OpenAIEmbedding(
            model_type=EmbeddingModelType.SILICONFLOW_BGE_M3,
            url=openai_api_base,
            api_key=openai_api_key
        )
    except Exception as e:
        print(f"Failed to initialize embedding model: {e}")
        return

    # 2. Vector Storage
    print("Connecting to Vector Storage...")
    storage_path = r"C:\SynologyDrive\INFOAGEN\INAGENT\local_data\qdrant_infosec"
    collection_name = "cli_docs_infosec"
    
    vector_storage = QdrantStorage(
        vector_dim=embedding_model.get_output_dim(),
        collection_name=collection_name,
        path=storage_path
    )

    # 3. Check Index
    # QdrantStorage doesn't have a direct 'count' method exposed in CAMEL's wrapper in all versions,
    # but let's try to query or check status. 
    # Actually, if we just try to retrieve, we'll see if we get results.
    # But better to ensure we have data.
    
    # Let's try to check client count directly if accessible, or just proceed to index if we think it's needed.
    # To be safe, let's just try to load the JSON and index if we suspect it's empty.
    # Or, we can query for *something*.
    
    # Let's assume we need to index if we haven't run the full agent yet.
    # But re-indexing might duplicate.
    # Let's check if the file exists and just try to query first?
    # The user asked to "test retrieval".
    
    # I'll implement a check: query "system". If 0 results, index.
    
    retriever = VectorRetriever(embedding_model=embedding_model, storage=vector_storage)
    
    test_query = "如何定义⼀个HTTP类型的后台服务"
    print(f"\nTesting Retrieval for query: '{test_query}'")
    
    try:
        results = retriever.query(test_query, top_k=3)
        if not results:
            print("No results found. Index might be empty. Attempting to load data...")
            index_data(vector_storage, embedding_model)
            results = retriever.query(test_query, top_k=3)
            
        print("\n--- Retrieval Results ---")
        if isinstance(results, list):
            for i, res in enumerate(results):
                content = res.get('text') if isinstance(res, dict) else str(res)
                score = res.get('score', 'N/A') if isinstance(res, dict) else 'N/A'
                print(f"\n[Result {i+1}] (Score: {score})")
                print(content[:500] + "..." if len(content) > 500 else content)
        else:
            print(results)
            
    except Exception as e:
        print(f"Error during retrieval: {e}")
        # Try indexing if error was due to empty collection?
        # Qdrant might raise error if collection doesn't exist.
        print("Attempting to create index and load data...")
        try:
            index_data(vector_storage, embedding_model)
            results = retriever.query(test_query, top_k=3)
            print("\n--- Retrieval Results (After Indexing) ---")
            if isinstance(results, list):
                for i, res in enumerate(results):
                    content = res.get('text') if isinstance(res, dict) else str(res)
                    print(f"\n[Result {i+1}]")
                    print(content[:500] + "..." if len(content) > 500 else content)
        except Exception as index_e:
            print(f"Indexing failed: {index_e}")

def index_data(vector_storage, embedding_model):
    json_path = r"C:\SynologyDrive\INFOAGEN\INAGENT\doc_local\cli\hybrid_auto\cli_content_list.json"
    if not os.path.exists(json_path):
        print(f"Error: Data file not found at {json_path}")
        return

    print(f"Loading data from {json_path}...")
    with open(json_path, 'r', encoding='utf-8') as f:
        content_list = json.load(f)
    
    texts = []
    metadatas = []
    
    for block in content_list:
        text = block.get("text", "").strip()
        if text and len(text) >= 10:
            texts.append(text)
            metadatas.append({
                "type": block.get("type"),
                "bbox": str(block.get("bbox")),
                "page_idx": block.get("page_idx"),
                "text_level": block.get("text_level")
            })
            
    if texts:
        print(f"Embedding and indexing {len(texts)} chunks...")
        
        # Batch processing for SiliconFlow API (limit 64)
        batch_size = 50 
        all_embeddings = []
        
        for i in range(0, len(texts), batch_size):
            batch_texts = texts[i:i + batch_size]
            print(f"Processing batch {i // batch_size + 1}/{(len(texts) + batch_size - 1) // batch_size} ({len(batch_texts)} items)...")
            try:
                batch_embeddings = embedding_model.embed_list(batch_texts)
                all_embeddings.extend(batch_embeddings)
            except Exception as e:
                print(f"Error embedding batch {i}: {e}")
                # Optional: break or continue? If we continue, indices won't match.
                # We should probably stop or handle carefully. 
                # For now, let's just re-raise to stop invalid indexing.
                raise e

        records = []
        for i, (text, embed, meta) in enumerate(zip(texts, all_embeddings, metadatas)):
            records.append(VectorRecord(vector=embed, payload={"text": text, **meta}))
        
        # Add to storage in batches too, just in case (though Qdrant local usually handles it, but safer)
        # Actually QdrantStorage.add handles list, let's just pass all if memory allows.
        # 9704 records is fine for local Qdrant.
        vector_storage.add(records)
        print("Indexing completed.")
    else:
        print("No valid text to index.")

if __name__ == "__main__":
    main()
