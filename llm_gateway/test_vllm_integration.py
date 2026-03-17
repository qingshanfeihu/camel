"""Test LLM Gateway vLLM integration"""
import os
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from openai import OpenAI

# Test Gateway connection
gateway_url = "http://127.0.0.1:9000/v1"
client = OpenAI(
    api_key="gateway-local-key",
    base_url=gateway_url
)

print(f"Testing Gateway at: {gateway_url}")
print("=" * 60)

try:
    # Test 1: List available models
    print("\n1. Listing available models...")
    models = client.models.list()
    print(f"   Found {len(models.data)} models:")
    for model in models.data:
        print(f"   - {model.id}")
    
    # Test 2: Simple chat completion with GLM-Z1-9B
    print("\n2. Testing chat completion with THUDM/GLM-Z1-9B-0414...")
    response = client.chat.completions.create(
        model="THUDM/GLM-Z1-9B-0414",
        messages=[
            {"role": "user", "content": "你好，请简单介绍一下自己。"}
        ],
        max_tokens=100,
        temperature=0.7
    )
    print(f"   Response: {response.choices[0].message.content}")
    print(f"   Model used: {response.model}")
    print(f"   Tokens: {response.usage.total_tokens}")
    
    print("\n" + "=" * 60)
    print("✅ Gateway vLLM integration test PASSED")
    
except Exception as e:
    print(f"\n❌ Gateway test FAILED: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
