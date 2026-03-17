# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========

"""Test script for LLM Gateway Service."""

import asyncio
import json
import sys
import time
from typing import Dict, Any

import httpx


class GatewayTester:
    """Test the LLM Gateway service."""
    
    def __init__(self, base_url: str = "http://localhost:8000"):
        """Initialize tester.
        
        Args:
            base_url: Gateway service base URL
        """
        self.base_url = base_url
        self.client = httpx.AsyncClient(timeout=60.0)
    
    async def test_health(self) -> bool:
        """Test health endpoint."""
        print("\n" + "=" * 80)
        print("🔍 Testing Health Endpoint")
        print("=" * 80)
        
        try:
            response = await self.client.get(f"{self.base_url}/health")
            response.raise_for_status()
            
            data = response.json()
            print(f"✓ Health check passed")
            print(f"  Status: {data.get('status')}")
            print(f"  Mode: {data.get('mode')}")
            print(f"  Chat models: {data.get('chat_models')}")
            print(f"  Embedding models: {data.get('embedding_models')}")
            print(f"  Reranker models: {data.get('reranker_models')}")
            
            return True
        except Exception as e:
            print(f"✗ Health check failed: {e}")
            return False
    
    async def test_metrics(self) -> bool:
        """Test metrics endpoint."""
        print("\n" + "=" * 80)
        print("📊 Testing Metrics Endpoint")
        print("=" * 80)
        
        try:
            response = await self.client.get(f"{self.base_url}/metrics")
            response.raise_for_status()
            
            data = response.json()
            print(f"✓ Metrics retrieved successfully")
            
            # Print global limits
            if data.get("global"):
                print(f"\n  Global Limits:")
                global_stats = data["global"]
                print(f"    RPM: {global_stats['rpm']['current']}/{global_stats['rpm']['limit']} "
                      f"({global_stats['rpm']['usage_percent']:.1f}%)")
                print(f"    TPM: {global_stats['tpm']['current']}/{global_stats['tpm']['limit']} "
                      f"({global_stats['tpm']['usage_percent']:.1f}%)")
            
            # Print provider limits
            if data.get("providers"):
                print(f"\n  Provider Limits:")
                for provider, stats in data["providers"].items():
                    print(f"    {provider}:")
                    print(f"      RPM: {stats['rpm']['current']}/{stats['rpm']['limit']} "
                          f"({stats['rpm']['usage_percent']:.1f}%)")
                    print(f"      TPM: {stats['tpm']['current']}/{stats['tpm']['limit']} "
                          f"({stats['tpm']['usage_percent']:.1f}%)")
            
            # Print model limits
            if data.get("models"):
                print(f"\n  Model Limits:")
                for model, stats in data["models"].items():
                    print(f"    {model}:")
                    print(f"      RPM: {stats['rpm']['current']}/{stats['rpm']['limit']} "
                          f"({stats['rpm']['usage_percent']:.1f}%)")
                    print(f"      TPM: {stats['tpm']['current']}/{stats['tpm']['limit']} "
                          f"({stats['tpm']['usage_percent']:.1f}%)")
            
            return True
        except Exception as e:
            print(f"✗ Metrics retrieval failed: {e}")
            return False
    
    async def test_chat_completion(self) -> bool:
        """Test chat completion endpoint."""
        print("\n" + "=" * 80)
        print("💬 Testing Chat Completion (Race Mode)")
        print("=" * 80)
        
        try:
            request_data = {
                "model": "glm-z1-9b",  # This will trigger race mode across all models
                "messages": [
                    {"role": "system", "content": "You are a helpful assistant."},
                    {"role": "user", "content": "Hello! What is 2+2? Please answer in one sentence."}
                ],
                "temperature": 0.7,
                "max_tokens": 100
            }
            
            print(f"📤 Sending request...")
            print(f"  Messages: {len(request_data['messages'])}")
            print(f"  User: {request_data['messages'][-1]['content']}")
            
            start_time = time.time()
            response = await self.client.post(
                f"{self.base_url}/v1/chat/completions",
                json=request_data
            )
            elapsed_time = time.time() - start_time
            
            response.raise_for_status()
            
            data = response.json()
            headers = response.headers
            
            print(f"\n✓ Chat completion successful")
            print(f"  Response time: {elapsed_time:.2f}s")
            print(f"  Model used: {headers.get('X-Model-Name', 'Unknown')} (ID: {headers.get('X-Model-ID', 'Unknown')})")
            print(f"  Provider: {headers.get('X-Provider', 'Unknown')}")
            print(f"  Model elapsed: {headers.get('X-Elapsed-Time', 'Unknown')}s")
            
            if data.get("choices"):
                content = data["choices"][0]["message"]["content"]
                print(f"\n  Assistant response: {content}")
            
            if data.get("usage"):
                usage = data["usage"]
                print(f"\n  Token usage:")
                print(f"    Prompt: {usage.get('prompt_tokens', 0)}")
                print(f"    Completion: {usage.get('completion_tokens', 0)}")
                print(f"    Total: {usage.get('total_tokens', 0)}")
            
            return True
        except Exception as e:
            print(f"✗ Chat completion failed: {e}")
            if hasattr(e, 'response') and e.response is not None:
                print(f"  Response: {e.response.text}")
            return False
    
    async def test_multiple_chat_requests(self, count: int = 3) -> bool:
        """Test multiple chat requests to verify rate limiting and race mode."""
        print("\n" + "=" * 80)
        print(f"🏃 Testing Multiple Chat Requests (n={count})")
        print("=" * 80)
        
        try:
            tasks = []
            for i in range(count):
                request_data = {
                    "model": "glm-z1-9b",
                    "messages": [
                        {"role": "user", "content": f"Request {i+1}: What is {i+2}+{i+2}? Answer briefly."}
                    ],
                    "temperature": 0.7,
                    "max_tokens": 50
                }
                
                task = self.client.post(
                    f"{self.base_url}/v1/chat/completions",
                    json=request_data
                )
                tasks.append(task)
            
            print(f"📤 Sending {count} requests concurrently...")
            start_time = time.time()
            
            responses = await asyncio.gather(*tasks, return_exceptions=True)
            
            elapsed_time = time.time() - start_time
            
            print(f"\n✓ All requests completed in {elapsed_time:.2f}s")
            print(f"  Average time per request: {elapsed_time/count:.2f}s")
            
            # Analyze results
            successful = 0
            failed = 0
            model_usage = {}
            
            for i, response in enumerate(responses):
                if isinstance(response, Exception):
                    print(f"  Request {i+1}: ✗ Failed - {response}")
                    failed += 1
                else:
                    model_id = response.headers.get('X-Model-ID', 'Unknown')
                    model_name = response.headers.get('X-Model-Name', 'Unknown')
                    elapsed = response.headers.get('X-Elapsed-Time', 'Unknown')
                    
                    print(f"  Request {i+1}: ✓ Success - Model: {model_name} ({elapsed}s)")
                    
                    model_usage[model_id] = model_usage.get(model_id, 0) + 1
                    successful += 1
            
            print(f"\n  Summary:")
            print(f"    Successful: {successful}/{count}")
            print(f"    Failed: {failed}/{count}")
            print(f"\n  Model usage distribution:")
            for model_id, count_val in model_usage.items():
                print(f"    {model_id}: {count_val} times")
            
            return successful > 0
        except Exception as e:
            print(f"✗ Multiple chat requests failed: {e}")
            return False
    
    async def test_embedding(self) -> bool:
        """Test embedding endpoint."""
        print("\n" + "=" * 80)
        print("🔢 Testing Embedding")
        print("=" * 80)
        
        try:
            request_data = {
                "model": "BAAI/bge-m3",
                "input": "This is a test sentence for embedding."
            }
            
            print(f"📤 Sending embedding request...")
            print(f"  Model: {request_data['model']}")
            print(f"  Input: {request_data['input']}")
            
            start_time = time.time()
            response = await self.client.post(
                f"{self.base_url}/v1/embeddings",
                json=request_data
            )
            elapsed_time = time.time() - start_time
            
            response.raise_for_status()
            
            data = response.json()
            
            print(f"\n✓ Embedding generated successfully")
            print(f"  Response time: {elapsed_time:.2f}s")
            print(f"  Vector count: {len(data.get('data', []))}")
            
            if data.get("data") and len(data["data"]) > 0:
                embedding = data["data"][0]["embedding"]
                print(f"  Vector dimension: {len(embedding)}")
                print(f"  First 5 values: {embedding[:5]}")
            
            if data.get("usage"):
                usage = data["usage"]
                print(f"\n  Token usage:")
                print(f"    Prompt: {usage.get('prompt_tokens', 0)}")
                print(f"    Total: {usage.get('total_tokens', 0)}")
            
            return True
        except Exception as e:
            print(f"✗ Embedding failed: {e}")
            if hasattr(e, 'response') and e.response is not None:
                print(f"  Response: {e.response.text}")
            return False
    
    async def run_all_tests(self):
        """Run all tests."""
        print("\n" + "=" * 80)
        print("🧪 INFOAGEN LLM Gateway Test Suite")
        print("=" * 80)
        
        results = {}
        
        # Test 1: Health check
        results["health"] = await self.test_health()
        
        # Test 2: Metrics
        results["metrics"] = await self.test_metrics()
        
        # Test 3: Single chat completion
        results["chat"] = await self.test_chat_completion()
        
        # Test 4: Multiple chat requests (race mode verification)
        results["multiple_chat"] = await self.test_multiple_chat_requests(count=5)
        
        # Test 5: Embedding
        results["embedding"] = await self.test_embedding()
        
        # Test 6: Check metrics after tests
        print("\n" + "=" * 80)
        print("📊 Final Metrics Check")
        print("=" * 80)
        await self.test_metrics()
        
        # Summary
        print("\n" + "=" * 80)
        print("📋 Test Summary")
        print("=" * 80)
        
        total = len(results)
        passed = sum(1 for v in results.values() if v)
        failed = total - passed
        
        for test_name, result in results.items():
            status = "✓ PASSED" if result else "✗ FAILED"
            print(f"  {test_name}: {status}")
        
        print(f"\n  Total: {total}")
        print(f"  Passed: {passed}")
        print(f"  Failed: {failed}")
        print(f"  Success rate: {passed/total*100:.1f}%")
        
        await self.client.aclose()
        
        return failed == 0


async def main():
    """Main test function."""
    import argparse
    
    parser = argparse.ArgumentParser(description="Test LLM Gateway Service")
    parser.add_argument(
        "--url",
        default="http://localhost:9000",
        help="Gateway service URL (default: http://localhost:9000)"
    )
    
    args = parser.parse_args()
    
    tester = GatewayTester(base_url=args.url)
    
    try:
        success = await tester.run_all_tests()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n\nTest interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n\nTest suite failed with error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
