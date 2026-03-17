# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
交互式配置生成器：基于workflow和RAG逻辑，动态查找数据库生成配置或功能解释

功能：
1. 交互式输入需求
2. 基于RAG检索相关文档
3. 生成配置命令或功能解释
4. 支持多轮对话
"""
import json
import logging
import re
import sys
from pathlib import Path
from typing import Dict, Any, Optional

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from INAGENT.utils import env_utils
from INAGENT.utils.index_utils import load_function_structure_index
from INAGENT.workflow_config_generator import (
    initialize_rag_system,
    initialize_llm_model,
    process_job,
    _documents_to_elements,
)
# 尝试导入_adaptive_rag_retrieval
try:
    from INAGENT.rag.fallback_retrieval import _adaptive_rag_retrieval
except ImportError:
    try:
        # 尝试从workflow_config_generator导入（如果它导入了）
        from INAGENT.workflow_config_generator import _adaptive_rag_retrieval
    except ImportError:
        # 如果都不存在，定义一个简单的实现
        def _adaptive_rag_retrieval(hybrid_retriever, reranker, job_content, decomposition_result=None, **kwargs):
            """简单的RAG检索实现（当fallback_retrieval不可用时）"""
            top_k_retrieval = kwargs.get("top_k_retrieval", 10)
            top_k_rerank = kwargs.get("top_k_rerank", 5)
            
            # 使用hybrid_retriever进行检索
            try:
                # 尝试使用query方法
                if hasattr(hybrid_retriever, 'query'):
                    retrieved_docs = hybrid_retriever.query(job_content, top_k=top_k_retrieval)
                elif hasattr(hybrid_retriever, 'retrieve'):
                    retrieved_docs = hybrid_retriever.retrieve(job_content, top_k=top_k_retrieval)
                else:
                    retrieved_docs = []
            except Exception as e:
                logger.warning(f"RAG检索失败: {e}")
                retrieved_docs = []
            
            # 提取文本内容
            texts = []
            for doc in retrieved_docs:
                if isinstance(doc, dict):
                    text = doc.get("text", "") or doc.get("page_content", "") or doc.get("content", "")
                else:
                    text = str(doc)
                if text:
                    texts.append(text[:500])  # 限制每个片段长度
            
            context = "\n\n".join(texts[:top_k_rerank])
            
            return context, {"retrieved_snippets": texts[:top_k_rerank]}, decomposition_result or {}
from INAGENT.agents.lb_ops_agent import (
    build_lb_ops_agent,
    build_lb_ops_prompt,
)
from camel.agents import ChatAgent
from camel.messages import BaseMessage

# 加载环境变量
env_utils.load_inagent_env()

logger = logging.getLogger(__name__)


def format_config_output(result: Dict[str, Any]) -> str:
    """格式化配置输出"""
    output = []
    output.append("=" * 80)
    output.append("配置生成结果")
    output.append("=" * 80)
    output.append("")
    
    if result.get("config_commands"):
        output.append("【配置命令】")
        output.append("-" * 80)
        for i, cmd in enumerate(result["config_commands"], 1):
            output.append(f"{i}. {cmd}")
        output.append("")
    
    if result.get("verify_commands"):
        output.append("【验证命令】")
        output.append("-" * 80)
        for i, cmd in enumerate(result["verify_commands"], 1):
            output.append(f"{i}. {cmd}")
        output.append("")
    
    if result.get("notes"):
        output.append("【注意事项】")
        output.append("-" * 80)
        output.append(result["notes"])
        output.append("")
    
    if result.get("decomposition_result"):
        decomp = result["decomposition_result"]
        output.append("【任务分解结果】")
        output.append("-" * 80)
        if decomp.get("scenario_id"):
            output.append(f"场景ID: {decomp['scenario_id']}")
        if decomp.get("product_modules"):
            output.append(f"产品模块: {', '.join(decomp['product_modules'])}")
        if decomp.get("protocol_type"):
            output.append(f"协议类型: {decomp['protocol_type']}")
        if decomp.get("required_steps"):
            output.append(f"必需步骤: {', '.join(decomp['required_steps'])}")
        if decomp.get("advanced_features"):
            output.append(f"高级功能: {', '.join(decomp['advanced_features'])}")
        output.append("")
    
    return "\n".join(output)


def format_explanation_output(result: Dict[str, Any]) -> str:
    """格式化功能解释输出"""
    output = []
    output.append("=" * 80)
    output.append("功能解释")
    output.append("=" * 80)
    output.append("")
    
    if result.get("explanation"):
        output.append(result["explanation"])
        output.append("")
    
    if result.get("related_docs"):
        output.append("【相关文档片段】")
        output.append("-" * 80)
        for i, doc in enumerate(result["related_docs"][:5], 1):  # 只显示前5个
            output.append(f"\n片段 {i}:")
            output.append(doc[:300] + "..." if len(doc) > 300 else doc)
        output.append("")
    
    if result.get("config_commands"):
        output.append("【相关配置命令示例】")
        output.append("-" * 80)
        for i, cmd in enumerate(result["config_commands"][:5], 1):  # 只显示前5个
            output.append(f"{i}. {cmd}")
        output.append("")
    
    return "\n".join(output)


def generate_config(job_content: str, hybrid_retriever, reranker, model, function_index: Dict[str, Any]) -> Dict[str, Any]:
    """生成配置命令"""
    try:
        result = process_job(job_content, hybrid_retriever, reranker, model, function_index)
        # 适配process_job返回的格式
        if isinstance(result, dict):
            # 提取配置结果
            config_result = result.get("config", {})
            decomposition_result = result.get("decomposition", {})
            
            return {
                "config_commands": config_result.get("config_commands", []),
                "verify_commands": config_result.get("verify_commands", []),
                "notes": config_result.get("notes", ""),
                "decomposition_result": decomposition_result,
            }
        return result
    except Exception as e:
        logger.error(f"生成配置失败: {e}", exc_info=True)
        return {"error": str(e)}


def generate_explanation(query: str, hybrid_retriever, reranker, model) -> Dict[str, Any]:
    """生成功能解释"""
    try:
        # 使用RAG检索相关文档
        context, retrieval_info, _ = _adaptive_rag_retrieval(
            hybrid_retriever=hybrid_retriever,
            reranker=reranker,
            job_content=query,
            top_k_retrieval=10,
            top_k_rerank=5,
            max_snippet_chars=500,
            max_context_chars=2000,
        )
        
        # 提取检索到的文档片段
        retrieved_snippets = []
        if isinstance(retrieval_info, dict):
            retrieved_snippets = retrieval_info.get("retrieved_snippets", [])
        
        # 如果retrieved_snippets为空，从context中提取
        if not retrieved_snippets and isinstance(context, str):
            # 如果context是字符串，按段落分割
            snippets = context.split("\n\n")
            retrieved_snippets = [s[:500] for s in snippets if s.strip()][:5]
        
        # 构建解释Agent
        explanation_prompt = f"""请基于以下文档内容，详细解释用户询问的功能或概念。

