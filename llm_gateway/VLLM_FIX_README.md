### LLM Gateway 配置修复说明

## 问题诊断
Gateway返回500错误 "All models failed to respond" 是因为：
- config.yaml 中只配置了SiliconFlow provider
- 本地vLLM服务（运行在8000端口）没有在Gateway配置中注册
- 当请求 THUDM/GLM-Z1-9B-0414 时，Gateway无法找到可用的provider

## 已完成的修复
✅ 在 `llm_gateway/config.yaml` 中添加了 vLLM provider 配置：
   - 启用了 vllm provider (enabled: true)
   - 配置了本地地址: http://127.0.0.1:8000/v1
   - 注册了两个chat模型:
     * THUDM/GLM-Z1-9B-0414 (id: glm-z1-9b-vllm)
     * Qwen/Qwen3-8B (id: qwen3-8b-vllm)

## 重启Gateway步骤

### 方法1：使用start_gateway.bat（推荐）
```powershell
cd C:\SynologyDrive\INFOAGEN\llm_gateway
.\start_gateway.bat
```

### 方法2：手动重启
```powershell
# 1. 停止当前Gateway进程
taskkill /F /PID 12076  # 或查找当前PID: netstat -ano | findstr ":9000"

# 2. 等待端口释放（约2秒）
Start-Sleep -Seconds 2

# 3. 启动新Gateway
cd C:\SynologyDrive\INFOAGEN\llm_gateway
.\start_gateway.bat
```

## 验证配置

运行测试脚本验证vLLM集成：
```powershell
cd C:\SynologyDrive\INFOAGEN\llm_gateway
python test_vllm_integration.py
```

预期输出：
```
Testing Gateway at: http://127.0.0.1:9000/v1
============================================================

1. Listing available models...
   Found X models:
   - glm-z1-9b-vllm
   - qwen3-8b-vllm
   - ... (其他SiliconFlow模型)

2. Testing chat completion with THUDM/GLM-Z1-9B-0414...
   Response: 你好！我是...
   Model used: THUDM/GLM-Z1-9B-0414
   Tokens: XX

============================================================
✅ Gateway vLLM integration test PASSED
```

## 重新运行向量生成

Gateway重启成功后，重新运行：
```powershell
cd C:\SynologyDrive\INFOAGEN
.\run_inagent_pipeline.bat
```

应该能看到：
- ✅ vLLM预检成功
- ✅ LLM Gateway正常响应
- ✅ 向量生成完成

## 配置说明

### config.yaml 新增内容
```yaml
  # Local vLLM Service
  vllm:
    name: "vLLM Local"
    enabled: true
    base_url: "http://127.0.0.1:8000/v1"
    api_key: "EMPTY"  # vLLM不需要API key
    
    limits:
      rpm: 10000      # 本地服务，设置较高限额
      tpm: 1000000
    
    chat_models:
      - id: "glm-z1-9b-vllm"
        model: "THUDM/GLM-Z1-9B-0414"
        enabled: true
        limits:
          rpm: 5000
          tpm: 500000
        description: "GLM-Z1 9B model via vLLM"
```

### Calling Mode 说明
当前配置 `calling_mode: "race"`：
- Gateway会并发调用所有可用provider（SiliconFlow + vLLM）
- 返回最快的响应
- 提高了响应速度和可靠性

其他可选模式：
- `balance`: 轮询负载均衡
- `hybrid`: 智能选择（基于延迟和成功率）

## 故障排查

### 如果测试脚本失败
1. 检查vLLM服务是否运行：
   ```powershell
   curl http://127.0.0.1:8000/v1/models
   ```

2. 检查Gateway日志：
   ```powershell
   Get-Content C:\SynologyDrive\INFOAGEN\llm_gateway\logs\gateway_*.log -Tail 50
   ```

3. 检查Gateway是否加载了新配置：
   查看日志中是否有 "Loading provider: vllm" 字样

### 如果Gateway无法启动
检查config.yaml语法：
```powershell
python -c "import yaml; yaml.safe_load(open('C:\\SynologyDrive\\INFOAGEN\\llm_gateway\\config.yaml'))"
```

## 下一步

Gateway配置完成后，整个流程应该是：
1. ✅ vLLM服务在8000端口
2. ✅ Gateway在9000端口，整合了vLLM和SiliconFlow
3. ✅ INAGENT通过Gateway统一调用LLM
4. ✅ 向量生成使用本地vLLM（更快、更稳定）
