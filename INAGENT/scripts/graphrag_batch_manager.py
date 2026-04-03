#!/usr/bin/env python3
# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
GraphRAG Batch Processing Manager

This script provides a unified interface for GraphRAG batch processing,
integrated with the INAGENT pipeline's database management actions.

Supports automatic chunking for large datasets:
- Max 50,000 requests per batch (Zhipu limit)
- Max 100MB per batch file (Zhipu limit)
- Recommended: 2000-5000 requests per batch for better progress tracking

Usage:
    # Submit batch extraction (from pipeline)
    python graphrag_batch_manager.py --action submit
    
    # Check status
    python graphrag_batch_manager.py --action status
    
    # Download and parse results
    python graphrag_batch_manager.py --action download
    
    # Full batch build (submit + wait + download + merge)
    python graphrag_batch_manager.py --action build
"""
import argparse
import asyncio
import hashlib
import json
import logging
import math
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

# Add paths
SCRIPT_DIR = Path(__file__).parent.resolve()
INAGENT_DIR = SCRIPT_DIR.parent
PROJECT_ROOT = INAGENT_DIR.parent

sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(INAGENT_DIR))
sys.path.insert(0, str(SCRIPT_DIR))

try:
    from INAGENT.utils import env_utils
except ImportError:
    # Fallback: try direct import
    try:
        import env_utils
    except ImportError:
        # Create a minimal env_utils placeholder
        class env_utils:
            @staticmethod
            def load_inagent_env():
                from dotenv import load_dotenv
                env_file = INAGENT_DIR / ".env"
                if env_file.exists():
                    load_dotenv(env_file)
                else:
                    # Try project root
                    env_file = PROJECT_ROOT / ".env"
                    if env_file.exists():
                        load_dotenv(env_file)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


# ========= Configuration =========

WORKSPACE_DIR = INAGENT_DIR / "graphrag_index"
STATE_FILE = WORKSPACE_DIR / "batch_state.json"
OUTPUT_DIR = WORKSPACE_DIR / "output" / "batch_results"
DOCUMENTS_FILE = WORKSPACE_DIR / "input" / "documents.json"

# Zhipu Batch API Limits
MAX_REQUESTS_PER_BATCH = 50000  # Zhipu limit
MAX_FILE_SIZE_MB = 100  # Zhipu limit
RECOMMENDED_BATCH_SIZE = 3000  # Recommended for better progress tracking

# Default Model
# glm-4-flashx-250414: Higher priority & concurrency, better performance
# glm-4-flash: FREE but slower
DEFAULT_MODEL = "glm-4-flashx-250414"

# Pre-load environment variables
env_utils.load_inagent_env()


def get_zhipu_api_key() -> str:
    """Get Zhipu API key from environment."""
    # Try multiple sources
    key = os.environ.get("ZHIPU_API_KEY") or os.environ.get("ZHIPUAI_API_KEY")
    
    if not key:
        # Fallback to hardcoded key (for development only)
        key = "2df699c2eea242a28267ff879b4c90ee.Iw4fox3XG6CdtgdS"
        logger.warning("Using fallback API key. Consider setting ZHIPU_API_KEY in .env")
    
    return key


def compute_batch_signature(
    documents: List[Dict[str, Any]],
    prompt_template: str,
    model: str,
    batch_size: int,
    limit: Optional[int],
) -> str:
    r"""Compute a deterministic signature for the batch input.

    This signature is used to skip regeneration when inputs are unchanged.
    """
    signature_payload = {
        "model": model,
        "batch_size": batch_size,
        "limit": limit,
        "prompt_template": prompt_template,
        "documents": documents,
    }
    payload_text = json.dumps(
        signature_payload, ensure_ascii=False, sort_keys=True
    )
    return hashlib.sha256(payload_text.encode("utf-8")).hexdigest()


def load_state() -> Optional[Dict[str, Any]]:
    """Load batch state from file."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"Failed to load state: {e}")
    return None


