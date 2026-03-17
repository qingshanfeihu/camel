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

"""快速测试LLM Gateway的各项功能."""

import json
import sys
import requests
from colorama import Fore, Style, init

init(autoreset=True)

BASE_URL = "http://localhost:9000"


def print_header(text):
    """打印测试标题."""
    print("\n" + "=" * 80)
    print(f"{Fore.CYAN}{text}{Style.RESET_ALL}")
    print("=" * 80)


def print_success(text):
    """打印成功信息."""
    print(f"{Fore.GREEN}✓ {text}{Style.RESET_ALL}")


def print_error(text):
    """打印错误信息."""
    print(f"{Fore.RED}✗ {text}{Style.RESET_ALL}")


def print_info(text):
    """打印信息."""
    print(f"{Fore.YELLOW}➜ {text}{Style.RESET_ALL}")


def test_health():
    """测试健康检查接口."""
    print_header("1. 测试健康检查接口")
    try:
        response = requests.get(f"{BASE_URL}/health", timeout=5)
        if response.status_code == 200:
            data = response.json()
            print_success("健康检查通过")
            print_info(f"   状态: {data.get('status')}")
            print_info(f"   模式: {data.get('mode')}")
            print_info(f"   聊天模型数: {data.get('chat_models')}")
            print_info(f"   向量模型数: {data.get('embedding_models')}")
            print_info(f"   Reranker模型数: {data.get('reranker_models')}")
            return True
        else:
            print_error(f"健康检查失败: HTTP {response.status_code}")
            return False
    except Exception as e:
        print_error(f"健康检查异常: {e}")
        return False


def test_metrics():
    """测试指标接口."""
    print_header("2. 测试指标接口")
    try:
        response = requests.get(f"{BASE_URL}/metrics", timeout=5)
        if response.status_code == 200:
            data = response.json()
            print_success("指标获取成功")
            print_info(f"   全局限流: {data.get('global', {})}")
            print_info(f"   提供商数: {len(data.get('providers', {}))}")
            print_info(f"   模型数: {len(data.get('models', {}))}")
            return True
        else:
            print_error(f"指标获取失败: HTTP {response.status_code}")
            return False
    except Exception as e:
        print_error(f"指标获取异常: {e}")
        return False


def test_chat_completion():
    """测试聊天补全接口（Race模式 - 三个模型并行）."""
    print_header("3. 测试聊天补全接口 (Race模式)")
    try:
        payload = {
            "model": "race",  # Race模式会并行调用所有模型
            "messages": [
                {"role": "system", "content": "你是一个乐于助人的AI助手。"},
                {"role": "user", "content": "请用一句话介绍什么是人工智能。"}
            ],
            "temperature": 0.7,
            "max_tokens": 100
        }
        
        print_info("发送请求到 /v1/chat/completions...")
        print_info(f"用户问题: {payload['messages'][1]['content']}")
        
        response = requests.post(
            f"{BASE_URL}/v1/chat/completions",
            json=payload,
            timeout=30
        )
        
        if response.status_code == 200:
            data = response.json()
            print_success("聊天补全成功")
            
            # 打印响应头中的模型信息
            model_id = response.headers.get('X-Model-ID', 'unknown')
            model_name = response.headers.get('X-Model-Name', 'unknown')
            provider = response.headers.get('X-Provider', 'unknown')
            elapsed = response.headers.get('X-Elapsed-Time', 'unknown')
            
            print_info(f"   获胜模型ID: {Fore.MAGENTA}{model_id}{Style.RESET_ALL}")
            print_info(f"   获胜模型名: {Fore.MAGENTA}{model_name}{Style.RESET_ALL}")
            print_info(f"   提供商: {provider}")
            print_info(f"   耗时: {elapsed}秒")
            
            # 打印回复内容
            if 'choices' in data and len(data['choices']) > 0:
                content = data['choices'][0]['message']['content']
                print_info(f"   AI回复: {Fore.CYAN}{content}{Style.RESET_ALL}")
            
            # 打印使用统计
            if 'usage' in data:
                usage = data['usage']
                print_info(f"   Token使用: prompt={usage.get('prompt_tokens')}, "
                          f"completion={usage.get('completion_tokens')}, "
                          f"total={usage.get('total_tokens')}")
            
            return True
        else:
            print_error(f"聊天补全失败: HTTP {response.status_code}")
            print_error(f"响应: {response.text}")
            return False
            
    except Exception as e:
        print_error(f"聊天补全异常: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_embedding():
    """测试向量嵌入接口."""
    print_header("4. 测试向量嵌入接口")
    try:
        payload = {
            "model": "BAAI/bge-m3",
            "input": "这是一段测试文本，用于生成向量表示。"
        }
        
        print_info("发送请求到 /v1/embeddings...")
        print_info(f"输入文本: {payload['input']}")
        
        response = requests.post(
            f"{BASE_URL}/v1/embeddings",
            json=payload,
            timeout=30
        )
        
        if response.status_code == 200:
            data = response.json()
            print_success("向量嵌入成功")
            
            if 'data' in data and len(data['data']) > 0:
                embedding = data['data'][0]['embedding']
                print_info(f"   向量维度: {len(embedding)}")
                print_info(f"   向量前5个值: {embedding[:5]}")
            
            if 'usage' in data:
                usage = data['usage']
                print_info(f"   Token使用: {usage.get('total_tokens')}")
            
            return True
        else:
            print_error(f"向量嵌入失败: HTTP {response.status_code}")
            print_error(f"响应: {response.text}")
            return False
            
    except Exception as e:
        print_error(f"向量嵌入异常: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    """运行所有测试."""
    print(f"\n{Fore.BLUE}{'=' * 80}")
    print(f"{Fore.BLUE}INFOAGEN LLM Gateway - 功能测试")
    print(f"{Fore.BLUE}Gateway URL: {BASE_URL}")
    print(f"{Fore.BLUE}{'=' * 80}{Style.RESET_ALL}\n")
    
    results = []
    
    # 运行所有测试
    results.append(("健康检查", test_health()))
    results.append(("指标接口", test_metrics()))
    results.append(("聊天补全", test_chat_completion()))
    results.append(("向量嵌入", test_embedding()))
    
    # 打印总结
    print_header("测试总结")
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    for name, result in results:
        if result:
            print_success(f"{name}: 通过")
        else:
            print_error(f"{name}: 失败")
    
    print(f"\n{Fore.BLUE}总计: {passed}/{total} 测试通过{Style.RESET_ALL}")
    
    if passed == total:
        print(f"{Fore.GREEN}\n🎉 所有测试通过！Gateway运行正常。{Style.RESET_ALL}\n")
        return 0
    else:
        print(f"{Fore.RED}\n⚠️  部分测试失败，请检查日志。{Style.RESET_ALL}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
