import asyncio
import json
import os
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../"))
)

from INAGENT.utils.env_utils import load_inagent_env

# Load environment variables from INAGENT/.env only
load_inagent_env()

from camel.agents import ChatAgent
from camel.embeddings import BaseEmbedding, OpenAICompatibleEmbedding
from camel.loaders.local_mineru_reader import LocalMinerUReader
from camel.messages import BaseMessage
from camel.models import ModelFactory
from camel.retrievers import SiliconFlowRerankRetriever, VectorRetriever
from camel.storages import QdrantStorage, VectorRecord
from camel.types import ModelPlatformType, ModelType


def _is_auth_error(exc: Exception) -> bool:
    """Best-effort detection of auth/invalid-key failures."""
    try:
        from openai import AuthenticationError  # type: ignore

        if isinstance(exc, AuthenticationError):
            return True
    except Exception:
        pass

    message = str(exc).lower()
    return (
        "401" in message
        or "unauthorized" in message
        or "authentication" in message
        or ("api key" in message and "invalid" in message)
    )


def _maybe_align_openai_compatible_env(api_key: Optional[str], api_base: str) -> None:
    """Align env vars used across CAMEL OpenAI-compatible integrations.

    This demo runs on SiliconFlow but uses OpenAI-compatible adapters.
    """
    if not api_key:
        return

    os.environ.setdefault("OPENAI_API_KEY", api_key)
    os.environ.setdefault("OPENAI_API_BASE_URL", api_base)
    os.environ.setdefault("OPENAI_COMPATIBILITY_API_KEY", api_key)
    os.environ.setdefault("OPENAI_COMPATIBILITY_API_BASE_URL", api_base)


# Dummy Embedding for demonstration if no real model is available
class DummyEmbedding(BaseEmbedding):
    def embed_list(self, objs: List[Any], **kwargs: Any) -> List[List[float]]:
        # Return random vectors of dimension 1536
        _ = kwargs
        return [[0.1] * 1536 for _ in objs]
    
    def get_output_dim(self) -> int:
        return 1536


def _get_encoding():
    from camel.utils.token_counting import get_model_encoding

    return get_model_encoding("text-embedding-3-small")


def _count_tokens(text: str) -> int:
    encoding = _get_encoding()
    return len(encoding.encode(text, disallowed_special=()))


def _is_header_block(block: Dict[str, Any]) -> bool:
    if block.get("type") != "text":
        return False
    text_level = block.get("text_level")
    if isinstance(text_level, int) and text_level > 0:
        return True
    text = (block.get("text") or "").strip()
    if not text:
        return False
    if len(text) <= 60 and any(
        text.startswith(p) for p in ("第", "Chapter", "CHAPTER")
    ):
        return True
    if len(text) <= 60 and text[:1].isdigit() and "." in text[:8]:
        return True
    return False


def _merge_texts(prev: str, nxt: str) -> str:
    if not prev:
        return nxt
    if not nxt:
        return prev
    if len(prev) <= 80 and len(nxt) <= 80:
        return f"{prev} {nxt}"
    return f"{prev}\n{nxt}"


@dataclass
class MinerUChunk:
    text: str
    metadata: Dict[str, Any]


