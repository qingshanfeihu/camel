#!/usr/bin/env python
"""One-shot script to run RAG indexing with proper cleanup."""
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
os.chdir(os.path.join(os.path.dirname(__file__), '..'))

import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

from INAGENT.workflow_config_generator import initialize_rag_system

try:
    retriever, reranker, graphrag = initialize_rag_system()
    # Explicitly close the Qdrant client to release the file lock
    if hasattr(retriever, 'vr') and hasattr(retriever.vr, 'storage'):
        storage = retriever.vr.storage
        if hasattr(storage, '_client'):
            storage._client.close()
            logging.info("Qdrant client closed successfully")

    # Verify point count
    from qdrant_client import QdrantClient
    qdrant_path = os.path.join(
        os.path.expanduser('~'), 'AppData', 'Local', 'INAGENT', 'vector_store', 'qdrant'
    )
    client = QdrantClient(path=qdrant_path)
    for col in client.get_collections().collections:
        info = client.get_collection(col.name)
        logging.info(f"Collection '{col.name}': {info.points_count} points")
    client.close()

    print("SUCCESS")
except Exception as e:
    logging.error(f"Failed: {e}", exc_info=True)
    print("FAILED")
    sys.exit(1)
