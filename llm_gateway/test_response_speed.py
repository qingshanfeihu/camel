#!/usr/bin/env python3
"""
LLM Gateway 响应速度测试脚本

测试目的：
1. 对比流式(stream=true)与非流式(stream=false)响应时间
2. 模拟 GraphRAG 的典型请求模式
3. 分析首字节时间(TTFB)与总响应时间
4. 测试并发请求性能

使用方法：
    python test_response_speed.py [--concurrent N] [--rounds N]
"""

import asyncio
import time
import argparse
import statistics
from typing import List, Dict, Any, Optional
from dataclasses import dataclass
import os
import sys

# 尝试导入 openai，如果失败则提示安装
try:
    from openai import AsyncOpenAI
except ImportError:
    print("请安装 openai: pip install openai")
    sys.exit(1)

# 配置
GATEWAY_URL = os.getenv("LLM_GATEWAY_BASE_URL", "http://127.0.0.1:9000/v1")
GATEWAY_API_KEY = os.getenv("LLM_GATEWAY_API_KEY", "gateway-local-key")

# GraphRAG 典型请求示例
GRAPHRAG_PROMPTS = {
    "entity_extraction": {
        "system": "You are an expert at extracting entities from text.",
        "user": """Extract all product modules, protocols, and configuration items from the following text:

"配置用户管理模块时，需要先设置 LDAP 认证服务器地址为 192.168.1.100，
端口号 389。然后在高可用配置中启用主备切换功能，设置心跳检测间隔为 5 秒。
SLB 负载均衡模块支持 Round-Robin 和 Weighted 两种算法。"

Return as JSON with entities array."""
    },
    "summarization": {
        "system": "You are an expert at summarizing technical documentation.",
        "user": """Summarize the key configuration steps for the following scenario:

The user wants to set up a high-availability cluster with load balancing.
The cluster should have automatic failover with 5-second heartbeat intervals.
LDAP authentication should be integrated for user management.
The system should support both active-passive and active-active modes.

Provide a brief technical summary in 2-3 sentences."""
    },
    "simple_qa": {
        "system": "You are a helpful assistant.",
        "user": "What is the default port for LDAP? Answer briefly."
    }
}


@dataclass
class RequestResult:
    """单次请求结果"""
    success: bool
    stream: bool
    prompt_type: str
    ttfb: float  # Time to First Byte (首字节时间)
    total_time: float  # 总响应时间
    tokens: int  # 总 token 数
    error: Optional[str] = None


async def test_non_stream_request(
    client: AsyncOpenAI,
    messages: List[Dict],
    prompt_type: str
) -> RequestResult:
    """测试非流式请求"""
    start_time = time.perf_counter()
    
    try:
        response = await client.chat.completions.create(
            model="gateway-auto",
            messages=messages,
            stream=False,
            max_tokens=500
        )
        
        total_time = time.perf_counter() - start_time
        tokens = response.usage.total_tokens if response.usage else 0
        
        return RequestResult(
            success=True,
            stream=False,
            prompt_type=prompt_type,
            ttfb=total_time,  # 非流式时 TTFB = 总时间
            total_time=total_time,
            tokens=tokens
        )
        
    except Exception as e:
        total_time = time.perf_counter() - start_time
        return RequestResult(
            success=False,
            stream=False,
            prompt_type=prompt_type,
            ttfb=total_time,
            total_time=total_time,
            tokens=0,
            error=str(e)
        )


async def test_stream_request(
    client: AsyncOpenAI,
    messages: List[Dict],
    prompt_type: str
) -> RequestResult:
    """测试流式请求"""
    start_time = time.perf_counter()
    ttfb = None
    content_chunks = []
    
    try:
        stream = await client.chat.completions.create(
            model="gateway-auto",
            messages=messages,
            stream=True,
            max_tokens=500
        )
        
        async for chunk in stream:
            if ttfb is None:
                ttfb = time.perf_counter() - start_time
            
            if chunk.choices and chunk.choices[0].delta.content:
                content_chunks.append(chunk.choices[0].delta.content)
        
        total_time = time.perf_counter() - start_time
        
        # 估算 token 数（流式时无法精确获取）
        full_content = "".join(content_chunks)
        estimated_tokens = len(full_content) // 4  # 粗略估算
        
        return RequestResult(
            success=True,
            stream=True,
            prompt_type=prompt_type,
            ttfb=ttfb or total_time,
            total_time=total_time,
            tokens=estimated_tokens
        )
        
    except Exception as e:
        total_time = time.perf_counter() - start_time
        return RequestResult(
            success=False,
            stream=True,
            prompt_type=prompt_type,
            ttfb=ttfb or total_time,
            total_time=total_time,
            tokens=0,
            error=str(e)
        )


