# 测试用例 Bug-to-Case 评审: List Template

- 运行ID: a3950c51e4bb
- 运行开始时间(UTC): 2026-03-28T02:56:31Z
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

### 发现 1: 缺失加密Cookie有效性验证（解密/篡改拒绝/过期）核心功能覆盖
**涉及范围**: 全局，未见任何 Functional 或 Integration 类型用例验证加密Cookie在真实HTTP流量中的实际行为
**问题或建议**: 当前所有用例仅验证CLI/WebUI配置是否成功，但未验证加密后的Cookie能否被设备正确解密、篡改后是否被拒绝、过期后是否失效。这是本次Bug修复的核心价值点，也是客户明确要求的“cookie加密”能力闭环验证。根据 bug_context 中“友商支持...指定cookie加密密码”及 product_knowledge 中“该命令用于配置...cookie值中后台服务信息的格式”，加密有效性是功能本质，非可选横切面。
**依据**: <bug_context> 中“需求描述：IT云项目中目前APV cookie加密只能全局配置，不能基于单独group来设定，需要支持指定cookie和加密密码进行加密”；<product_knowledge> 中“slb mode ircookie <ircookie_mode> [group_name] [password]”命令定义其用途为配置cookie值中后台服务信息的格式，而加密即为此格式的一种实现方式。
**优先级**: High

### 发现 2: CLI `no slb mode ircookie global` 命令预期结果与规格矛盾
**涉及范围**: 模块 2/25: CLI > no slb mode ircookie <group_name>，用例 #43
**问题或建议**: 用例 #43 预期结果为“删除失败(仅支持删除组的配置)”，但 <cli_reference> 明确指出 `no slb mode ircookie <group_name>` 的语法说明是“该命令用于删除为指定服务组配置的...格式信息”，且 `group_name` 参数在 fixed_details 中明确标注“[group_name] option, default is global”。这意味着 `no slb mode ircookie global` 是合法且应被支持的命令，其行为应为删除全局配置，而非“仅支持删除组的配置”。当前预期结果错误地限制了功能范围。
**依据**: <cli_reference> 中“no slb mode ircookie <group_name>”的官方说明；<bug_context> fixed_details 中“[group_name] option, default is global”。
**优先级**: High

### 发现 3: WebUI 全局配置模块存在严重功能缺失，与CLI能力不一致
**涉及范围**: 模块 12/25: WebUI > WebUI(global配置) 与 模块 13/25: WebUI > segment webui(global配置)
**问题或建议**: 模块 12 允许配置全局 `ircookie`，但模块 13 明确用例 #125 验证“配置失败，不支持配置全局ircookie”。这表明 WebUI 的 segment 界面与标准 WebUI 在全局配置能力上存在不一致。更严重的是，模块 12 的测试用例（#81-#100）全部针对 `enc_ip`/`enc_name` 等加密模式，但 <product_knowledge> 和 <bug_context> 均未提及全局加密是本次变更的一部分——变更焦点是“支持基于group配置”，全局配置本应保持原有行为（如 plainname/hexname/ip），新增全局加密属于过度设计且无规格支撑。
**依据**: <bug_context> Comment 2：“1.支持基于group配置slb mode ircookie；2.支持配置AES加密的密码”；<product_knowledge> 中 `slb mode ircookie` 命令文档未将全局加密列为新特性。
**优先级**: High

### 发现 4: 加密算法可选性（AES-128/AES-256）未被覆盖，且规格未定义
**涉及范围**: 全局，所有涉及 `passwd` 的用例（如 #23-#34, #107-#124 等）
**问题或建议**: 所有用例均假设 AES 是唯一且隐含的加密算法，未测试算法可选性（如 `slb mode ircookie enc_ip aes-128 <passwd>` vs `aes-256`）。虽然 <check_spec_constraints> 查询返回“ircookie是否支持AES-128/AES-256算法可选”无明确结果，但 <bug_context> 中“支持配置AES加密的密码”及 <product_knowledge> 中“[passwd] aes encry passwd”均使用单数“AES”，表明算法固定，无需可选。因此，当前用例未遗漏此点，但需在评审结论中明确：算法可选性非本次需求，不应作为覆盖缺口。
**依据**: <bug_context> fixed_details 中“[passwd] aes encry passwd”；<product_knowledge> 中“slb mode ircookie <ircookie_mode> [group_name] [password]”命令参数描述。
**优先级**: Medium

