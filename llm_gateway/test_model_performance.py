#!/usr/bin/env python3
"""
测试各个模型的单独性能
直接调用 SiliconFlow API，绕过 Gateway 的轮询
"""
import asyncio
import time
from openai import AsyncOpenAI

# SiliconFlow API 配置
API_KEY = "sk-nrycmpigprnxcoieinmbtqiohcgeucibsxpdjgbhsyivvbzn"
BASE_URL = "https://api.siliconflow.cn/v1"

# 测试的模型列表
MODELS = [
    ("THUDM/GLM-Z1-9B-0414", "GLM-Z1-9B"),
    ("Qwen/Qwen3-8B", "Qwen3-8B"),
    ("deepseek-ai/DeepSeek-R1-0528-Qwen3-8B", "DeepSeek-R1-Qwen3-8B"),
]

# 测试 prompt
TEST_PROMPT = "What is Python? Answer in 2-3 sentences."


async def test_model(client: AsyncOpenAI, model: str, name: str) -> dict:
    """测试单个模型的响应时间"""
    start = time.time()
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": TEST_PROMPT}],
            max_tokens=200,
            temperature=0.7,
        )
        elapsed = time.time() - start
        content = response.choices[0].message.content or ""
        tokens = response.usage.total_tokens if response.usage else 0
        
        return {
            "name": name,
            "model": model,
            "success": True,
            "time": elapsed,
            "tokens": tokens,
            "content_length": len(content),
            "content_preview": content[:100] + "..." if len(content) > 100 else content,
        }
    except Exception as e:
        elapsed = time.time() - start
        return {
            "name": name,
            "model": model,
            "success": False,
            "time": elapsed,
            "error": str(e),
        }


async def main():
    print("=" * 70)
    print("🔬 SiliconFlow 模型性能单独测试")
    print("=" * 70)
    print(f"测试 Prompt: {TEST_PROMPT}")
    print(f"Max Tokens: 200")
    print()
    
    client = AsyncOpenAI(
        api_key=API_KEY,
        base_url=BASE_URL,
        max_retries=0,  # 禁用重试
        timeout=60.0,
    )
    
    results = []
    
    for model, name in MODELS:
        print(f"测试 {name}...")
        
        # 每个模型测试 3 次
        model_times = []
        for i in range(3):
            result = await test_model(client, model, name)
            if result["success"]:
                model_times.append(result["time"])
                print(f"  Round {i+1}: {result['time']:.2f}s, {result['tokens']} tokens")
            else:
                print(f"  Round {i+1}: ❌ {result.get('error', 'Unknown error')[:50]}")
        
        if model_times:
            avg_time = sum(model_times) / len(model_times)
            results.append({
                "name": name,
                "model": model,
                "avg_time": avg_time,
                "min_time": min(model_times),
                "max_time": max(model_times),
                "tests": len(model_times),
            })
        print()
    
    # 汇总结果
    print("=" * 70)
    print("📊 模型性能汇总")
    print("=" * 70)
    print(f"{'模型':<25} {'平均时间':<12} {'最快':<10} {'最慢':<10}")
    print("-" * 70)
    
    # 按平均时间排序
    results.sort(key=lambda x: x["avg_time"])
    
    for r in results:
        print(f"{r['name']:<25} {r['avg_time']:.2f}s{'':<6} {r['min_time']:.2f}s{'':<4} {r['max_time']:.2f}s")
    
    print()
    print("=" * 70)
    print("💡 建议")
    print("=" * 70)
    
    if results:
        fastest = results[0]
        slowest = results[-1]
        speed_diff = (slowest["avg_time"] / fastest["avg_time"] - 1) * 100
        
        print(f"最快模型: {fastest['name']} (平均 {fastest['avg_time']:.2f}s)")
        print(f"最慢模型: {slowest['name']} (平均 {slowest['avg_time']:.2f}s)")
        print(f"速度差异: {speed_diff:.0f}%")
        print()
        
        if speed_diff > 50:
            print("⚠️  模型间性能差异较大！")
            print("   建议:")
            print(f"   1. 如果速度优先，只使用 {fastest['name']}")
            print("   2. 使用 race 模式让最快的模型先返回")
            print("   3. 或者移除最慢的模型以提高整体响应速度")
        else:
            print("✓ 模型间性能差异在可接受范围内")


if __name__ == "__main__":
    asyncio.run(main())