class MinerUChunker:
    def __init__(
        self,
        token_limit: int = 800,
        min_chunk_tokens: int = 120,
    ) -> None:
        self.token_limit = token_limit
        self.min_chunk_tokens = min_chunk_tokens

    def chunk(self, blocks: Sequence[Dict[str, Any]]) -> List[MinerUChunk]:
        chunks: List[MinerUChunk] = []
        current_header: Optional[str] = None
        current_header_level: Optional[int] = None
        state: Dict[str, Any] = {
            "text": "",
            "tokens": 0,
            "page_start": None,
            "page_end": None,
            "bbox_list": [],
        }

        def flush() -> None:
            if not str(state["text"]).strip():
                state["text"] = ""
                state["tokens"] = 0
                state["page_start"] = None
                state["page_end"] = None
                state["bbox_list"] = []
                return
            if int(state["tokens"]) < self.min_chunk_tokens and chunks:
                last = chunks[-1]
                last.text = _merge_texts(last.text, state["text"])
                last.metadata["page_end"] = max(
                    last.metadata.get("page_end", -1),
                    state["page_end"] or -1,
                )
                last.metadata["bbox_list"].extend(state["bbox_list"])
            else:
                chunks.append(
                    MinerUChunk(
                        text=state["text"],
                        metadata={
                            "section_title": current_header,
                            "section_level": current_header_level,
                            "page_start": state["page_start"],
                            "page_end": state["page_end"],
                            "bbox_list": state["bbox_list"],
                        },
                    )
                )
            state["text"] = ""
            state["tokens"] = 0
            state["page_start"] = None
            state["page_end"] = None
            state["bbox_list"] = []

        for block in blocks:
            if (block.get("type") or "") != "text":
                continue
            text = (block.get("text") or "").strip()
            if not text:
                continue

            is_header = _is_header_block(block)
            page_idx = block.get("page_idx")
            bbox = block.get("bbox")

            if isinstance(page_idx, int):
                if state["page_start"] is None:
                    state["page_start"] = page_idx
                state["page_end"] = page_idx
            if bbox is not None:
                state["bbox_list"].append(
                    {
                        "page_idx": page_idx,
                        "bbox": bbox,
                        "type": block.get("type"),
                    }
                )

            if is_header:
                flush()
                current_header = text
                text_level = block.get("text_level")
                current_header_level = (
                    int(text_level) if isinstance(text_level, int) else None
                )
                state["text"] = text
                state["tokens"] = _count_tokens(state["text"])
                # Preserve structural metadata for header chunks
                state["page_start"] = page_idx if isinstance(page_idx, int) else None
                state["page_end"] = page_idx if isinstance(page_idx, int) else None
                state["bbox_list"] = []
                if bbox is not None:
                    state["bbox_list"].append(
                        {
                            "page_idx": page_idx,
                            "bbox": bbox,
                            "type": block.get("type"),
                        }
                    )
                continue

            candidate_text = _merge_texts(state["text"], text)
            candidate_tokens = _count_tokens(candidate_text)
            if (
                candidate_tokens > self.token_limit
                and str(state["text"]).strip()
            ):
                flush()
                if current_header:
                    state["text"] = current_header
                    state["tokens"] = _count_tokens(state["text"])

            state["text"] = _merge_texts(state["text"], text)
            state["tokens"] = _count_tokens(state["text"])

        flush()
        return chunks

