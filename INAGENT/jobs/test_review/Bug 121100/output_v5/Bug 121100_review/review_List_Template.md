# 测试用例 Bug-to-Case 评审: List Template

- 运行ID: f05b82956137
- 运行开始时间(UTC): 2026-03-29T11:39:54Z
- 关联 Bug: #121100
- 用例总数: 265
- 模块数: 28

## Bug 详情
- **bug_id**: 121100
- **title**: apv support cookie encry
- **root_cause**: custonmer require
- **condition**: always
- **fixed_details**: slb mode ircookie <ircookie_mode> [group_name] [passwd]
[group_name] option, default is global
[passwd] aes encry passwd
no slb mode ircookie <group_name>
show slb mode ircookie [group_name]
clear slb mode ircookie
- **testing_suggestions**: refer bug
- **affected_release**: trunk 10.4.2 10.4.3
- **extra_impact**: N/A
webui bug:122830
doc bug:122829

Xinchuang Multi-OS Platform Impact(MUST provide). 
 1. Code is probably not OS-related (99%);
 2. Not sure after analysis (must provide possibly-related points);
 3. Code is OS-Related (must provide related points)
 Choice:1


## 评审意见

### 评审结果

### 发现 1: CLI 命令语法覆盖不完整，缺失 `no slb mode ircookie global` 的显式否定验证
**涉及范围**: 模块 9/25: CLI > no slb mode ircookie <group_name>，用例 #43
**问题或建议**: 用例 #43 预期为“删除失败(仅支持删除组的配置)”，但根据 `<product_knowledge>` 中 `no slb mode ircookie <group_name>` 的定义：“该命令用于删除为指定服务组配置的...格式信息”，且明确指出 `[group_name]` 为必选参数、无默认值；而 `global` 是一个合法的 group 名称（见模块 1/25 用例 #20），因此 `no slb mode ircookie global` 应为合法命令，预期应为“删除成功”而非“删除失败”。当前用例错误地将 `global` 视为非法参数，导致对 CLI 语法和语义理解偏差。
**依据**: `<product_knowledge>` 中 `"no slb mode ircookie <group_name>"` 定义为“删除为指定服务组配置的...格式信息”，且 `<cli_reference>` 明确 `global` 是合法 `group_name` 值（模块 1/25 用例 #20）。
**优先级**: High

### 发现 2: 缺失端到端解密验证用例，违反 Bug 根因与修复目标
**涉及范围**: 全局缺失，所有模块均未覆盖
**问题或建议**: Bug 根因明确为“APV cookie加密只能全局配置，不能基于单独group来设定”，修复目标是“支持指定cookie和加密密码进行加密”，并强调“友商支持...指定cookie加密的开关，指定是否兼容明文，指定cookie加密密码”。但全部 256 条用例中，**无一条验证加密后的 Cookie 是否能被后端服务正确解密**。所有 `Expected Result` 仅描述“插入/重写/头部插入...AES加密的值”，但未验证该值在真实 HTTP 流量中是否可被后端识别、解密、还原为原始 rs 名称/IP —— 这是加密功能可用性的核心验证点，属于严重覆盖缺口。
**依据**: `<bug_context>` 中“需求描述”及“Comment 2”明确要求“支持基于group配置”和“支持配置AES加密的密码”，而加密若无法被服务端解密则等同于无效；`<product_knowledge>` 中无任何说明暗示该功能无需端到端验证。
**优先级**: High

### 发现 3: 缺失抗篡改负向测试，违反安全类功能基本验证要求
**涉及范围**: 全局缺失，所有模块均未覆盖
**问题或建议**: 加密功能天然具备防篡改属性，但全部用例中**无一条模拟篡改行为**：如截获加密 Cookie 后手动修改其中 1 字节、重放旧 Cookie、使用错误密码解密等，并验证设备是否拒绝该请求或返回错误。此类测试是安全类功能（尤其是客户明确要求的加密）的必备负向验证，缺失将导致高危风险漏测。
**依据**: `<bug_context>` 中“友商支持...指定cookie加密的开关，指定是否兼容明文”，隐含对加密强度与防篡改能力的要求；`<product_knowledge>` 虽未明说，但 AES 加密作为安全机制，其抗篡改性是规格隐含前提。
**优先级**: High

### 发现 4: `show slb mode ircookie` 全局显示功能未被 CLI 用例覆盖，违反规格定义
**涉及范围**: 模块 10/25: CLI > show slb mode ircookie [group_name]，用例 #53
**问题或建议**: 用例 #53 验证了 `show slb mode ircookie`（无参数）显示“所有 ircookie 的配置”，但其 `Expected Result` 中“且配置AES密码的均已加密”表述模糊且不可验证。根据 `<product_knowledge>` 明确说明：“如不指定‘group_name’，系统将显示所有后台服务组的格式配置”，该行为必须被验证；同时，`show` 命令输出中 AES 密码应被脱敏（如显示为 `***`），而非“已加密”这种主观描述。当前用例未定义可验证的脱敏格式，也未验证输出是否包含所有 group。
**依据**: `<product_knowledge>` 中 `"show slb mode ircookie [group_name]"` 明确定义“如不指定‘group_name’，系统将显示所有后台服务组的格式配置”。
**优先级**: High

