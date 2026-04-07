# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
[DEPRECATED] 文档分类器 — 已被 knowledge_linker.py 取代。

此文件仅保留 classify_document / infer_product_module_from_path 签名
作为过渡期兼容垫片，供尚未迁移的调用方使用。
新代码请直接使用 knowledge_linker.farmer_link / owner_decide。
"""
import logging
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

_VALID_CATEGORIES = {
    "cli/reference", "app/reference",
    "architecture/design",
    "spec/func_spec", "spec/prd", "spec/design",
    "test/test_list", "test/test_strategy", "test/test_template",
    "review/bug_fix", "review/rules",
}

_LLM_PROMPT = """你是一个文档分类助手。根据以下文档文件名和内容片段，判断这份文档属于哪个分类。

文件名：{filename}

内容片段（前2000字）：
{content_preview}

请从以下分类中选择最合适的一个（只输出分类值，不要解释）：
- cli/reference：CLI命令参考文档
- app/reference：产品功能说明文档
- architecture/design：产品架构设计文档
- spec/func_spec：软件功能规格书
- spec/prd：产品需求文档
- spec/design：软件设计文档
- test/test_list：测试用例列表
- test/test_strategy：测试策略/计划
- test/test_template：测试用例模板
- review/bug_fix：Bug修复记录
- review/rules：评审规则/标准
- unknown：无法判断

只输出分类值，例如：cli/reference"""


def _llm_classify(filename: str, content_preview: str) -> str:
    if not content_preview or len(content_preview.strip()) < 100:
        return "unknown"
    try:
        from INAGENT.utils.llm_config import get_gateway_config
        from openai import OpenAI
        cfg = get_gateway_config()
        api_key = cfg.get("api_key", "")
        base_url = cfg.get("base_url", "")
        model = cfg.get("chat_model", "")
        if not all([api_key, base_url, model]):
            return "unknown"
        client = OpenAI(api_key=api_key, base_url=base_url)
        prompt = _LLM_PROMPT.format(filename=filename, content_preview=content_preview[:2000])
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=20,
            temperature=0.0,
        )
        raw = resp.choices[0].message.content.strip().lower()
        for cat in _VALID_CATEGORIES:
            if cat in raw:
                logger.info("[classify] LLM 判断: %s -> %s", filename, cat)
                return cat
        return "unknown"
    except Exception as e:
        logger.warning("[classify] LLM 分类失败 %s: %s", filename, e)
        return "unknown"


def classify_document(
    file_path: Path,
    content_preview: str = "",
) -> Tuple[str, float]:
    """[DEPRECATED] 直接用 LLM 判断，返回 (category, confidence)。"""
    cat = _llm_classify(file_path.name, content_preview)
    if cat != "unknown":
        return cat, 0.8
    return "unknown", 0.0


def infer_product_module_from_path(file_path: Path) -> Optional[str]:
    """[DEPRECATED] 返回 None，模块推断已移至 knowledge_linker。"""
    return None
