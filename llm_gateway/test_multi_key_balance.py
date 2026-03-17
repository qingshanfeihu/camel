#!/usr/bin/env python3
"""Test multi-API-key load balancing in LLM Gateway."""

import asyncio
import httpx
import time
from typing import Dict, List


async def test_chat_completion(session_id: int) -> Dict:
    """Test a single chat completion request."""
    url = "http://127.0.0.1:9000/v1/chat/completions"
    
    payload = {
        "model": "any-model",  # Gateway ignores this
        "messages": [
            {"role": "user", "content": f"Count from 1 to 3 (Session {session_id})"}
        ],
        "temperature": 0.7,
        "max_tokens": 50
    }
    
    start_time = time.time()
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            
            elapsed = time.time() - start_time
            result = response.json()
            
            # Extract model info from response
            model_used = result.get("model", "unknown")
            content = result["choices"][0]["message"]["content"]
            
            return {
                "session_id": session_id,
                "success": True,
                "elapsed": elapsed,
                "model": model_used,
                "content_length": len(content),
                "content_preview": content[:50] + "..." if len(content) > 50 else content
            }
            
        except Exception as e:
            elapsed = time.time() - start_time
            return {
                "session_id": session_id,
                "success": False,
                "elapsed": elapsed,
                "error": str(e)
            }


async def test_concurrent_requests(num_requests: int = 10):
    """Test multiple concurrent requests to verify load balancing."""
    print(f"\n{'='*80}")
    print(f"Testing Multi-API-Key Load Balancing")
    print(f"{'='*80}")
    print(f"Sending {num_requests} concurrent requests...")
    print(f"Expected behavior: Requests should be distributed across 2 API keys")
    print(f"  - Model IDs should alternate: qwen3-8b_key1, qwen3-8b_key2")
    print(f"  - Both accounts share the load evenly\n")
    
    # Create concurrent tasks
    tasks = [test_chat_completion(i+1) for i in range(num_requests)]
    
    start_time = time.time()
    results = await asyncio.gather(*tasks)
    total_elapsed = time.time() - start_time
    
    # Analyze results
    successful = [r for r in results if r["success"]]
    failed = [r for r in results if not r["success"]]
    
    print(f"\n{'='*80}")
    print(f"Results Summary")
    print(f"{'='*80}")
    print(f"Total requests: {num_requests}")
    print(f"Successful: {len(successful)}")
    print(f"Failed: {len(failed)}")
    print(f"Total time: {total_elapsed:.2f}s")
    print(f"Average latency: {sum(r['elapsed'] for r in successful) / len(successful):.2f}s" if successful else "N/A")
    
    # Count models used
    if successful:
        model_counts = {}
        for r in successful:
            model = r["model"]
            model_counts[model] = model_counts.get(model, 0) + 1
        
        print(f"\nLoad Distribution:")
        for model, count in sorted(model_counts.items()):
            percentage = (count / len(successful)) * 100
            print(f"  {model}: {count} requests ({percentage:.1f}%)")
    
    # Show individual results
    print(f"\n{'='*80}")
    print(f"Individual Request Results")
    print(f"{'='*80}")
    for r in results:
        if r["success"]:
            print(f"Session {r['session_id']:2d}: ✓ {r['elapsed']:.2f}s | Model: {r['model']} | Preview: {r['content_preview']}")
        else:
            print(f"Session {r['session_id']:2d}: ✗ {r['elapsed']:.2f}s | Error: {r['error']}")
    
    print(f"\n{'='*80}")
    print("Test completed!")
    print(f"{'='*80}\n")


async def test_sequential_requests(num_requests: int = 6):
    """Test sequential requests to clearly see round-robin behavior."""
    print(f"\n{'='*80}")
    print(f"Testing Sequential Round-Robin Behavior")
    print(f"{'='*80}")
    print(f"Sending {num_requests} sequential requests...")
    print(f"Expected pattern: key1 -> key2 -> key1 -> key2 -> ...\n")
    
    for i in range(num_requests):
        result = await test_chat_completion(i+1)
        if result["success"]:
            print(f"Request {i+1}: Model: {result['model']} | Latency: {result['elapsed']:.2f}s")
        else:
            print(f"Request {i+1}: Failed - {result['error']}")
        
        # Small delay between requests
        await asyncio.sleep(0.5)
    
    print(f"\n{'='*80}")
    print("Sequential test completed!")
    print(f"{'='*80}\n")


async def main():
    """Run all tests."""
    print("\n" + "="*80)
    print("LLM Gateway Multi-API-Key Load Balancing Test")
    print("="*80)
    
    # Test 1: Sequential requests (clear round-robin pattern)
    await test_sequential_requests(6)
    
    # Test 2: Concurrent requests (stress test)
    await test_concurrent_requests(10)


if __name__ == "__main__":
    asyncio.run(main())
