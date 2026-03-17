#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""简单测试LLM Gateway聊天接口"""

import json
import requests

BASE_URL = "http://localhost:9000"

# Test chat completion
payload = {
    "model": "race",
    "messages": [
        {"role": "system", "content": "你是一个乐于助人的AI助手。"},
        {"role": "user", "content": "你好,请介绍一下自己。"}
    ],
    "temperature": 0.7,
    "max_tokens": 100
}

print("发送聊天请求到:", f"{BASE_URL}/v1/chat/completions")
print("请求参数:", json.dumps(payload, ensure_ascii=False, indent=2))
print()

try:
    response = requests.post(
        f"{BASE_URL}/v1/chat/completions",
        json=payload,
        timeout=30
    )
    
    print(f"响应状态码: {response.status_code}")
    print(f"响应头: {dict(response.headers)}")
    print()
    
    if response.status_code == 200:
        data = response.json()
        print("响应成功!")
        print("响应内容:", json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print("响应失败!")
        print("错误信息:", response.text)
        
except Exception as e:
    print(f"请求异常: {e}")
    import traceback
    traceback.print_exc()