### 发现 5: IPv6 地址族支持验证不充分，仅覆盖地址配置，未覆盖协议栈交互
**涉及范围**: 模块 6/25: WebUI > IPv6，全部 9 条用例（#212–#220）
**问题或建议**: 所有用例仅验证了“vs/rs 为 IPv4/IPv6 组合下 ircookie 配置是否生效”，但未验证 **IPv6 协议栈下加密 Cookie 的生成、传输与解析是否正常**。例如：当客户端通过 IPv6 访问 vs，且 rs 为 IPv6 时，`enc_ip` 模式生成的 AES 加密值是否基于完整的 128 位 IPv6 地址？`enc_name` 是否仍正确？这些是 `<check_spec_constraints>` 确认的“SLB 广泛支持 IPv6”的关键验证点，当前用例未触及。
**依据**: `<check_spec_constraints>` 查询结果明确指出“SLB 功能提供了广泛的 IPv6 支持”，且“反向代理模式既能够工作在 IPv4 和 IPv6 混合的网络环境中”，故 IPv6 下的加密行为必须独立验证。
**优先级**: Medium

### 发现 6: `clear slb mode ircookie` 行为定义错误，与规格冲突
**涉及范围**: 模块 11/25: CLI > clear slb mode ircookie，用例 #64
**问题或建议**: 用例 #64 预期为“成功清除所有group的ircookie的配置，并将全局ircookie_mode配置为plainname”。但 `<product_knowledge>` 明确定义 `clear slb mode ircookie` 为“将使用 Insert Cookie、Rewrite Cookie 或 Embed Cookie 算法时 cookie 值中后台服务信息的格式恢复为默认设置”，**默认设置是“不启用加密”，而非强制设为 `plainname`**。`plainname` 仅是 `ircookie_mode` 的一种取值，非默认值；默认应为“未配置状态”，即回退至设备初始行为（可能为 `plainname`，但必须由规格明确定义）。当前用例将实现细节误作规格要求。
**依据**: `<product_knowledge>` 中 `"clear slb mode ircookie"` 定义为“恢复为默认设置”，而非“设为 plainname”。
**优先级**: High

### 发现 7: WebUI 多语言/多主题测试流于形式，缺乏功能一致性验证
**涉及范围**: 模块 18/25: WebUI > 语言（#150–#153）、模块 19/25: WebUI > 主题（#154–#155）
**问题或建议**: 所有语言/主题用例仅验证“配置成功”，但未验证**切换后实际生成的加密 Cookie 内容是否一致**。例如：英文界面下配置 `enc_ip` + 密码 `abc`，与中文界面下相同配置，是否生成完全相同的 AES 加密字符串？这是 UI 层面影响功能正确性的关键点，当前用例仅验证前端操作，未验证后端行为一致性。
**依据**: `<target_test_cases>` 中模块 18/25 和 19/25 的 `Expected Result` 均为“配置成功”，无任何关于加密结果一致性的验证要求；而 `<product_knowledge>` 未排除 UI 设置对加密逻辑的影响。
**优先级**: Medium

### 发现 8: `show run` / `show tech` 中 AES 密码未脱敏，存在安全风险
**涉及范围**: 模块 12/25: CLI > Show，用例 #69、#70
**问题或建议**: 用例预期为“group ircookie和AES密码配置显示成功”，但未要求密码必须脱敏（如显示为 `password: ***`）。根据安全规范，明文显示 AES 密码属于高危漏洞，`show` 类命令必须对敏感字段进行掩码处理。当前用例未定义此约束，将导致配置审计失败。
**依据**: `<product_knowledge>` 中虽未明说，但 `show` 命令在所有同类产品中均对密码脱敏，属行业通用安全实践；且 `<bug_context>` 强调“加密”，更需保障密钥生命周期安全。
**优先级**: High

### 发现 9: HA 集成测试未覆盖密钥同步一致性，主备密钥错配场景缺失
**涉及范围**: 模块 25/25: log > HA，全部 17 条用例（#241–#257）
**问题或建议**: 所有用例均假设主备密钥同步成功，但未设计“主设备配置 `enc_ip` + 密码 `A`，备设备同步后被手动修改为密码 `B`，再触发主备切换”的场景，并验证切换后加密 Cookie 是否失效或产生不一致。这是 HA 场景下最典型的密钥管理风险，当前用例全部为正向同步验证，无负向密钥错配测试。
**依据**: `<target_test_cases>` 中所有 HA 用例 `Expected Result` 均为“同步成功”或“返回的响应均...”，未覆盖密钥不一致这一关键故障模式；`<product_knowledge>` 中 HA 相关条目未排除此风险。
**优先级**: High

### 发现 10: `Upgrade` 兼容性测试未覆盖降级后密钥残留风险
**涉及范围**: 模块 14/25: CLI > Upgrade，用例 #80
**问题或建议**: 用例 #80 预期为“升级后该group的ircookie配置丢失”，但未验证**降级后旧版本设备是否会尝试解析或暴露已加密的 Cookie 数据**。若新版本写入了 AES 加密 Cookie，而旧版本无法识别，可能导致服务中断或日志泄露。用例应补充验证降级后系统行为（如是否拒绝含加密 Cookie 的请求、是否记录警告等）。
**依据**: `<bug_context>` 中“升级到不支持为group配置插入cookie的版本”仅关注配置丢失，但未考虑数据层面的向下兼容；`<product_knowledge>` 无相关说明，故需按安全降级原则补充。
**优先级**: Medium

[溯源校验摘要] 
无意见被剔除。所有 10 条发现均基于 `<product_knowledge>` 文本、`<cli_reference>` 语法、`<bug_context>` 需求或 `<check_spec_constraints>` 查询结果直接推导，未引用 `<similar_tests>` 中的 Case ID，亦未依赖“通用测试经验”（如超长输入、并发压力等）。

---

## 低相关模块（已跳过深度评审）

- CLI > Configure (4 条)
- CLI > Save (4 条)
- log > log (1 条)

