# 测试用例 Bug-to-Case 评审: List Template

- 运行ID: 681621f010dd
- 运行开始时间(UTC): 2026-03-29T10:51:22Z
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

### 发现 1: show slb mode ircookie 命令元数据验证缺失
**涉及范围**: 模块 2/28: CLI > show slb mode ircookie [group_name]（用例 #53–#59）
**问题或建议**: 所有 `show slb mode ircookie` 用例的 Expected Result 均未验证加密状态、算法类型、密钥ID等关键元数据字段，仅笼统描述“显示成功”或“已加密”。根据 CLI 参考文档，该命令用于“显⽰指定的后台服务组使⽤Insert Cookie、Rewrite Cookie或Embed Cookie算法时cookie值中后台服务信息的格式配置”，但未要求输出加密元数据；然而，Bug修复明确引入 AES 加密密码（`[passwd] aes encry passwd`），且产品知识中多次强调“AES加密的密码”，因此预期结果必须可验证加密是否生效、使用何种算法、密钥是否被正确应用。当前用例无法区分“配置了密码但未启用加密”与“配置并生效”的真实状态。
**依据**: `<bug_context>` 中 Fixed Details 明确 `[passwd] aes encry passwd`；`<cli_reference>` 中 `show slb mode ircookie [group_name]` 的描述为“显⽰...格式配置”，但未定义输出字段细节；结合 `<product_knowledge>` 中“AES加密的密码”为关键交付项，其可观测性必须通过 show 命令输出验证。
**优先级**: High

### 发现 2: IPv6 CLI 端到端配置与验证缺失
**涉及范围**: 模块 1/28（CLI 配置）、模块 2/28（CLI show）、模块 9/28（CLI no）、模块 10/28（CLI clear）——全部无 IPv6 地址族相关用例
**问题或建议**: 尽管 `<test_cases.md>` 中模块 23/28（WebUI > IPv6）覆盖了 IPv6 功能，但 CLI 层面完全缺失 IPv6 场景：无 `slb mode ircookie ...` 在 IPv6 VS/RS 下的配置、无 `show slb mode ircookie` 在 IPv6 组下的输出验证、无 `no slb mode ircookie g1` 在 IPv6 组下的删除验证。而 `<check_spec_constraints>` 明确指出 SLB 对 IPv6 “提供了⼴泛的IPv6⽀持”，且“反向代理模式既能够⼯作在IPv4和IPv6混合的⽹络环境中”，故 CLI 必须支持 IPv6 上下文中的 ircookie 配置。
**依据**: `<check_spec_constraints>` 查询结果：“设备对SLB功能提供了⼴泛的IPv6⽀持”；`<bug_context>` 中 Fixed Details 未限定地址族，属全协议栈能力；`<test_cases.md>` 中 CLI 模块（#1–#64）全部未提及 IPv6。
**优先级**: High

### 发现 3: Negative 测试未覆盖“全局配置与 group 配置冲突”场景
**涉及范围**: 模块 1/28（CLI 配置）、模块 3/28（WebUI group 配置）、模块 16/28（WebUI global 配置）
**问题或建议**: Bug 根因是“只能全局配置，不能基于单独 group 来设定”，修复后需验证“全局 + group 同时存在时的行为一致性”。当前用例中，模块 5/28（全局与 group 优先级）仅验证了“删除 group 配置后回退到全局”，但未构造“全局配置为 enc_ip + group 配置为 plainname”等非法/冲突组合，并验证系统是否拒绝、告警或按优先级静默覆盖。Negative 类型用例（如 #12, #15, #17）仅覆盖单参数大小写/非法值，未覆盖跨配置层级的逻辑冲突。
**依据**: `<bug_context>` Root Cause：“只能全局配置，不能基于单独group来设定”；`<product_knowledge>` 中 `no slb mode ircookie <group_name>` 和 `clear slb mode ircookie` 均存在，说明多层级配置共存是设计前提，冲突处理必须验证。
**优先级**: High

### 发现 4: CLI 命令语法完整性缺陷：`no slb mode ircookie <group_name>` 不支持 `global`
**涉及范围**: 模块 9/28: CLI > no slb mode ircookie <group_name>（用例 #43）
**问题或建议**: 用例 #43 的 Expected Result 为“删除失败(仅支持删除组的配置)”，但 `<cli_reference>` 明确列出 `no slb mode ircookie <group_name>`，且 `<bug_context>` Fixed Details 中 `[group_name] option, default is global`，说明 `global` 是合法参数值。当前用例将 `global` 视为非法输入，与修复后语法矛盾，属于 CLI 语法理解错误，导致 Negative 测试用例误判。
**依据**: `<cli_reference>` 中 `no slb mode ircookie <group_name>`；`<bug_context>` Fixed Details：“[group_name] option, default is global”；用例 #43 描述“参数为global | 删除失败”，与规格冲突。
**优先级**: High

