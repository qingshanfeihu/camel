---
description: "生成 run_review.py 的调用命令。指定输入 Excel 文件或目录、输出目录、可选的 bug profile JSON，生成可直接运行的命令行。"
argument-hint: "输入文件路径 或 任务描述"
agent: agent
---

根据以下信息生成 `run_review.py` 的调用命令。

## 输入要求

请提供（未提供的项目我会从默认值推断）：

1. **输入源**：Excel 文件路径 或 目录路径（默认：`INAGENT/jobs/test_review/`）
2. **输出目录**：可选，默认 `INAGENT/reports/`
3. **指定 Sheet**：可选，需要评审的 Sheet 名称列表
4. **Bug Profile**：可选，JSON 文件路径（用于针对性评审）

## 命令生成规则

- 从 workspace 根目录运行（`c:\SynologyDrive\INFOAGEN`）
- 使用 `python -m INAGENT.run_review`（模块方式，确保包导入正确）
- 路径使用引号包裹（防止空格问题）

## 命令模板

**单文件评审**：
```powershell
python -m INAGENT.run_review --file "INAGENT/jobs/test_review/<文件名>.xlsx"
```

**目录批量评审**：
```powershell
python -m INAGENT.run_review --input-dir "INAGENT/jobs/test_review/<子目录>"
```

**指定 Sheet**：
```powershell
python -m INAGENT.run_review --file "<路径>" --sheets "Sheet1" "Sheet2"
```

**带 Bug Profile 的针对性评审**：
```powershell
python -m INAGENT.run_review --file "<路径>" --bug-profile "INAGENT/jobs/test_review/<bug>.json"
```

**指定输出目录**：
```powershell
python -m INAGENT.run_review --file "<路径>" --output-dir "INAGENT/reports/<子目录>"
```

## 执行前检查

#file:INAGENT/.env 中确认以下变量已配置：
- `LLM_GATEWAY_BASE_URL`
- `LLM_GATEWAY_API_KEY`
- `INAGENT_PRODUCT_NAME`（可选，有默认值）

然后列出 `INAGENT/jobs/test_review/` 下可用的输入文件，帮助用户确认路径。