### 发现 5: IPv6 地址族支持已覆盖，但用例设计存在冗余
**涉及范围**: 模块 19/25: WebUI > IPv6，用例 #212-#220
**问题或建议**: <check_spec_constraints> 明确确认“ircookie配置是否支持IPv6地址族场景”的答案为“设备对SLB功能提供了广泛的IPv6支持”，且用例 #212-#220 已全面覆盖 vs/rs 的 IPv4/IPv6 组合。然而，这些用例全部集中在“修改ircookie模式”这一单一操作流上，缺乏对 IPv6 下加密Cookie有效性（发现1）的端到端验证，也未与 HA、HTTP 版本等其他横切面组合。建议将部分用例重组为“IPv6 + 加密Cookie有效性”组合场景，而非仅做模式切换。
**依据**: <check_spec_constraints> 查询结果；<discover_cross_cutting_concerns> 输出“地址族覆盖: 涉及 IPv4”，并推导出“测试是否同时覆盖了IPv4和IPv6场景？”。
**优先级**: Medium

### 发现 6: `show slb mode ircookie` 命令对 `global` 参数的处理逻辑未被验证
**涉及范围**: 模块 3/25: CLI > show slb mode ircookie [group_name]，用例 #54
**问题或建议**: 用例 #54 预期“参数为global | 显示失败”，但 <cli_reference> 中 `show slb mode ircookie [group_name]` 的说明是“如不指定‘group_name’，系统将显示所有后台服务组的格式配置”，并未排除 `global`。结合 fixed_details 中 `[group_name] option, default is global`，`show slb mode ircookie global` 应为合法命令，用于显示全局配置。当前用例错误地将其标记为失败场景，构成负面测试误判。
**依据**: <cli_reference> 中 `show slb mode ircookie [group_name]` 的官方说明；<bug_context> fixed_details 中“[group_name] option, default is global”。
**优先级**: High

### 发现 7: CLI `clear slb mode ircookie` 的预期结果与规格不符
**涉及范围**: 模块 9/25: CLI > clear slb mode ircookie（轻量检查），用例 #64
**问题或建议**: 用例 #64 预期“成功清除所有group的ircookie的配置，并将全局ircookie_mode配置为plainname”。但 <cli_reference> 中 `clear slb mode ircookie` 的说明是“该命令用于将使用Insert Cookie、Rewrite Cookie或Embed Cookie算法时cookie值中后台服务信息的格式恢复为默认设置”，并未提及“将全局ircookie_mode配置为plainname”。默认设置应为产品初始状态，而非硬编码为 `plainname`。此预期结果过度承诺，且与规格冲突。
**依据**: <cli_reference> 中 `clear slb mode ircookie` 的官方说明。
**优先级**: High

### 发现 8: WebUI 多语言/主题兼容性测试未覆盖加密密码字段的渲染与提交
**涉及范围**: 模块 14/25: WebUI > 语言（#150-#153）、模块 15/25: WebUI > 主题（#154-#155）
**问题或建议**: 这些用例仅验证“配置group的ircookie和AES密码”整体操作成功，但未验证在不同语言/主题下，AES密码输入框的渲染是否正常（如中文乱码、日文字符截断）、密码提交时是否被正确编码和传输。由于密码字段涉及敏感信息，其UI层健壮性至关重要。
**依据**: <discover_cross_cutting_concerns> 输出“WebUI接口覆盖: 该功能涉及WebUI管理接口。测试是否覆盖了WebUI配置场景（语言、主题、浏览器兼容性）？”；<target_test_cases> 中相关用例仅描述“配置成功”，未细化到字段级。
**优先级**: Medium

### 发现 9: HA 集成测试未覆盖主备切换时加密状态的同步一致性
**涉及范围**: 模块 24/25: log > HA，用例 #243-#257
**问题或建议**: 所有用例均验证“主备切换后，返回的响应均插入/重写/插入cookie值为...”，但未验证主设备上配置的AES密码是否被正确同步到备设备，以及备设备在接管后是否能用同一密码解密客户端携带的加密Cookie。这是HA场景下加密功能可用性的关键验证点，当前用例仅验证了配置同步，未验证加密状态同步。
**依据**: <discover_cross_cutting_concerns> 输出“高可用性: 功能涉及HA特性。测试是否覆盖了主备切换、故障恢复等HA场景？”；<bug_context> 中“友商支持...指定cookie加密密码”，暗示加密状态需在集群内一致。
**优先级**: High

### 发现 10: HTTP 版本兼容性测试未覆盖加密Cookie在不同版本下的解析差异
**涉及范围**: 模块 18/25: WebUI > http版本，用例 #206-#211
**问题或建议**: 这些用例仅验证“返回的响应均插入/重写/插入cookie值为rs的ip经过AES加密后的值”，但未验证HTTP/1.0、HTTP/1.1、HTTP/2 在处理 Set-Cookie 头部、Cookie 大小限制、字符编码等方面的差异是否影响加密Cookie的生成与传输。例如，HTTP/2 的头部压缩可能影响加密后Cookie的长度和格式。
**依据**: <discover_cross_cutting_concerns> 输出“协议覆盖: 涉及协议 DNS, FTP, HTTP, HTTPS, IP, RTSP, SIP, SNMP, TCP, UDP。测试是否覆盖了各协议版本（如HTTP 1.0/1.1/2.0）？”；<target_test_cases> 中相关用例预期结果过于笼统。
**优先级**: Medium

