"""Test script for mineru_rag_demo functionality."""

import asyncio
import json
import os
import sys

sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../"))
)

from camel.loaders.local_mineru_reader import LocalMinerUReader


async def test_mineru_output():
    """Test if MinerU output is correctly generated and accessible."""
    print("=" * 70)
    print("Test 1: MinerU Output Verification")
    print("=" * 70)
    
    # Check if output directory exists
    output_dir = r"C:\SynologyDrive\INFOAGEN\mineru\output_cli_pdf"
    json_path = os.path.join(
        output_dir, "cli", "hybrid_auto", "cli_content_list.json"
    )
    
    if not os.path.exists(json_path):
        print("❌ JSON file not found!")
        return False
    
    print(f"✅ JSON file found: {json_path}")
    
    # Load and validate JSON
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            content_list = json.load(f)
        
        print(f"✅ JSON loaded successfully")
        print(f"   - Total blocks: {len(content_list)}")
        
        # Count different types
        type_counts = {}
        for block in content_list:
            block_type = block.get("type", "unknown")
            type_counts[block_type] = type_counts.get(block_type, 0) + 1
        
        print(f"   - Block types:")
        for btype, count in sorted(type_counts.items()):
            print(f"     * {btype}: {count}")
        
        # Check for text content
        text_blocks = [b for b in content_list if b.get("type") == "text"]
        if text_blocks:
            print(f"✅ Found {len(text_blocks)} text blocks")
            sample_text = text_blocks[0].get("text", "")[:100]
            print(f"   - Sample text: {sample_text}...")
        else:
            print("❌ No text blocks found!")
            return False
        
        return True
        
    except Exception as e:
        print(f"❌ Error loading JSON: {e}")
        return False


async def test_chunking():
    """Test if chunking works correctly."""
    print("\n" + "=" * 70)
    print("Test 2: Chunking Verification")
    print("=" * 70)
    
    try:
        # Load JSON
        json_path = os.path.join(
            r"C:\SynologyDrive\INFOAGEN\mineru\output_cli_pdf",
            "cli", "hybrid_auto", "cli_content_list.json"
        )
        
        with open(json_path, "r", encoding="utf-8") as f:
            content_list = json.load(f)
        
        # Import chunker
        from apps.agents.mineru_rag_demo import MinerUChunker
        
        chunker = MinerUChunker(token_limit=800, min_chunk_tokens=120)
        chunks = chunker.chunk(content_list)
        
        print(f"✅ Chunking completed")
        print(f"   - Total chunks: {len(chunks)}")
        
        if chunks:
            print(f"   - First chunk:")
            first_chunk = chunks[0]
            print(f"     * Text length: {len(first_chunk.text)}")
            print(f"     * Metadata: {first_chunk.metadata}")
            print(f"     * Sample text: {first_chunk.text[:200]}...")
            return True
        else:
            print("❌ No chunks generated!")
            return False
            
    except Exception as e:
        print(f"❌ Error during chunking: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_bm25_retrieval():
    """Test BM25 retrieval without API key."""
    print("\n" + "=" * 70)
    print("Test 3: BM25 Retrieval (No API Key Required)")
    print("=" * 70)
    
    try:
        # Load JSON
        json_path = os.path.join(
            r"C:\SynologyDrive\INFOAGEN\mineru\output_cli_pdf",
            "cli", "hybrid_auto", "cli_content_list.json"
        )
        
        with open(json_path, "r", encoding="utf-8") as f:
            content_list = json.load(f)
        
        # Import chunker
        from apps.agents.mineru_rag_demo import MinerUChunker
        
        chunker = MinerUChunker(token_limit=800, min_chunk_tokens=120)
        chunks = chunker.chunk(content_list)
        texts = [c.text for c in chunks]
        
        if not texts:
            print("❌ No text to index!")
            return False
        
        # Test BM25
        try:
            from rank_bm25 import BM25Okapi
            
            tokenized_corpus = [t.split(" ") for t in texts]
            bm25 = BM25Okapi(tokenized_corpus)
            
            # Test query
            query = "How to configure system numa?"
            scores = bm25.get_scores(query.split(" "))
            
            # Get top 5 results
            scored_idxs = sorted(
                enumerate(scores), key=lambda x: x[1], reverse=True
            )[:5]
            
            print(f"✅ BM25 retrieval successful")
            print(f"   - Query: '{query}'")
            print(f"   - Top 5 results:")
            
            for i, (idx, score) in enumerate(scored_idxs, 1):
                text_preview = texts[idx][:100].replace("\n", " ")
                print(f"     {i}. Score: {score:.4f}")
                print(f"        Text: {text_preview}...")
            
            # Check if we got meaningful results
            if scored_idxs[0][1] > 0:
                print("✅ Found relevant results!")
                return True
            else:
                print("⚠️  No relevant results (all scores = 0)")
                return False
                
        except ImportError:
            print("❌ rank_bm25 not installed. Install with: pip install rank-bm25")
            return False
            
    except Exception as e:
        print(f"❌ Error during BM25 retrieval: {e}")
        import traceback
        traceback.print_exc()
        return False


async def main():
    """Run all tests."""
    print("\n" + "=" * 70)
    print("MINERU RAG DEMO - FUNCTIONALITY TEST")
    print("=" * 70 + "\n")
    
    results = {}
    
    # Test 1: MinerU output
    results["mineru_output"] = await test_mineru_output()
    
    # Test 2: Chunking
    results["chunking"] = await test_chunking()
    
    # Test 3: BM25 retrieval
    results["bm25_retrieval"] = await test_bm25_retrieval()
    
    # Summary
    print("\n" + "=" * 70)
    print("TEST SUMMARY")
    print("=" * 70)
    
    for test_name, passed in results.items():
        status = "✅ PASSED" if passed else "❌ FAILED"
        print(f"{test_name:20} : {status}")
    
    all_passed = all(results.values())
    print("\n" + "=" * 70)
    if all_passed:
        print("🎉 ALL TESTS PASSED!")
    else:
        print("⚠️  SOME TESTS FAILED")
    print("=" * 70 + "\n")
    
    return all_passed


if __name__ == "__main__":
    asyncio.run(main())
