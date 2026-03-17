#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Complete test script that runs service and tests it."""

import subprocess
import time
import sys
import requests
import json

def start_service():
    """Start the gateway service."""
    print("\n" + "=" * 80)
    print("Starting LLM Gateway Service...")
    print("=" * 80)
    
    # Start service in background
    process = subprocess.Popen(
        [sys.executable, "llm_gateway\\start.py"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1
    )
    
    # Wait for service to start
    print("Waiting for service to initialize (15 seconds)...")
    time.sleep(15)
    
    return process

def test_health(base_url):
    """Test health endpoint."""
    print("\n" + "=" * 80)
    print("1. Testing Health Check...")
    print("=" * 80)
    
    try:
        response = requests.get(f"{base_url}/health", timeout=5)
        if response.status_code == 200:
            data = response.json()
            print("✓ Health check passed")
            print(f"  Status: {data.get('status')}")
            print(f"  Mode: {data.get('mode')}")
            print(f"  Chat models: {data.get('chat_models')}")
            return True
        else:
            print(f"✗ Health check failed: HTTP {response.status_code}")
            return False
    except Exception as e:
        print(f"✗ Health check error: {e}")
        return False

def test_chat(base_url):
    """Test chat completion."""
    print("\n" + "=" * 80)
    print("2. Testing Chat Completion...")
    print("=" * 80)
    
    payload = {
        "model": "race",
        "messages": [
            {"role": "system", "content": "你是一个乐于助人的AI助手。"},
            {"role": "user", "content": "你好！请用一句话介绍自己。"}
        ],
        "temperature": 0.7,
        "max_tokens": 100
    }
    
    try:
        print("Sending request...")
        response = requests.post(
            f"{base_url}/v1/chat/completions",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=60
        )
        
        print(f"Response status: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            print("✓ Chat completion succeeded")
            print(f"  Model: {data.get('model')}")
            print(f"  Content: {data.get('choices', [{}])[0].get('message', {}).get('content', '')[:100]}...")
            return True
        else:
            print(f"✗ Chat completion failed: HTTP {response.status_code}")
            print(f"  Response: {response.text}")
            return False
            
    except Exception as e:
        print(f"✗ Chat completion error: {e}")
        import traceback
        traceback.print_exc()
        return False

def main():
    """Main test function."""
    base_url = "http://localhost:9000"
    
    # Start service
    process = start_service()
    
    try:
        # Run tests
        health_ok = test_health(base_url)
        chat_ok = test_chat(base_url) if health_ok else False
        
        # Summary
        print("\n" + "=" * 80)
        print("Test Summary")
        print("=" * 80)
        print(f"Health Check: {'✓ PASS' if health_ok else '✗ FAIL'}")
        print(f"Chat Completion: {'✓ PASS' if chat_ok else '✗ FAIL'}")
        print("=" * 80)
        
        # Wait for user input before stopping
        print("\nPress Ctrl+C to stop the service and exit...")
        try:
            process.wait()
        except KeyboardInterrupt:
            print("\nStopping service...")
            
    finally:
        # Cleanup
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        
        print("Service stopped.")

if __name__ == "__main__":
    main()
