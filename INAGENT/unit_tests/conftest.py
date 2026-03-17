# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
pytest 配置和共享 fixtures
"""
import pytest
import json
import tempfile
from pathlib import Path
from typing import Dict, Any


@pytest.fixture
def temp_dir():
    """创建临时目录"""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def sample_function_index():
    """示例功能结构索引"""
    return {
        "version": "2.0",
        "source": "auto_extracted",
        "metadata_statistics": {
            "product_modules": {
                "SLB": {
                    "count": 100,
                    "keywords": ["slb", "负载均衡", "服务器负载均衡"],
                    "step_types": {"backend_servers": 20, "virtual_services": 15}
                },
                "LLB": {
                    "count": 50,
                    "keywords": ["llb", "链路负载均衡"],
                    "step_types": {}
                }
            },
            "protocol_types": {
                "HTTP": {
                    "count": 80,
                    "keywords": ["http", "web"]
                },
                "TCP": {
                    "count": 60,
                    "keywords": ["tcp"]
                }
            },
            "step_types": {
                "backend_servers": {
                    "count": 40,
                    "keywords": ["real server", "服务组", "slb real"]
                },
                "virtual_services": {
                    "count": 35,
                    "keywords": ["virtual service", "虚拟服务", "slb virtual"]
                },
                "health_checks": {
                    "count": 30,
                    "keywords": ["health check", "健康检查", "monitor"]
                }
            }
        },
        "modules": {
            "SLB": {
                "module_name": "SLB",
                "keywords": ["slb", "负载均衡"],
                "step_types": {},
                "cli_commands": []
            }
        },
        "scenarios": {
            "SLB_HTTP_FULL_CONFIG": {
                "scenario_id": "SLB_HTTP_FULL_CONFIG",
                "product_modules": ["SLB"],
                "protocol_types": ["HTTP"],
                "required_steps": ["backend_servers", "virtual_services", "health_checks"]
            }
        }
    }


@pytest.fixture
def sample_knowledge_blocks():
    """示例知识库块"""
    return [
        {
            "page_content": "SLB HTTP 健康检查配置示例",
            "metadata": {
                "product_module": "SLB",
                "protocol_type": ["HTTP"],
                "step_type": "health_checks",
                "scenario_id": "SLB_HTTP_FULL_CONFIG",
                "source_file": "app.json",
                "page_idx": 1,
                "block_id": 1
            }
        },
        {
            "page_content": "配置 slb real server",
            "metadata": {
                "product_module": "SLB",
                "step_type": "backend_servers",
                "source_file": "cli.json",
                "page_idx": 2,
                "block_id": 2
            }
        }
    ]


@pytest.fixture
def sample_decomposition_result():
    """示例任务分解结果"""
    return {
        "scenario_id": "SLB_HTTP_FULL_CONFIG",
        "product_modules": ["SLB"],
        "protocol_type": ["HTTP"],
        "required_steps": ["backend_servers", "virtual_services", "health_checks"],
        "advanced_features": ["cookie_persistence"],
        "rag_queries": [
            {
                "step_type": "backend_servers",
                "query": "SLB HTTP 后台服务配置示例",
                "priority": 1
            },
            {
                "step_type": "health_checks",
                "query": "SLB HTTP 健康检查配置示例",
                "priority": 1
            }
        ]
    }