async def run_test_round(
    client: AsyncOpenAI,
    prompt_type: str,
    stream: bool
) -> RequestResult:
    """运行单轮测试"""
    prompt = GRAPHRAG_PROMPTS[prompt_type]
    messages = [
        {"role": "system", "content": prompt["system"]},
        {"role": "user", "content": prompt["user"]}
    ]
    
    if stream:
        return await test_stream_request(client, messages, prompt_type)
    else:
        return await test_non_stream_request(client, messages, prompt_type)


async def run_concurrent_test(
    client: AsyncOpenAI,
    prompt_type: str,
    stream: bool,
    concurrency: int
) -> List[RequestResult]:
    """并发测试"""
    tasks = [
        run_test_round(client, prompt_type, stream)
        for _ in range(concurrency)
    ]
    return await asyncio.gather(*tasks)


def print_statistics(results: List[RequestResult], label: str):
    """打印统计结果"""
    successful = [r for r in results if r.success]
    failed = [r for r in results if not r.success]
    
    print(f"\n{'='*60}")
    print(f"📊 {label}")
    print(f"{'='*60}")
    print(f"总请求数: {len(results)}, 成功: {len(successful)}, 失败: {len(failed)}")
    
    if failed:
        print(f"\n❌ 失败详情:")
        for r in failed[:3]:  # 只显示前3个错误
            print(f"   - {r.error[:100]}...")
    
    if successful:
        ttfbs = [r.ttfb for r in successful]
        totals = [r.total_time for r in successful]
        tokens = [r.tokens for r in successful]
        
        print(f"\n⏱️  首字节时间 (TTFB):")
        print(f"   最小: {min(ttfbs):.3f}s")
        print(f"   最大: {max(ttfbs):.3f}s")
        print(f"   平均: {statistics.mean(ttfbs):.3f}s")
        if len(ttfbs) > 1:
            print(f"   标准差: {statistics.stdev(ttfbs):.3f}s")
        
        print(f"\n⏱️  总响应时间:")
        print(f"   最小: {min(totals):.3f}s")
        print(f"   最大: {max(totals):.3f}s")
        print(f"   平均: {statistics.mean(totals):.3f}s")
        if len(totals) > 1:
            print(f"   标准差: {statistics.stdev(totals):.3f}s")
        
        print(f"\n📦 Token 统计:")
        print(f"   平均: {statistics.mean(tokens):.0f}")
        print(f"   总计: {sum(tokens)}")
        
        # 计算吞吐量
        total_elapsed = sum(totals)
        tps = sum(tokens) / total_elapsed if total_elapsed > 0 else 0
        print(f"   吞吐量: {tps:.1f} tokens/s")


