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
import json
import logging
import os
import re
import sys
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional
import socket
import subprocess
import time

# Add project root to sys.path to use local camel package
sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
)

from INAGENT.utils.env_utils import load_inagent_env

from camel.models import ModelFactory
from camel.toolkits import WebDeployToolkit
from camel.types import ModelPlatformType

from INAGENT.agents.env_setup_agent import (
    build_env_setup_agent,
    build_env_setup_prompt,
)

logger = logging.getLogger(__name__)


def _load_env() -> None:
    load_inagent_env()


def _build_model() -> Optional[object]:
    gateway_url = os.getenv("LLM_GATEWAY_BASE_URL")
    if not gateway_url:
        return None
    api_key = os.getenv("LLM_GATEWAY_API_KEY") or "local-gateway"
    if not gateway_url.rstrip("/").endswith("/v1"):
        gateway_url = f"{gateway_url.rstrip('/')}/v1"
    model_name = os.getenv("SILICONFLOW_MODEL_TYPE") or "Qwen/Qwen2.5-72B-Instruct"
    return ModelFactory.create(
        model_platform=ModelPlatformType.SILICONFLOW,
        model_type=model_name,
        api_key=api_key,
        url=gateway_url,
        model_config_dict={"temperature": 0.0},
    )


def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    # 1) 直接尝试解析整段文本（LLM 输出纯 JSON 时最高效）
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    # 2) 尝试 fenced code blocks
    candidates: list = []
    fenced = re.findall(r"```json\s*(.*?)\s*```", text, re.DOTALL)
    candidates.extend(fenced)

    # 3) 使用平衡括号法提取 JSON 对象（支持嵌套 {}）
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        depth = 0
        in_string = False
        escape_next = False
        for j in range(i, len(text)):
            c = text[j]
            if escape_next:
                escape_next = False
                continue
            if c == "\\":
                escape_next = True
                continue
            if c == '"' and not escape_next:
                in_string = not in_string
                continue
            if in_string:
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[i : j + 1])
                    break

    for raw in candidates:
        cleaned = raw.strip()
        if cleaned.startswith("{{") and cleaned.endswith("}}"):
            cleaned = cleaned[1:-1].strip()
        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue
    return None


def _load_job_content() -> str:
    jobs_dir = Path(__file__).parent.parent / "jobs"
    if not jobs_dir.exists():
        raise RuntimeError(f"Jobs directory not found: {jobs_dir}")
    job_files = sorted(jobs_dir.glob("*.txt"))
    if not job_files:
        raise RuntimeError(f"No job files found in {jobs_dir}")
    return job_files[0].read_text(encoding="utf-8").strip()


def _diagnose_http_server(port: int) -> None:
    logger.warning("Diagnosing http.server on port %s", port)
    logger.warning("Python executable: %s", sys.executable)
    process = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    start_time = time.time()
    listening = False
    while time.time() - start_time < 3:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                listening = True
                break
        except OSError:
            if process.poll() is not None:
                break
            time.sleep(0.1)

    process.terminate()
    stderr_output = ""
    if process.stderr is not None:
        try:
            _, stderr_bytes = process.communicate(timeout=2)
            if stderr_bytes:
                stderr_output = stderr_bytes.decode(errors="replace")
        except Exception:
            stderr_output = ""
    logger.warning(
        "Direct http.server listening=%s, returncode=%s, stderr=%s",
        listening,
        process.poll(),
        stderr_output.strip() or "(empty)",
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    _load_env()
    job_content = _load_job_content()
    model = _build_model()
    env_plan: Dict[str, Any] = {}
    if model is None:
        override_json = os.getenv("LB_ENV_PLAN_JSON")
        if not override_json:
            raise RuntimeError(
                "LLM_GATEWAY_BASE_URL not found. Set LLM_GATEWAY_BASE_URL "
                "(and LLM_GATEWAY_API_KEY if required) or provide "
                "LB_ENV_PLAN_JSON for a mock env plan."
            )
        env_plan = _extract_json_object(override_json) or {}
        if not env_plan:
            raise RuntimeError("LB_ENV_PLAN_JSON is invalid JSON.")
        logger.info("Using LB_ENV_PLAN_JSON override for env plan.")
    else:
        env_agent = build_env_setup_agent(model)
        prompt = build_env_setup_prompt(job_content)
        response = env_agent.step(prompt)
        payload = response.msg.content if response.msg else ""

        logger.info("Env agent raw response length: %s", len(payload))
        env_plan = _extract_json_object(payload) or {}
        if not env_plan:
            raise RuntimeError("Env agent did not return valid JSON.")

    html_content = env_plan.get("html_content") or (
        "<html><body><h1>chinamobile</h1></body></html>"
    )
    port = env_plan.get("port") or 8000
    try:
        port = int(port)
    except ValueError:
        port = 8000

    fallback_ports = [port, 8080, 18080, 8000]
    if port < 1024:
        fallback_ports = [8080, 18080, 8000, port]

    logger.info("Env agent proposed port: %s", port)
    toolkit = WebDeployToolkit()
    deploy_result: Dict[str, Any] = {}
    for candidate in fallback_ports:
        deploy_result = toolkit.deploy_html_content(
            html_content=html_content,
            port=candidate,
        )
        if deploy_result.get("success"):
            port = candidate
            break
        logger.warning(
            "Port %s failed: %s",
            candidate,
            deploy_result.get("error"),
        )

    if not deploy_result.get("success"):
        _diagnose_http_server(18081)
        raise RuntimeError(f"HTTP服务启动失败: {deploy_result.get('error')}")

    server_url = deploy_result.get("server_url")
    if not server_url:
        raise RuntimeError("server_url missing from deploy result.")

    logger.info("HTTP服务地址: %s", server_url)
    with urllib.request.urlopen(server_url, timeout=5) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        if "chinamobile" not in body:
            raise RuntimeError("响应内容不包含 chinamobile")

    logger.info("HTTP服务验证通过。")


if __name__ == "__main__":
    main()