### 发现 11: 负面测试覆盖不足，缺少对 `passwd` 参数空格、Unicode、控制字符的验证
**涉及范围**: 所有涉及 `passwd` 的 Boundary/Negative 用例（如 #23-#34, #107-#124, #132-#149 等）
**问题或建议**: 当前 Negative 测试覆盖了空字符串、中文、超长字符串，但未覆盖常见安全边界：开头/结尾空格（`" abc "`）、Unicode零宽字符（U+200B）、ASCII控制字符（如 `\x00`, `\x0A`）。这些是密码字段常见的注入或解析异常点。
**依据**: <product_knowledge> 中 `slb mode ircookie` 命令参数 `[password]` 未定义具体字符集约束，故需按通用CLI安全规范覆盖；<target_test_cases> 中现有 Negative 用例未包含此类场景。
**优先级**: Medium

### 发现 12: `show run` / `show tech` 用例（#69-#70）未验证加密密码是否被脱敏显示
**涉及范围**: 模块 10/25: CLI > Show，用例 #69-#70
**问题或建议**: 预期结果仅为“group ircookie和AES密码配置显示成功”，但安全规范要求敏感信息（如AES密码）在 `show run` 等明文输出中必须被脱敏（如显示为 `******`）。未验证脱敏行为，存在安全合规风险。
**依据**: 安全基线要求；<target_test_cases> 中预期结果未体现脱敏要求。
**优先级**: High

### 发现 13: Stress 测试未覆盖加密密码高频轮换对CPU/内存的影响
**涉及范围**: 模块 25/25: Stress Testing > HA，用例 #258-#265
**问题或建议**: 这些用例均以“cpu使用率达到70%”为目标，但未监控内存占用、加密上下文创建/销毁频率、或密钥缓存命中率。AES加密是计算密集型操作，高频轮换密码可能导致密钥缓存失效、频繁重初始化，进而引发性能瓶颈。当前压力指标单一，不足以证明加密功能的稳定性。
**依据**: <discover_cross_cutting_concerns> 输出“高可用性: 功能涉及HA特性。测试是否覆盖了主备切换、故障恢复等HA场景？”；<target_test_cases> 中压力目标仅限CPU。
**优先级**: Medium

### 发现 14: `clear config` 系列用例（#71-#74）未验证全局与group配置的清除顺序和残留
**涉及范围**: 模块 11/25: CLI > Clear，用例 #71-#74
**问题或建议**: 预期结果均为“group ircookie和AES密码配置清除成功”，但未验证清除后 `show slb mode ircookie` 是否返回默认值（如 `plainname`），也未验证 `clear config factorydefault` 后，设备重启是否真正恢复出厂状态。清除操作的幂等性和彻底性是回归测试的关键。
**依据**: <cli_reference> 中 `clear config all` 等命令的说明；<target_test_cases> 中预期结果过于简单。
**优先级**: Medium

### 发现 15: 日志集成测试（#233）未验证AES密码在日志中的脱敏策略
**涉及范围**: 模块 21/25: log > log，用例 #233
**问题或建议**: 预期结果为“日志记录会对AES密码加密”，但日志系统通常不加密，而是脱敏（masking）。若日志真对密码加密，则无法审计；若未脱敏，则违反安全规范。此处预期结果表述错误且模糊，应明确为“日志中AES密码字段显示为`***`”。
**依据**: 安全日志最佳实践；<target_test_cases> 中预期结果不准确。
**优先级**: High

[溯源校验摘要]
被剔除意见1: “应测试超长输入”类建议 — 因 <product_knowledge> 未定义长度约束，且 <target_test_cases> 中 #27/#28/#33/#34/#111/#112 等已覆盖128/129字节边界，故不视为缺口。
被剔除意见2: “应测试并发连接”类建议 — <load_stress_analysis> 为空，且 <target_test_cases> 模块25已含Stress用例，故不额外提出。
被剔除意见3: 引用 <similar_tests> 中的 case118 — <similar_tests> 内容未提供具体ID，且 <target_test_cases> 中无 case118，属幻觉，已剔除。

---

## 低相关模块（已跳过深度评审）

- CLI > Configure (4 条)
- CLI > Save (4 条)
- CLI > Upgrade (2 条)

