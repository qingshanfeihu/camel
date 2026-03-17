# CAMEL-AI Codebase Guide for AI Agents

## Project Overview
CAMEL (Communicative Agents for "Mind" Exploration of Large Language Model Society) is a Python framework for building multi-agent systems. The project focuses on finding scaling laws of agents through large-scale agent simulation, data generation, and task automation.

## Architecture & Core Components

### Agent Hierarchy
All agents inherit from `BaseAgent` (abstract base in [`camel/agents/base.py`](../camel/agents/base.py)) with two required methods: `reset()` and `step()`.

- **ChatAgent** ([`camel/agents/chat_agent.py`](../camel/agents/chat_agent.py)): Core agent for LLM interactions with tool calling, memory, and streaming
- **Specialized Agents**: TaskSpecifyAgent, TaskPlannerAgent, CriticAgent, SearchAgent, MCPAgent, RepoAgent, etc. - all extend ChatAgent
- **ProgrammableChatAgent**: Base for agents with programmed instruction sequences

### Multi-Agent Systems
- **RolePlaying** ([`camel/societies/role_playing.py`](../camel/societies/role_playing.py)): Two-agent conversations with optional task specification, planning, and critic loops
- **Workforce** ([`camel/societies/workforce/`](../camel/societies/workforce/)): Hierarchical multi-agent task decomposition system

### Model Backends
Model abstraction via `BaseModelBackend` ([`camel/models/`](../camel/models/)) supports 50+ LLM providers. Create models using `ModelFactory`:
```python
from camel.models import ModelFactory
from camel.types import ModelPlatformType, ModelType

model = ModelFactory.create(
    model_platform=ModelPlatformType.OPENAI,
    model_type=ModelType.GPT_4O,
    model_config_dict={"temperature": 0.0}
)
```

### Toolkits & Function Calling
Toolkits ([`camel/toolkits/`](../camel/toolkits/)) provide structured tool integration:
- Use `@FunctionTool` decorator or inherit from `BaseToolkit`
- Tools are OpenAI-compatible function schemas
- Agent accepts `tools` parameter: `ChatAgent(model=model, tools=[search_tool])`

## Development Workflows

### Environment Setup
```bash
# Install editable with dev dependencies
make install-editable
pip install -e ".[dev]"

# Install optional feature groups
pip install "camel-ai[rag,web_tools,document_tools]"
```

### Code Quality
```bash
make format        # Auto-format with ruff + isort
make ruff          # Lint check
make mypy          # Type checking
make pre-commit    # Run all pre-commit hooks
```

### Testing
```bash
# Run tests (uses pytest)
pytest test/
pytest test/agents/  # Specific directory

# With coverage
pytest --cov=camel test/
```

Test files use `test_*.py` naming and `def test_*()` functions. Mock external dependencies when testing.

### Documentation
- Sphinx docs in [`docs/`](../docs/) with auto-generated API references
- Build: `cd docs && make html`
- Cookbooks live in Colab first, then converted to docs

## Project Conventions

### Imports & Dependencies
- Use absolute imports: `from camel.agents import ChatAgent`
- Decorator for optional dependencies: `@dependencies_required('module_name')`
- Decorator for API keys: `@api_keys_required([('param_name', 'ENV_VAR')])`
- API keys loaded from environment or `.env` file

### File Headers
Every Python file starts with CAMEL copyright header:
```python
# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
# Licensed under the Apache License, Version 2.0 (the "License")
# ...
```

### Type Annotations
- All public APIs must have type hints
- Use `typing` module: `Optional`, `Union`, `List`, `Dict`, etc.
- Return types required for functions/methods

### Module Structure
- `__init__.py` files export public API (see [`camel/__init__.py`](../camel/__init__.py))
- Keep large files modular - `chat_agent.py` is 5800 lines but well-organized
- Use `_types.py` for internal type definitions, `_utils.py` for helpers

### Examples
Place usage examples in [`examples/`](../examples/) organized by feature:
- `examples/agents/` - agent demonstrations
- `examples/models/` - model provider examples
- `examples/toolkits/` - toolkit usage
- `examples/usecases/` - end-to-end applications

## Key Design Patterns

### Stateful Agents
Agents maintain conversation history and memory. The `step()` method processes one interaction:
```python
response = agent.step("Your query")
print(response.msgs[0].content)
```

### Tool Integration
Tools must be callable functions or FunctionTool objects:
```python
from camel.toolkits import SearchToolkit
search_tool = SearchToolkit().search_duckduckgo
agent = ChatAgent(model=model, tools=[search_tool])
```

### Message System
Messages use `BaseMessage` ([`camel/messages/`](../camel/messages/)) with role, content, and metadata. Messages are immutable and support conversion between formats (OpenAI, Anthropic, etc.).

### Async Support
Many components support async operations. Use `async def` for I/O-bound operations and provide both sync/async versions when practical.

## Configuration

### Optional Dependencies
The project uses extensive optional dependency groups ([`pyproject.toml`](../pyproject.toml)):
- `dev` - development tools
- `rag` - retrieval/vector databases
- `web_tools` - search, scraping
- `document_tools` - PDF, DOCX parsing
- `communication_tools` - Slack, Discord, GitHub
- Install with: `pip install "camel-ai[group1,group2]"`

### Python Version
Requires Python 3.10-3.14 (specified in `pyproject.toml`)

## Common Pitfalls

1. **Missing Dependencies**: Always check if optional dependencies are installed before using features
2. **API Keys**: Set environment variables before running examples (OPENAI_API_KEY, etc.)
3. **Model Initialization**: Use ModelFactory, not direct model class instantiation
4. **Tool Format**: Tools must follow OpenAI function calling schema
5. **Memory Management**: For long conversations, consider clearing agent memory periodically

## Contributing

See [`CONTRIBUTING.md`](../CONTRIBUTING.md) for:
- Fork-and-Pull-Request workflow (community contributors)
- Checkout-and-Pull-Request workflow (org members)
- Cookbook writing in Google Colab using template
- Unit test requirements for all features

## Resources

- Documentation: https://docs.camel-ai.org
- Discord: https://discord.camel-ai.org/
- Paper: https://arxiv.org/abs/2303.17760
- Examples: https://github.com/camel-ai/camel/tree/master/examples