[用户问题]
{query}

[相关文档内容]
{context}

[要求]
1. 提供清晰、准确的功能解释
2. 如果文档中包含配置示例，可以列出相关命令
3. 如果文档不完整，请说明并建议如何获取更多信息
4. 使用中文回答

请输出详细的功能解释。"""
        
        agent = ChatAgent(
            system_message=BaseMessage.make_assistant_message(
                role_name="技术文档专家",
                content=(
                    "你是技术文档专家，擅长解释负载均衡器相关的功能和配置。"
                    "基于提供的文档内容，为用户提供清晰、准确的功能解释。"
                ),
            ),
            model=model,
        )
        
        response = agent.step(explanation_prompt)
        explanation = response.msgs[0].content if response.msgs else ""
        
        return {
            "explanation": explanation,
            "related_docs": retrieved_snippets,
            "config_commands": [],
        }
    except Exception as e:
        logger.error(f"生成解释失败: {e}", exc_info=True)
        return {"error": str(e)}


def _run_knowledge_router_mode(query, model, hybrid_retriever, reranker, mode):
    """使用知识路由器执行测试用例编写或评审。"""
    from INAGENT.rag.knowledge_router import KnowledgeRouter
    from INAGENT.rag.cli_reference import CLIReferenceRetriever
    from INAGENT.rag.test_rules import TestRulesEngine

    kr = KnowledgeRouter(
        cli_retriever=CLIReferenceRetriever(),
        rules_engine=TestRulesEngine(),
        hybrid_retriever=hybrid_retriever,
        reranker=reranker,
    )
    retrieval = kr.retrieve(query, mode=mode, max_context_chars=8000)
    context = retrieval.get("context", "")
    layers = retrieval.get("layers_used", [])
    logger.info("[%s] 使用知识层: %s", mode, layers)

    if mode == "test_write":
        sys_prompt = (
            "你是 NSAE (InfosecOS) 负载均衡器的测试用例编写专家。\n"
            "根据测试规范、CLI 参考和产品知识，为指定功能编写完整的测试用例。\n"
            "必须覆盖 Configuration/Boundary/Negative/Functional 等测试类型，\n"
            "CLI 命令语法必须准确。使用 Markdown 表格输出。"
        )
        user_prompt = f"[测试需求]\n{query}\n\n{context}\n\n请编写完整的测试用例集。"
    else:
        sys_prompt = (
            "你是 NSAE (InfosecOS) 负载均衡器的测试用例评审专家。\n"
            "对测试用例进行规范合规性、覆盖度和质量评审。\n"
            "按评审检查清单逐项检查，标注 PASS/WARN/FAIL 并给出改进建议。"
        )
        user_prompt = f"[待评审测试用例]\n{query}\n\n{context}\n\n请进行完整评审。"

    agent = ChatAgent(
        system_message=BaseMessage.make_assistant_message(
            role_name="测试专家",
            content=sys_prompt,
        ),
        model=model,
    )
    response = agent.step(user_prompt)
    return response.msgs[0].content if response.msgs else "无法生成结果"


def main():
    """主函数：交互式配置生成"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    
    print("=" * 80)
    print("INAGENT 交互式配置生成器")
    print("=" * 80)
    print("")
    print("功能说明:")
    print("  1. 生成配置：输入配置需求，系统会生成对应的配置命令")
    print("  2. 功能解释：输入功能问题，系统会基于文档提供详细解释")
    print("  3. 编写测试用例：输入测试需求，系统会基于规范和文档生成测试用例")
    print("  4. 评审测试用例：输入测试用例，系统会进行规范性和质量评审")
    print("")
    
    # 检查数据库状态
    index_path = Path(__file__).parent / "knowledge_base" / "function_structure_index.json"
    kb_path = Path(__file__).parent / "knowledge_base" / "reference" / "knowledge_base.json"
    
    if not kb_path.exists():
        print("[错误] 知识库不存在，请先运行功能1初始化数据库")
        print(f"  知识库路径: {kb_path}")
        return 1
    
    if not index_path.exists():
        print("[警告] 功能结构索引不存在，某些功能可能受限")
        print(f"  索引路径: {index_path}")
        function_index = {}
    else:
        function_index = load_function_structure_index(index_path)
        print(f"[信息] 已加载功能结构索引: {len(function_index.get('scenarios', {}))} 个场景")
    
    # 初始化RAG系统
    print("")
    print("正在初始化RAG系统...")
    try:
        hybrid_retriever, reranker = initialize_rag_system()
        print("[成功] RAG系统初始化完成")
    except Exception as e:
        print(f"[错误] RAG系统初始化失败: {e}")
        print("请检查:")
        print("  1. 知识库文件是否存在")
        print("  2. .env文件中的API配置是否正确")
        return 1
    
    # 初始化LLM模型
    print("正在初始化LLM模型...")
    try:
        model = initialize_llm_model()
        print("[成功] LLM模型初始化完成")
    except Exception as e:
        print(f"[错误] LLM模型初始化失败: {e}")
        return 1
    
    print("")
    print("=" * 80)
    print("系统就绪，可以开始交互")
    print("=" * 80)
    print("")
    
    # 交互式循环
    while True:
        print("请选择操作模式:")
        print("  [1] 生成配置命令")
        print("  [2] 功能解释/查询")
        print("  [3] 编写测试用例")
        print("  [4] 评审测试用例")
        print("  [q] 退出")
        print("")
        mode = input("请输入选项 (1/2/3/4/q): ").strip().lower()
        
        if mode == 'q':
            print("")
            print("感谢使用，再见！")
            break
        
        if mode not in ['1', '2', '3', '4']:
            print("[错误] 无效的选项，请重新选择")
            print("")
            continue
        
        print("")
        if mode == '1':
            print("【生成配置模式】")
            print("请输入您的配置需求（例如：如何配置HTTP类型的SLB服务）")
            print("输入 'back' 返回主菜单")
            print("")
            query = input("需求: ").strip()
            
            if query.lower() == 'back':
                print("")
                continue
            
            if not query:
                print("[错误] 需求不能为空")
                print("")
                continue
            
            print("")
            print("正在生成配置，请稍候...")
            print("")
            
            result = generate_config(query, hybrid_retriever, reranker, model, function_index)
            
            if result.get("error"):
                print(f"[错误] {result['error']}")
            else:
                print(format_config_output(result))
            
            # 询问是否保存
            save = input("是否保存结果到文件？(y/n): ").strip().lower()
            if save == 'y':
                output_dir = Path(__file__).parent / "reports"
                output_dir.mkdir(parents=True, exist_ok=True)
                import time
                timestamp = time.strftime("%Y%m%d_%H%M%S")
                output_file = output_dir / f"interactive_config_{timestamp}.json"
                with open(output_file, "w", encoding="utf-8") as f:
                    json.dump(result, f, ensure_ascii=False, indent=2)
                print(f"[成功] 结果已保存到: {output_file}")
        
        elif mode == '2':
            print("【功能解释模式】")
            print("请输入您的问题（例如：什么是会话保持？如何配置健康检查？）")
            print("输入 'back' 返回主菜单")
            print("")
            query = input("问题: ").strip()
            
            if query.lower() == 'back':
                print("")
                continue
            
            if not query:
                print("[错误] 问题不能为空")
                print("")
                continue
            
            print("")
            print("正在检索文档并生成解释，请稍候...")
            print("")
            
            result = generate_explanation(query, hybrid_retriever, reranker, model)
            
            if result.get("error"):
                print(f"[错误] {result['error']}")
            else:
                print(format_explanation_output(result))

        elif mode == '3':
            print("【测试用例编写模式】")
            print("请输入测试需求（例如：为 http2 settings headertablesize 编写测试用例）")
            print("输入 'back' 返回主菜单")
            print("")
            query = input("测试需求: ").strip()

            if query.lower() == 'back':
                print("")
                continue

            if not query:
                print("[错误] 测试需求不能为空")
                print("")
                continue

            print("")
            print("正在检索规范和文档，生成测试用例...")
            print("")

            result = _run_knowledge_router_mode(query, model, hybrid_retriever, reranker, "test_write")
            print(result)

            save = input("是否保存结果到文件？(y/n): ").strip().lower()
            if save == 'y':
                output_dir = Path(__file__).parent / "reports"
                output_dir.mkdir(parents=True, exist_ok=True)
                import time as _time
                timestamp = _time.strftime("%Y%m%d_%H%M%S")
                output_file = output_dir / f"test_cases_{timestamp}.md"
                output_file.write_text(result, encoding="utf-8")
                print(f"[成功] 结果已保存到: {output_file}")

        elif mode == '4':
            print("【测试用例评审模式】")
            print("请粘贴要评审的测试用例（或输入包含测试用例的文件路径）")
            print("输入 'back' 返回主菜单")
            print("")
            query = input("测试用例: ").strip()

            if query.lower() == 'back':
                print("")
                continue

            if not query:
                print("[错误] 输入不能为空")
                print("")
                continue

            # 如果输入是文件路径，尝试读取文件内容
            if Path(query).exists():
                try:
                    query = Path(query).read_text(encoding="utf-8")
                    print(f"[信息] 已读取文件，{len(query)} 字符")
                except Exception as e:
                    print(f"[错误] 读取文件失败: {e}")
                    print("")
                    continue

            print("")
            print("正在加载评审标准，进行用例评审...")
            print("")

            result = _run_knowledge_router_mode(query, model, hybrid_retriever, reranker, "test_review")
            print(result)
        
        print("")
        print("-" * 80)
        print("")
    
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n\n用户中断操作")
        sys.exit(0)
    except Exception as e:
        logger.error(f"程序异常: {e}", exc_info=True)
        sys.exit(1)