async def main():
    # 1. Configuration
    # Use a dummy key if not set, assuming user might use local LLM
    # ========== API Configuration ==========
    # Use SiliconFlow API only
    api_key = os.environ.get("SILICONFLOW_API_KEY")
    if api_key and is_placeholder_value(api_key):
        api_key = None

    # Default to SiliconFlow endpoint
    api_base = os.environ.get(
        "SILICONFLOW_API_BASE_URL", "https://api.siliconflow.cn/v1"
    )

    _maybe_align_openai_compatible_env(api_key, api_base)
    
    if not api_key:
        print(
            "\n" + "=" * 70 + "\n"
            "ERROR: No API key found!\n\n"
            "Please set one of the following environment variables:\n\n"
            "  1. SILICONFLOW_API_KEY (recommended, cost-effective)\n"
            "     Get your key at: https://siliconflow.cn/\n"
            "     Example:\n"
            "       PowerShell: $env:SILICONFLOW_API_KEY='sk-your_key'\n"
            "       Bash:       export SILICONFLOW_API_KEY='sk-your_key'\n\n"
            "See README_mineru_rag_demo.md for detailed setup instructions.\n"
            + "=" * 70 + "\n"
        )
        print("Demo will continue with retrieval only (no LLM generation).\n")
    else:
        print(f"✓ Using API endpoint: {api_base}")
        print(f"✓ API key configured (length: {len(api_key)})\n")

    # ========== Model Configuration (override via env) ==========
    # If you already configured these models elsewhere, set env vars to make
    # this demo pick them up.
    # - SILICONFLOW_MODEL_TYPE / SILICONFLOW_CHAT_MODEL: e.g. "THUDM/GLM-Z1-9B-0414"
    # - SILICONFLOW_RERANK_MODEL: e.g. "netease-youdao/bce-reranker-base_v1"
    chat_model_name = os.environ.get("SILICONFLOW_MODEL_TYPE") or os.environ.get(
        "SILICONFLOW_CHAT_MODEL", "Qwen/Qwen2.5-7B-Instruct"
    )
    rerank_model_name = os.environ.get(
        "SILICONFLOW_RERANK_MODEL", "BAAI/bge-reranker-v2-m3"
    )
    
    print(f"✓ Chat Model: {chat_model_name}")
    print(f"✓ Rerank Model: {rerank_model_name}")

    file_path = r"C:\SynologyDrive\INFOAGEN\source\docs\cli.pdf"
    mineru_cmd = r"C:\SynologyDrive\INFOAGEN\mineru\venv\Scripts\mineru.exe"
    output_dir = r"C:\SynologyDrive\INFOAGEN\mineru\output_cli_pdf"
    
    # 2. Process PDF with MinerU (Local)
    print(f"Processing {file_path} with MinerU...")
    reader = LocalMinerUReader(
        mineru_command=mineru_cmd,
        output_dir=output_dir,
    )
    
    # Submit task (runs mineru CLI)
    try:
        task_id = await reader.submit_task(file_path)
    except Exception as e:
        print(f"Error processing file: {e}")
        # If it fails, maybe it's because it's already processed or some other
        # issue.
        # We can try to proceed if output exists.
        print("Attempting to read existing output...")
        task_id = "cli"  # Hardcoded for this demo if we assume it's cli.pdf

    # Confirm Markdown output exists (MinerU should produce it alongside JSON)
    md_path: Optional[str] = None
    try:
        task_dir = os.path.join(output_dir, task_id, "hybrid_auto")
        candidate_md = os.path.join(task_dir, f"{task_id}.md")
        if os.path.exists(candidate_md):
            md_path = candidate_md
        else:
            # Fallback: search within task directory
            task_root = os.path.join(output_dir, task_id)
            if os.path.exists(task_root):
                for root, _dirs, files in os.walk(task_root):
                    for file in files:
                        if file.lower().endswith(".md"):
                            md_path = os.path.join(root, file)
                            break
                    if md_path:
                        break
    except Exception:
        md_path = None

    if md_path:
        print(f"✓ MinerU Markdown generated: {md_path}")
    else:
        print(
            "[WARN] MinerU Markdown file not found in output_dir. "
            "JSON parsing may still work, but PDF→MD generation could have failed."
        )

    try:
        json_output = await reader.get_task_output(task_id)
    except Exception as e:
        print(f"Failed to read MinerU output from output_dir: {e}")
        json_output = ""
    if not json_output:
        json_path = (
            r"C:\SynologyDrive\INFOAGEN\INAGENT\doc_local\cli\hybrid_auto\cli_content_list.json"
        )
        if os.path.exists(json_path):
            with open(json_path, "r", encoding="utf-8") as f:
                json_output = f.read()
        else:
            print("Failed to get MinerU output.")
            return

    print("Successfully retrieved MinerU structured output.")
    
    # 3. Prepare Embedding/Vector Store (optional)
    enable_vector = bool(api_key)
    embedding_model: BaseEmbedding
    vector_storage: Optional[QdrantStorage] = None

    if api_key:
        print("Using SiliconFlow Embedding Model (BAAI/bge-m3).")
        try:
            embedding_model_name = (
                os.getenv("SILICONFLOW_EMBEDDING_MODEL") or "BAAI/bge-m3"
            )
            embedding_model = OpenAICompatibleEmbedding(
                model_name=embedding_model_name,
                url=api_base,
                api_key=api_key,
            )
            # Preflight: fail fast on invalid key
            embedding_model.embed_list(["ping"])  # type: ignore[arg-type]
            vector_storage = QdrantStorage(
                vector_dim=embedding_model.get_output_dim()
            )
        except Exception as e:
            enable_vector = False
            print(
                "\n[WARN] Embedding API not available; falling back to BM25-only retrieval."
            )
            if _is_auth_error(e):
                print(
                    "[WARN] Authentication failed (likely invalid API key). "
                    "Set a valid SILICONFLOW_API_KEY to enable embeddings/rerank/LLM."
                )
            else:
                print(f"[WARN] Embedding initialization failed: {e}")
            embedding_model = DummyEmbedding()
    else:
        print("Falling back to DummyEmbedding for demonstration purposes.")
        embedding_model = DummyEmbedding()
        vector_storage = QdrantStorage(vector_dim=embedding_model.get_output_dim())

    # 4. Ingest Data into Vector Storage (only if enabled)
    content_list = json.loads(json_output)

    chunker = MinerUChunker(token_limit=800, min_chunk_tokens=120)
    chunks = chunker.chunk(content_list)
    if not chunks:
        print("No text content found to index.")
        return
    
    texts = [c.text for c in chunks]
    metadatas = [c.metadata for c in chunks]

    if enable_vector and vector_storage is not None:
        print("Ingesting data into vector storage...")
        BATCH_SIZE = 50
        try:
            for i in range(0, len(texts), BATCH_SIZE):
                batch_texts = texts[i : i + BATCH_SIZE]
                batch_metadatas = metadatas[i : i + BATCH_SIZE]

                embeddings = embedding_model.embed_list(batch_texts)
                records: List[VectorRecord] = []
                for text, meta, vec in zip(
                    batch_texts, batch_metadatas, embeddings
                ):
                    payload = {
                        "content path": file_path,
                        "md_path": md_path,
                        "metadata": meta,
                        "text": text,
                    }
                    records.append(VectorRecord(vector=vec, payload=payload))

                vector_storage.add(records)
                print(f"Indexed {len(records)} chunks.")
        except Exception as e:
            enable_vector = False
            vector_storage = None
            print(
                "\n[WARN] Vector indexing failed; falling back to BM25-only retrieval."
            )
            if _is_auth_error(e):
                print(
                    "[WARN] Authentication failed during embedding calls. "
                    "Your API key/base URL is likely incorrect."
                )
            else:
                print(f"[WARN] Vector indexing error: {e}")

    # 6. Run RAG Query
    query = "How to configure system numa?"
    print(f"\nQuerying: '{query}'")
    
    retrieved_info: List[Dict[str, Any]] = []
    if enable_vector and vector_storage is not None:
        retriever = VectorRetriever(
            embedding_model=embedding_model, storage=vector_storage
        )
        try:
            retrieved_info = retriever.query(query, top_k=50)
        except Exception as e:
            enable_vector = False
            retrieved_info = []
            print(
                "[WARN] Vector retrieval failed; continuing with BM25-only retrieval."
            )
            print(f"[WARN] Vector retrieval error: {e}")
    bm25_results: List[Dict[str, Any]] = []
    try:
        from rank_bm25 import BM25Okapi  # type: ignore[import-not-found]

        tokenized_corpus = [t.split(" ") for t in texts]
        bm25 = BM25Okapi(tokenized_corpus)
        scores = bm25.get_scores(query.split(" "))
        scored_idxs = sorted(
            enumerate(scores), key=lambda x: x[1], reverse=True
        )[:50]
        for idx, score in scored_idxs:
            bm25_results.append(
                {
                    "similarity score": float(score),
                    "content path": file_path,
                    "metadata": metadatas[idx],
                    "text": texts[idx],
                }
            )
    except Exception:
        bm25_results = []

    def rrf_merge(
        vector_results: List[Dict[str, Any]],
        keyword_results: List[Dict[str, Any]],
        top_k: int = 50,
        vector_weight: float = 0.8,
        keyword_weight: float = 0.2,
        rank_smoothing_factor: int = 60,
    ) -> List[Dict[str, Any]]:
        id_to_info: Dict[str, Dict[str, Any]] = {}
        vector_ordered = sorted(
            [r for r in vector_results if "text" in r],
            key=lambda x: float(x.get("similarity score", 0.0)),
            reverse=True,
        )
        keyword_ordered = sorted(
            [r for r in keyword_results if "text" in r],
            key=lambda x: float(x.get("similarity score", 0.0)),
            reverse=True,
        )

        for i, info in enumerate(vector_ordered):
            key = info.get("text", "")
            if key not in id_to_info:
                id_to_info[key] = dict(info)
                id_to_info[key]["_v_rank"] = i
            else:
                id_to_info[key]["_v_rank"] = min(
                    id_to_info[key].get("_v_rank", i),
                    i,
                )

        for i, info in enumerate(keyword_ordered):
            key = info.get("text", "")
            if key not in id_to_info:
                id_to_info[key] = dict(info)
                id_to_info[key]["_k_rank"] = i
            else:
                id_to_info[key]["_k_rank"] = min(
                    id_to_info[key].get("_k_rank", i),
                    i,
                )

        merged: List[Dict[str, Any]] = []
        for info in id_to_info.values():
            v_rank = info.get("_v_rank")
            k_rank = info.get("_k_rank")
            score = 0.0
            if v_rank is not None:
                score += vector_weight / (
                    rank_smoothing_factor + float(v_rank)
                )
            if k_rank is not None:
                score += keyword_weight / (
                    rank_smoothing_factor + float(k_rank)
                )
            info["rrf_score"] = score
            info.pop("_v_rank", None)
            info.pop("_k_rank", None)
            merged.append(info)

        merged.sort(key=lambda x: float(x.get("rrf_score", 0.0)), reverse=True)
        return merged[:top_k]

    vector_weight = 0.8 if retrieved_info else 0.0
    keyword_weight = 0.2 if retrieved_info else 1.0
    hybrid_top50 = rrf_merge(
        retrieved_info,
        bm25_results,
        top_k=50,
        vector_weight=vector_weight,
        keyword_weight=keyword_weight,
    )
    reranked_top5 = hybrid_top50
    if api_key:
        try:
            reranker = SiliconFlowRerankRetriever(
                model_name=rerank_model_name,
                api_key=api_key,
                url=api_base,
            )
            reranked_top5 = reranker.query(
                query=query, retrieved_result=hybrid_top50, top_k=5
            )
        except Exception as e:
            print(
                "[WARN] Rerank failed; using non-reranked top-5 contexts."
            )
            if _is_auth_error(e):
                print(
                    "[WARN] Authentication failed for rerank. "
                    "Check SILICONFLOW_API_KEY and endpoint."
                )
            else:
                print(f"[WARN] Rerank error: {e}")
            reranked_top5 = hybrid_top50[:5]
    else:
        reranked_top5 = hybrid_top50[:5]

    def format_context_item(item: Dict[str, Any]) -> str:
        meta = item.get("metadata") or {}
        if "page_start" in meta or "page_end" in meta:
            p0 = meta.get("page_start")
            p1 = meta.get("page_end")
            if p0 is not None and p1 is not None and p0 != p1:
                page_str = f"Page {p0 + 1}-{p1 + 1}"
            elif p0 is not None:
                page_str = f"Page {p0 + 1}"
            else:
                page_str = ""
        else:
            page_str = ""
        source = os.path.basename(item.get("content path") or file_path)
        cite = f"({source}, {page_str})" if page_str else f"({source})"
        return f"- {item.get('text','').strip()} {cite}"

    context_str = "\n".join([format_context_item(x) for x in reranked_top5])
    print(f"\nRetrieved Context:\n{context_str}\n")

    # Setup ChatAgent with OpenAI-compatible model (SiliconFlow)
    sys_msg = BaseMessage.make_assistant_message(
        role_name="Assistant",
        content=(
            "You are a helpful assistant. Answer the user's question using "
            "the provided context."
        ),
    )
    
    if api_key:
        try:
            # Create SiliconFlow model via CAMEL
            model = ModelFactory.create(
                model_platform=ModelPlatformType.SILICONFLOW,
                model_type=chat_model_name,
                api_key=api_key,
                url=api_base,
                model_config_dict={"temperature": 0.2, "max_tokens": 2000},
            )
            
            agent = ChatAgent(system_message=sys_msg, model=model)
            
            user_msg_content = f"Context:\n{context_str}\n\nQuestion: {query}"
            user_msg = BaseMessage.make_user_message(
                role_name="User", content=user_msg_content
            )
            
            print("\nAsking Agent...")
            response = agent.step(user_msg)
            print("\nAgent Response:")
            print(response.msg.content)
        except Exception as e:
            print(f"\nAgent step failed: {e}")
            print("Context retrieved successfully:")
            print(context_str)
    else:
        print(
            "\nSkipping agent response generation (no API key provided).\n"
            "Context retrieved successfully:"
        )
        print(context_str)

if __name__ == "__main__":
    asyncio.run(main())