async def main():
    parser = argparse.ArgumentParser(description="LLM Gateway 响应速度测试")
    parser.add_argument("--rounds", type=int, default=3, help="每种测试的轮数")
    parser.add_argument("--concurrent", type=int, default=1, help="并发请求数")
    parser.add_argument("--prompt", type=str, choices=list(GRAPHRAG_PROMPTS.keys()) + ["all"],
                        default="all", help="测试的 prompt 类型")
    args = parser.parse_args()
    
    print("=" * 60)
    print("🚀 LLM Gateway 响应速度测试")
    print("=" * 60)
    print(f"网关地址: {GATEWAY_URL}")
    print(f"测试轮数: {args.rounds}")
    print(f"并发数: {args.concurrent}")
    print(f"Prompt 类型: {args.prompt}")
    
    # 创建客户端（禁用 SDK 重试）
    client = AsyncOpenAI(
        api_key=GATEWAY_API_KEY,
        base_url=GATEWAY_URL,
        timeout=120.0,
        max_retries=0  # 禁用 SDK 重试
    )
    
    # 确定要测试的 prompt 类型
    prompt_types = list(GRAPHRAG_PROMPTS.keys()) if args.prompt == "all" else [args.prompt]
    
    all_results = {
        "stream": [],
        "non_stream": []
    }
    
    # 预热请求
    print("\n🔥 预热请求...")
    try:
        await client.chat.completions.create(
            model="gateway-auto",
            messages=[{"role": "user", "content": "test"}],
            stream=False,
            max_tokens=10
        )
        print("✓ 预热完成")
    except Exception as e:
        print(f"⚠️ 预热失败: {e}")
    
    # 运行测试
    for prompt_type in prompt_types:
        print(f"\n{'─'*60}")
        print(f"📋 测试 Prompt: {prompt_type}")
        print(f"{'─'*60}")
        
        # 测试非流式
        print("\n🔄 非流式请求测试...")
        for round_num in range(args.rounds):
            if args.concurrent > 1:
                results = await run_concurrent_test(client, prompt_type, False, args.concurrent)
            else:
                result = await run_test_round(client, prompt_type, False)
                results = [result]
            
            all_results["non_stream"].extend(results)
            
            for r in results:
                status = "✓" if r.success else "✗"
                print(f"  Round {round_num + 1}: {status} {r.total_time:.3f}s (tokens: {r.tokens})")
            
            # 间隔避免限速
            await asyncio.sleep(0.5)
        
        # 测试流式
        print("\n🌊 流式请求测试...")
        for round_num in range(args.rounds):
            if args.concurrent > 1:
                results = await run_concurrent_test(client, prompt_type, True, args.concurrent)
            else:
                result = await run_test_round(client, prompt_type, True)
                results = [result]
            
            all_results["stream"].extend(results)
            
            for r in results:
                status = "✓" if r.success else "✗"
                print(f"  Round {round_num + 1}: {status} TTFB={r.ttfb:.3f}s, Total={r.total_time:.3f}s")
            
            await asyncio.sleep(0.5)
    
    # 打印汇总统计
    print_statistics(all_results["non_stream"], "非流式请求 (stream=false)")
    print_statistics(all_results["stream"], "流式请求 (stream=true)")
    
    # 对比分析
    print(f"\n{'='*60}")
    print("📈 流式 vs 非流式 对比分析")
    print(f"{'='*60}")
    
    non_stream_success = [r for r in all_results["non_stream"] if r.success]
    stream_success = [r for r in all_results["stream"] if r.success]
    
    if non_stream_success and stream_success:
        ns_avg = statistics.mean([r.total_time for r in non_stream_success])
        s_avg = statistics.mean([r.total_time for r in stream_success])
        s_ttfb_avg = statistics.mean([r.ttfb for r in stream_success])
        
        print(f"\n非流式平均总时间: {ns_avg:.3f}s")
        print(f"流式平均总时间: {s_avg:.3f}s")
        print(f"流式平均首字节: {s_ttfb_avg:.3f}s")
        
        diff_pct = ((s_avg - ns_avg) / ns_avg) * 100 if ns_avg > 0 else 0
        if diff_pct > 0:
            print(f"\n⚠️ 流式比非流式慢 {diff_pct:.1f}%")
        else:
            print(f"\n✓ 流式比非流式快 {abs(diff_pct):.1f}%")
        
        ttfb_benefit = ((ns_avg - s_ttfb_avg) / ns_avg) * 100 if ns_avg > 0 else 0
        print(f"💡 流式 TTFB 优势: 比非流式提前 {ttfb_benefit:.1f}% 获得首字节")
        
        print("\n" + "="*60)
        print("📌 结论")
        print("="*60)
        if s_ttfb_avg < ns_avg * 0.5:
            print("✓ 流式模式的 TTFB 优势明显，适合需要快速响应的场景")
        if abs(diff_pct) < 10:
            print("✓ 流式和非流式总时间差异不大，可根据业务需求选择")
        if diff_pct > 20:
            print("⚠️ 流式模式总时间明显更长，可能是网络延迟或 chunk 处理开销")
        
        print("\n💡 对于 GraphRAG:")
        print("   - 实体提取/摘要生成: 推荐非流式 (需完整响应进行解析)")
        print("   - 对话/QA: 可使用流式 (改善用户体验)")
    
    print("\n测试完成！")


if __name__ == "__main__":
    asyncio.run(main())
