# Quick test for API Gateway
import requests
import json
import time

print("="*80)
print("Testing LLM Gateway on http://localhost:9000")
print("="*80)

# Wait for service to start
print("\nWaiting for service to start...")
time.sleep(3)

# Test 1: Health check
print("\n[Test 1] Health Check")
try:
    resp = requests.get("http://localhost:9000/health", timeout=5)
    print(f"Status: {resp.status_code}")
    print(f"Response: {json.dumps(resp.json(), indent=2, ensure_ascii=False)}")
except Exception as e:
    print(f"Failed: {e}")

# Test 2: Metrics
print("\n[Test 2] Metrics")
try:
    resp = requests.get("http://localhost:9000/metrics", timeout=5)
    print(f"Status: {resp.status_code}")
    print(f"Response: {json.dumps(resp.json(), indent=2, ensure_ascii=False)}")
except Exception as e:
    print(f"Failed: {e}")

# Test 3: Chat completion
print("\n[Test 3] Chat Completion")
try:
    payload = {
        "model": "THUDM/GLM-Z1-9B-0414",
        "messages": [
            {"role": "user", "content": "你好，请用一句话介绍一下自己"}
        ],
        "max_tokens": 100
    }
    resp = requests.post(
        "http://localhost:9000/v1/chat/completions",
        json=payload,
        timeout=30
    )
    print(f"Status: {resp.status_code}")
    result = resp.json()
    if "choices" in result:
        print(f"Response: {result['choices'][0]['message']['content']}")
    else:
        print(f"Response: {json.dumps(result, indent=2, ensure_ascii=False)}")
except Exception as e:
    print(f"Failed: {e}")

print("\n" + "="*80)
print("Tests completed")
print("="*80)