def save_state(state: Dict[str, Any]) -> None:
    """Save batch state to file."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info(f"State saved to {STATE_FILE}")


def get_batch_client():
    """Get Zhipu batch client."""
    from graphrag.graphrag.language_model.providers.zhipu_batch import ZhipuBatchClient
    return ZhipuBatchClient(api_key=get_zhipu_api_key())


def action_status() -> int:
    """Check batch status (supports multiple batches)."""
    state = load_state()
    
    print("\n" + "=" * 60)
    print("📊 GraphRAG Batch Status")
    print("=" * 60)
    
    if not state:
        print("\n⚠️ No batch job found.")
        print("Run with --action submit to start a new batch.")
        return 0
    
    # Check if multi-batch or single-batch state
    batches = state.get("batches", [])
    
    if not batches:
        # Legacy single-batch format
        batch_id = state.get("batch_id")
        if batch_id:
            batches = [{
                "batch_id": batch_id,
                "document_count": state.get("document_count", 0),
                "start_idx": 0,
            }]
        else:
            print("\n⚠️ No batch ID in state.")
            return 0
    
    total_docs = state.get("total_documents", sum(b.get("document_count", 0) for b in batches))
    print(f"\nTotal Documents: {total_docs}")
    print(f"Number of Batches: {len(batches)}")
    print(f"Created: {state.get('created_at', 'N/A')}")
    print(f"Model: {state.get('model', 'glm-4-flash')}")
    
    # Get status for each batch
    client = get_batch_client()
    all_completed = 0
    all_failed = 0
    all_total = 0
    all_done = True
    
    print("\n" + "-" * 40)
    print("Batch Details:")
    print("-" * 40)
    
    for i, batch_info in enumerate(batches):
        batch_id = batch_info.get("batch_id")
        doc_count = batch_info.get("document_count", 0)
        
        try:
            status, info = client.get_batch_status(batch_id)
            
            # Handle SDK object or dict
            if isinstance(info, dict):
                counts = info.get("request_counts", {})
            else:
                counts = getattr(info, "request_counts", None) or {}
            
            if isinstance(counts, dict):
                total = counts.get("total", 0)
                completed = counts.get("completed", 0)
                failed = counts.get("failed", 0)
            else:
                total = getattr(counts, "total", 0) or 0
                completed = getattr(counts, "completed", 0) or 0
                failed = getattr(counts, "failed", 0) or 0
            
            all_completed += completed
            all_failed += failed
            all_total += total
            
            progress = int((completed + failed) / total * 100) if total > 0 else 0
            
            status_emoji = {
                "validating": "🔍",
                "in_progress": "⏳",
                "finalizing": "🔄",
                "completed": "✅",
                "failed": "❌",
                "cancelled": "🚫",
                "cancelling": "🚫",
            }.get(status.value, "❓")
            
            print(f"  [{i+1}/{len(batches)}] {status_emoji} {status.value} | {completed}/{total} ({progress}%) | {batch_id[:20]}...")
            
            if status.value not in ["completed", "failed", "cancelled", "expired"]:
                all_done = False
                
        except Exception as e:
            print(f"  [{i+1}/{len(batches)}] ❓ Error: {e}")
            all_done = False
    
    # Overall progress
    overall_progress = int((all_completed + all_failed) / all_total * 100) if all_total > 0 else 0
    
    print("\n" + "-" * 40)
    print(f"Overall Progress: {all_completed}/{all_total} ({overall_progress}%)")
    print(f"  Completed: {all_completed}")
    print(f"  Failed: {all_failed}")
    print("-" * 40)
    
    if all_done:
        print("\n✅ All batches completed!")
        print("Run with --action download to get results.")
    else:
        print("\n⏳ Some batches still in progress...")
    
    # Update state
    state["status"] = "completed" if all_done else "in_progress"
    state["last_check"] = datetime.now().isoformat()
    save_state(state)
    
    return 0


def action_submit(limit: Optional[int] = None, batch_size: int = RECOMMENDED_BATCH_SIZE, model: str = DEFAULT_MODEL) -> int:
    """Submit new batch job(s) with automatic chunking for large datasets.
    
    Args:
        limit: Limit number of documents (for testing)
        batch_size: Number of requests per batch (default: 3000)
        model: Model to use (default: glm-4-flashx-250414)
    """
    from graphrag.graphrag.language_model.providers.zhipu_batch import (
        ZhipuBatchClient, BatchRequest
    )
    
    print("\n" + "=" * 60)
    print("🚀 Submitting GraphRAG Batch Job(s)")
    print("=" * 60)
    
    # Check if documents exist
    if not DOCUMENTS_FILE.exists():
        print(f"\n❌ Documents file not found: {DOCUMENTS_FILE}")
        print("Run GraphRAG workspace initialization first:")
        print("  python INAGENT/scripts/init_graphrag.py --init")
        return 1
    
    # Load documents
    print(f"\n📂 Loading documents from {DOCUMENTS_FILE}")
    documents = json.loads(DOCUMENTS_FILE.read_text(encoding="utf-8"))
    total_docs = len(documents)
    
    if limit:
        documents = documents[:limit]
        print(f"   Limited to {len(documents)} of {total_docs} documents")
    else:
        print(f"   Loaded {total_docs} documents")
    
    # Load prompt template
    prompt_file = WORKSPACE_DIR / "prompts" / "extract_graph.txt"
    if prompt_file.exists():
        prompt_template = prompt_file.read_text(encoding="utf-8")
        print(f"📋 Loaded prompt template ({len(prompt_template)} chars)")
    else:
        print(f"⚠️ Using default prompt template")
        prompt_template = _get_default_prompt()

    signature = compute_batch_signature(
        documents=documents,
        prompt_template=prompt_template,
        model=model,
        batch_size=batch_size,
        limit=limit,
    )
    state = load_state()
    if state and state.get("signature") == signature:
        if state.get("status") == "completed":
            print("\n✅ Cache hit: input unchanged and last batch completed.")
            print("Skip regeneration and reuse existing results.")
            print("Run with --action download to fetch results.")
            return 0
    
    # Prepare all requests first
    print(f"\n📝 Preparing batch requests...")
    all_requests = []
    
    for i, doc in enumerate(documents):
        doc_id = doc.get("id", f"doc_{i:06d}")
        doc_text = doc.get("text", "") or doc.get("content", "")
        
        if not doc_text.strip():
            continue
        
        # Format prompt
        prompt = prompt_template.replace("{input_text}", doc_text)
        prompt = prompt.replace("{tuple_delimiter}", "<|>")
        prompt = prompt.replace("{record_delimiter}", "##")
        
        # Add completion marker
        if "<|COMPLETE|>" not in prompt:
            prompt += "\n\n# 输出\n\n<|COMPLETE|>\n"
        
        all_requests.append(BatchRequest(
            custom_id=doc_id,
            messages=[{"role": "user", "content": prompt}],
            model=model,
            temperature=0.0,
            max_tokens=2000,
        ))
    
    total_requests = len(all_requests)
    print(f"   Prepared {total_requests} requests")
    
    # Calculate number of batches needed
    num_batches = math.ceil(total_requests / batch_size)
    
    # Validate against Zhipu limits
    if batch_size > MAX_REQUESTS_PER_BATCH:
        batch_size = MAX_REQUESTS_PER_BATCH
        num_batches = math.ceil(total_requests / batch_size)
        print(f"⚠️ Adjusted batch size to {batch_size} (Zhipu limit)")
    
    print(f"\n📦 Splitting into {num_batches} batches ({batch_size} requests each)")
    print(f"   Zhipu limits: max {MAX_REQUESTS_PER_BATCH} requests, {MAX_FILE_SIZE_MB}MB per batch")
    
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    # Submit each batch
    batches_info = []
    
    for batch_idx in range(num_batches):
        start_idx = batch_idx * batch_size
        end_idx = min(start_idx + batch_size, total_requests)
        batch_requests = all_requests[start_idx:end_idx]
        
        print(f"\n" + "-" * 40)
        print(f"Batch {batch_idx + 1}/{num_batches}: docs {start_idx} - {end_idx - 1}")
        print("-" * 40)
        
        # Create client and add requests
        client = ZhipuBatchClient(api_key=get_zhipu_api_key())
        for req in batch_requests:
            client.add_request(req)
        
        # Create JSONL file
        jsonl_file = OUTPUT_DIR / f"batch_input_{timestamp}_part{batch_idx + 1:02d}.jsonl"
        print(f"📄 Creating: {jsonl_file.name}")
        client.create_jsonl_file(str(jsonl_file))
        
        # Check file size
        file_size_mb = jsonl_file.stat().st_size / (1024 * 1024)
        print(f"   File size: {file_size_mb:.2f} MB")
        
        if file_size_mb > MAX_FILE_SIZE_MB:
            print(f"❌ File too large! Max {MAX_FILE_SIZE_MB}MB allowed.")
            print("   Consider reducing batch_size.")
            return 1
        
        # Upload file
        print(f"📤 Uploading...")
        file_id = client.upload_file(str(jsonl_file))
        print(f"   File ID: {file_id}")
        
        # Create batch
        print(f"🎯 Creating batch...")
        batch_id = client.create_batch(file_id)
        print(f"   Batch ID: {batch_id}")
        
        # Get initial status
        status, _ = client.get_batch_status(batch_id)
        print(f"   Status: {status.value}")
        
        batches_info.append({
            "batch_id": batch_id,
            "file_id": file_id,
            "jsonl_file": str(jsonl_file),
            "document_count": len(batch_requests),
            "start_idx": start_idx,
            "end_idx": end_idx,
            "status": status.value,
        })
        
        # Small delay between batch submissions to avoid rate limiting
        if batch_idx < num_batches - 1:
            time.sleep(1)
    
    # Save state
    state = {
        "batches": batches_info,
        "total_documents": total_requests,
        "batch_count": num_batches,
        "batch_size": batch_size,
        "created_at": datetime.now().isoformat(),
        "status": "submitted",
        "model": model,
        "signature": signature,
        "documents_file": str(DOCUMENTS_FILE),
        "prompt_file": str(prompt_file),
        "limit": limit,
    }
    save_state(state)
    
    print("\n" + "=" * 60)
    print("✅ All batches submitted successfully!")
    print("=" * 60)
    print(f"\nTotal Documents: {total_requests}")
    print(f"Number of Batches: {num_batches}")
    print(f"Model: {model}")
    if model == "glm-4-flash":
        print("  (FREE in batch mode!)")
    elif "flashx" in model.lower():
        print("  (Higher priority & concurrency)")
    print(f"\nTo check status:")
    print(f"  python graphrag_batch_manager.py --action status")
    
    return 0


def action_download() -> int:
    """Download and merge results from all batches."""
    from graphrag.graphrag.language_model.providers.zhipu_batch import ZhipuBatchClient
    
    print("\n" + "=" * 60)
    print("📥 Downloading Batch Results")
    print("=" * 60)
    
    state = load_state()
    if not state:
        print("\n❌ No batch job found. Run --action submit first.")
        return 1
    
    # Support both multi-batch and legacy single-batch format
    if "batches" in state:
        batches = state["batches"]
    elif "batch_id" in state:
        # Legacy format
        batches = [{
            "batch_id": state["batch_id"],
            "document_count": state.get("document_count", 0),
        }]
    else:
        print("\n❌ No batch information found in state.")
        return 1
    
    print(f"\nFound {len(batches)} batch(es) to download")
    
    all_results = []
    total_successful = 0
    total_failed = 0
    
    client = ZhipuBatchClient(api_key=get_zhipu_api_key())
    
    for i, batch_info in enumerate(batches):
        batch_id = batch_info["batch_id"]
        doc_count = batch_info.get("document_count", 0)
        
        print(f"\n" + "-" * 40)
        print(f"Batch {i + 1}/{len(batches)}: {batch_id}")
        print(f"Documents: {doc_count}")
        print("-" * 40)
        
        try:
            status, info = client.get_batch_status(batch_id)
            
            if status.value != "completed":
                print(f"⚠️ Batch not completed. Status: {status.value}")
                print("   Skipping this batch...")
                continue
            
            # Get output file ID
            if isinstance(info, dict):
                output_file_id = info.get("output_file_id")
            else:
                output_file_id = getattr(info, "output_file_id", None)
            
            if not output_file_id:
                print("❌ No output file ID found")
                continue
            
            # Download results
            print(f"📥 Downloading results...")
            results = client.download_results(batch_id)
            print(f"   Downloaded {len(results)} results")
            
            for result in results:
                if result.success:
                    total_successful += 1
                    all_results.append({
                        "custom_id": result.custom_id,
                        "success": True,
                        "response": result.response,
                        "batch_id": batch_id,
                    })
                else:
                    total_failed += 1
                    all_results.append({
                        "custom_id": result.custom_id,
                        "success": False,
                        "error": result.error,
                        "batch_id": batch_id,
                    })
            
            print(f"   ✅ Batch {i + 1} done: {len(results)} results")
            
        except Exception as e:
            logger.error(f"Failed to download batch {batch_id}: {e}")
            print(f"❌ Error: {e}")
            continue
    
    if not all_results:
        print("\n❌ No results downloaded from any batch.")
        return 1
    
    # Save merged results
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = OUTPUT_DIR / f"batch_output_{timestamp}.json"
    
    output_file.write_text(
        json.dumps(all_results, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )
    
    print("\n" + "=" * 60)
    print("✅ All results merged and saved!")
    print("=" * 60)
    print(f"\nOutput file: {output_file}")
    print(f"Total results: {len(all_results)}")
    print(f"Successful: {total_successful}")
    print(f"Failed: {total_failed}")
    
    # Update state
    state["download_file"] = str(output_file)
    state["download_time"] = datetime.now().isoformat()
    state["results_successful"] = total_successful
    state["results_failed"] = total_failed
    save_state(state)
    
    return 0


def action_wait(timeout: int = 24 * 3600, silent: bool = False) -> int:
    """Wait for all batches to complete.
    
    Args:
        timeout: Maximum wait time in seconds (default: 24 hours)
        silent: If True, minimal output for integration use
    """
    from graphrag.graphrag.language_model.providers.zhipu_batch import ZhipuBatchClient
    
    if not silent:
        print("\n" + "=" * 60)
        print("⏳ Waiting for Batch Completion")
        print("=" * 60)
    
    state = load_state()
    if not state:
        print("\n❌ No batch job found. Run --action submit first.")
        return 1
    
    # Support both multi-batch and legacy single-batch format
    if "batches" in state:
        batches = state["batches"]
    elif "batch_id" in state:
        # Legacy format
        batches = [{
            "batch_id": state["batch_id"],
            "document_count": state.get("document_count", 0),
        }]
    else:
        print("\n❌ No batch information found in state.")
        return 1
    
    total_docs = state.get("total_documents", sum(b.get("document_count", 0) for b in batches))
    
    if not silent:
        print(f"\nTotal batches: {len(batches)}")
        print(f"Total documents: {total_docs}")
        print(f"Timeout: {timeout // 3600} hours")
        print("\nPress Ctrl+C to stop waiting\n")
    else:
        print(f"Waiting for {len(batches)} batch(es) ({total_docs} docs)...")
    
    client = ZhipuBatchClient(api_key=get_zhipu_api_key())
    poll_interval = 30
    start_time = time.time()
    
    # Track completion status for each batch
    batch_status = {b["batch_id"]: "pending" for b in batches}
    
    try:
        while True:
            elapsed = time.time() - start_time
            if elapsed > timeout:
                print(f"\n⏰ Timeout after {elapsed/3600:.1f} hours")
                return 1
            
            # Check each pending batch
            all_done = True
            any_failed = False
            total_completed = 0
            total_total = 0
            
            for batch_info in batches:
                batch_id = batch_info["batch_id"]
                
                # Skip already completed/failed batches
                if batch_status[batch_id] in ["completed", "failed", "cancelled", "expired"]:
                    if batch_status[batch_id] == "completed":
                        total_completed += batch_info.get("document_count", 0)
                        total_total += batch_info.get("document_count", 0)
                    continue
                
                try:
                    status, info = client.get_batch_status(batch_id)
                    
                    # Handle SDK object or dict
                    if isinstance(info, dict):
                        counts = info.get("request_counts", {})
                    else:
                        counts = getattr(info, "request_counts", None) or {}
                    
                    if isinstance(counts, dict):
                        batch_total = counts.get("total", 0)
                        batch_completed = counts.get("completed", 0)
                        batch_failed = counts.get("failed", 0)
                    else:
                        batch_total = getattr(counts, "total", 0) or 0
                        batch_completed = getattr(counts, "completed", 0) or 0
                        batch_failed = getattr(counts, "failed", 0) or 0
                    
                    total_completed += batch_completed + batch_failed
                    total_total += batch_total
                    
                    if status.value == "completed":
                        batch_status[batch_id] = "completed"
                    elif status.value in ["failed", "cancelled", "expired"]:
                        batch_status[batch_id] = status.value
                        any_failed = True
                    else:
                        all_done = False
                        
                except Exception as e:
                    logger.warning(f"Error checking batch {batch_id}: {e}")
                    all_done = False
            
            # Progress display
            progress = int(total_completed / total_total * 100) if total_total > 0 else 0
            completed_batches = sum(1 for s in batch_status.values() if s == "completed")
            
            if not silent:
                timestamp = datetime.now().strftime('%H:%M:%S')
                status_str = (
                    f"[{timestamp}] Batches: {completed_batches}/{len(batches)} | "
                    f"Progress: {total_completed}/{total_total} ({progress}%)"
                )
                print(f"\r{status_str}     ", end="", flush=True)
            
            if all_done:
                if any_failed:
                    print(f"\n⚠️ Some batches failed")
                    for batch_id, status in batch_status.items():
                        if status not in ["completed", "pending"]:
                            print(f"   - {batch_id}: {status}")
                    return 1
                else:
                    print(f"\n✅ All {len(batches)} batches completed!")
                    return 0
            
            time.sleep(poll_interval)
            
    except KeyboardInterrupt:
        print("\n\n👋 Stopped by user")
        return 1


def _get_default_prompt() -> str:
    """Get default extraction prompt."""
    return """你是网络设备配置领域的知识图谱构建专家。