### 发现 5: WebUI 多语言/主题兼容性测试未覆盖 CLI 配置同步场景
**涉及范围**: 模块 19/28（WebUI > 语言）、模块 20/28（WebUI > 主题）、模块 11/28（CLI > Configure）、模块 12/28（CLI > Show）
**问题或建议**: 模块 19/28 和 20/28 验证了在不同语言/主题下 WebUI 配置 ircookie 的功能，但未验证“在英文界面配置后，切换至中文界面执行 `show run` 是否仍能正确解析 AES 密码”或“dark 主题下 CLI `configure all` 是否能正确加载 WebUI 配置”。CLI 与 WebUI 配置共享同一配置树，多语言/主题变更不应影响底层配置读写一致性。当前测试割裂了 UI 表现层与 CLI 控制层的集成验证。
**依据**: `<test_cases.md>` 中模块 19/28、20/28 仅做 UI 操作，未与 CLI 模块（#65–#70）交叉验证；`<product_knowledge>` 中 CLI 与 WebUI 均为 SLB 的“管理接口”，属同一功能模块。
**优先级**: Medium

### 发现 6: Stress Testing 缺乏对 AES 密钥轮换的独立压力验证
**涉及范围**: 模块 28/28: Stress Testing > HA（用例 #258–#265）
**问题或建议**: 当前所有 Stress 用例均将“修改 slb mode ircookie 的模式及 AES 的密码”耦合在 CPU 负载场景中，但未设计纯密钥轮换压力测试（如：不修改模式，仅高频更新 AES 密码 1000 次/秒，持续 1h）。AES 密钥管理涉及加解密上下文重建、内存分配/释放，是安全敏感路径，必须独立验证其稳定性。现有用例无法分离“模式切换”与“密钥更新”的故障归因。
**依据**: `<bug_context>` Fixed Details 明确 `[passwd] aes encry passwd` 为独立可配置项；`<test_cases.md>` 中 #264–#265 仅在“同时修改 group meth 与 AES 密码”场景下施压，未隔离密钥维度。
**优先级**: Medium

### 发现 7: Upgrade 兼容性测试未覆盖降级后 AES 密码残留风险
**涉及范围**: 模块 15/28: CLI > Upgrade（用例 #80）
**问题或建议**: 用例 #80 预期“升级到不支持为group配置插入cookie的版本 | 升级后该group的ircookie配置丢失”，但未验证“AES 密码是否以明文/弱加密形式残留在配置文件中”，构成严重安全风险。降级应确保敏感字段（如 AES 密码）被彻底擦除或不可逆销毁，而非简单丢弃配置项。
**依据**: `<bug_context>` 中 `passwd` 为 AES 加密密码，属高敏数据；`<test_cases.md>` #80 仅验证配置“丢失”，未验证存储介质安全性。
**优先级**: High

### 发现 8: log > log（用例 #233）未定义日志加密强度与算法
**涉及范围**: 模块 25/28: log > log（用例 #233）
**问题或建议**: Expected Result 仅写“日志记录会对AES密码加密”，但未指明加密算法（AES-128/AES-256？）、密钥来源（静态密钥 or KMS？）、是否启用 FIPS 模式。日志加密若使用弱算法或硬编码密钥，将使整个加密体系失效。该用例缺乏可验证的加密强度断言。
**依据**: `<bug_context>` Fixed Details 强调 `[passwd] aes encry passwd`，暗示 AES 是可信加密标准；`<test_cases.md>` #233 的 Expected Result 过于模糊，违反 （Expected Result 可量化/可验证）。
**优先级**: High

### 发现 9: CLI 自动补全用例（#6, #42, #52, #63）未验证补全内容准确性
**涉及范围**: 模块 1/28（#6）、模块 9/28（#42）、模块 2/28（#52）、模块 10/28（#63）
**问题或建议**: 所有自动补全用例的 Expected Result 均为“能够自动补全”，但未验证补全项是否完整、是否包含 `enc_name`/`enc_ip`/`[group_name]`/`[passwd]` 等新增参数。CLI 补全是用户操作效率与语法引导的关键，必须验证补全列表的完备性与正确性。
**依据**: `<cli_reference>` 中 `slb mode ircookie <ircookie_mode> [group_name] [passwd]` 为完整语法；`<test_cases.md>` 中补全用例（#6, #42, #52, #63）均未定义补全内容，违反 。
**优先级**: Medium

### 发现 10: WebUI segment 模块（模块 17/28, 24/28）与核心修复无技术关联，应移出回归范围
**涉及范围**: 模块 17/28（segment webui global）、模块 24/28（segment）
**问题或建议**: `<bug_context>` 与 `<product_knowledge>` 均未提及 segment 模块与 ircookie 加密的任何数据流或功能依赖；`<test_cases.md>` 中模块 17/28 明确“配置失败，不支持配置全局ircookie”，模块 24/28 仅为全局/组优先级的二次验证，属冗余回归。保留将稀释核心路径测试资源。
**依据**: `<data_source_boundary_rules>` 第 2 条：“<product_knowledge> 是参考背景，不是评审对象”；但 `<bug_context>` 与 `<product_knowledge>` 均无 segment 关联证据；`<test_cases.md>` 模块 17/28 自身声明“不支持”，与本次修复目标（支持 group 配置）正交。
**优先级**: Low

[溯源校验摘要] 
无意见被剔除。所有 10 条发现均具备 `<bug_context>`、`<cli_reference>`、`<product_knowledge>` 或 `<test_cases.md>` 中的字面证据支撑，未引用 `<similar_tests>` 中的 Case ID，未基于通用测试经验臆测（如超长输入、并发数），且所有引用的用例编号（#1–#265）均来自 `<target_test_cases>`。