你需要从技术文档中提取实体和关系，构建配置知识图谱。

# 实体类型定义
请识别以下类型的实体：
- product_module: 产品功能模块
- protocol: 网络协议类型
- step_type: 配置步骤类型
- configuration: 配置项或配置块
- command: CLI 命令
- parameter: 配置参数
- scenario: 配置场景

# 关系类型定义
- DEPENDS_ON: 配置依赖关系
- SUPPORTS_PROTOCOL: 协议支持关系
- BELONGS_TO: 归属关系
- CONFIGURES: 配置关系
- IS_ADVANCED_FEATURE: 高级特性关系
- PART_OF: 组成部分关系

# 输出格式
("entity"<|>实体名<|>实体类型<|>实体描述)##
("relationship"<|>源实体<|>目标实体<|>关系类型<|>关系描述<|>关系权重)##

# 待处理文本
{input_text}
"""


def action_cancel() -> int:
    """Cancel all pending batches."""
    from graphrag.graphrag.language_model.providers.zhipu_batch import ZhipuBatchClient
    
    print("\n" + "=" * 60)
    print("🛑 Cancelling Batch Job(s)")
    print("=" * 60)
    
    state = load_state()
    if not state:
        print("\n❌ No batch job found.")
        return 1
    
    # Support both multi-batch and legacy single-batch format
    if "batches" in state:
        batches = state["batches"]
    elif "batch_id" in state:
        batches = [{"batch_id": state["batch_id"]}]
    else:
        print("\n❌ No batch information found in state.")
        return 1
    
    print(f"\nFound {len(batches)} batch(es) to cancel")
    
    client = ZhipuBatchClient(api_key=get_zhipu_api_key())
    cancelled_count = 0
    
    for i, batch_info in enumerate(batches):
        batch_id = batch_info["batch_id"]
        print(f"\n[{i + 1}/{len(batches)}] Cancelling {batch_id}...")
        
        try:
            # First check current status
            status, _ = client.get_batch_status(batch_id)
            
            if status.value in ["completed", "failed", "cancelled", "expired"]:
                print(f"   ⚠️ Already {status.value}, skipping")
                continue
            
            # Cancel the batch
            result = client.cancel_batch(batch_id)
            print(f"   ✅ Cancelled successfully")
            cancelled_count += 1
            
        except Exception as e:
            print(f"   ❌ Error: {e}")
    
    print("\n" + "=" * 60)
    print(f"Cancelled {cancelled_count}/{len(batches)} batch(es)")
    print("=" * 60)
    
    return 0


def action_build_graphrag_index() -> int:
    """Build GraphRAG index from batch results."""
    print("\n" + "=" * 60)
    print("🔨 Building GraphRAG Index")
    print("=" * 60)
    
    try:
        import asyncio
        from init_graphrag import build_index
        
        workspace_dir = WORKSPACE_DIR
        print(f"\nWorkspace: {workspace_dir}")
        
        # Run async build
        success = asyncio.run(build_index(workspace_dir))
        
        if success:
            print("\n✅ GraphRAG index built successfully!")
            return 0
        else:
            print("\n❌ GraphRAG index build failed")
            return 1
            
    except Exception as e:
        logger.error(f"Failed to build GraphRAG index: {e}")
        print(f"\n❌ Error: {e}")
        return 1


def main():
    parser = argparse.ArgumentParser(description="GraphRAG Batch Processing Manager")
    
    parser.add_argument(
        "--action",
        choices=["status", "submit", "download", "wait", "cancel", "build", "index"],
        default="status",
        help="Action to perform (build = full pipeline: submit + wait + download + index)"
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Limit number of documents (for testing)"
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=RECOMMENDED_BATCH_SIZE,
        help=f"Requests per batch (default: {RECOMMENDED_BATCH_SIZE}, max: {MAX_REQUESTS_PER_BATCH})"
    )
    parser.add_argument(
        "--model",
        type=str,
        default=DEFAULT_MODEL,
        help=f"Model to use (default: {DEFAULT_MODEL}). Options: glm-4-flashx-250414 (fast), glm-4-flash (FREE)"
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=24 * 3600,
        help="Timeout for wait action (seconds)"
    )
    
    args = parser.parse_args()
    
    # Load environment
    env_utils.load_inagent_env()
    
    if args.action == "status":
        return action_status()
    elif args.action == "submit":
        return action_submit(args.limit, args.batch_size, args.model)
    elif args.action == "download":
        return action_download()
    elif args.action == "wait":
        return action_wait(args.timeout)
    elif args.action == "cancel":
        return action_cancel()
    elif args.action == "index":
        return action_build_graphrag_index()
    elif args.action == "build":
        # Full build: submit + wait + download + index
        print("\n" + "=" * 60)
        print("🚀 Full GraphRAG Build Pipeline")
        print("=" * 60)
        print(f"\nModel: {args.model}")
        print("\nThis will automatically:")
        print("  1. Submit batch job to Zhipu API")
        print("  2. Wait for completion")
        print("  3. Download results")
        print("  4. Build GraphRAG index")
        print("\n⏳ This may take several hours for large datasets...")
        
        # Step 1: Submit
        print("\n" + "-" * 40)
        print("Step 1/4: Submitting batch job...")
        print("-" * 40)
        ret = action_submit(args.limit, args.batch_size, args.model)
        if ret != 0:
            print("\n❌ Build failed at submit step")
            return ret
        
        # Step 2: Wait
        print("\n" + "-" * 40)
        print("Step 2/4: Waiting for completion...")
        print("-" * 40)
        ret = action_wait(args.timeout, silent=True)
        if ret != 0:
            print("\n❌ Build failed at wait step")
            return ret
        
        # Step 3: Download
        print("\n" + "-" * 40)
        print("Step 3/4: Downloading results...")
        print("-" * 40)
        ret = action_download()
        if ret != 0:
            print("\n❌ Build failed at download step")
            return ret
        
        # Step 4: Build index
        print("\n" + "-" * 40)
        print("Step 4/4: Building GraphRAG index...")
        print("-" * 40)
        ret = action_build_graphrag_index()
        if ret != 0:
            print("\n❌ Build failed at index step")
            return ret
        
        print("\n" + "=" * 60)
        print("🎉 Full GraphRAG Build Complete!")
        print("=" * 60)
        print("\nGraphRAG is now ready for use in workflows.")
        return 0
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
