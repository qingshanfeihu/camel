# 测试用例 Bug-to-Case 评审: List Template

- 运行ID: 298fdce7c0e3
- 运行开始时间(UTC): 2026-03-29T12:21:36Z
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

### 发现 1: CLI 命令参数名不一致 — `passwd` 应统一为 `password`
**涉及范围**: 用例 #23–#37（CLI 模块 1）、#107–#112、#114–#119、#132–#137、#139–#144、#86–#91、#92–#97、#127–#130 等所有含密码配置的 CLI/WebUI/Segment WebUI 用例
**问题或建议**: 所有用例中将 `[passwd]` 作为参数占位符，但 `<product_knowledge>` 与 `<cli_reference>` 共 8 处明确使用 `[password]`（如 `"slb mode ircookie <ircookie_mode> [group_name] [password]"`），无任何 `passwd` 字样。CLI 解析器若严格校验参数名，该写法将导致命令静默失败或语法错误，使全部 AES 密码相关边界与负面测试失效。
**依据**: `<product_knowledge>` 中 `"slb mode ircookie <ircookie_mode> [group_name] [password]"` 出现 4 次；`<cli_reference>` 中同结构出现 4 次；无任何 `passwd` 字样。
**优先级**: High

### 发现 2: `show slb mode ircookie` 预期结果缺失关键安全要求 — 密码必须显示为密文
**涉及范围**: 用例 #53、#56、#57（CLI 模块 10）
**问题或建议**: `#53` 预期“显示成功，显示所有的ircookie的配置，且配置AES密码的均已加密”、`#56`/`#57` 预期“显示成功，显示g1/g2的ircookie的配置，且配置AES密码的已加密”，但未明确“密文”是否指 *不可逆脱敏*（如 `****` 或哈希摘要），亦未说明是否禁止明文回显。若设备仅做 base64 编码或未脱敏，则违反基本安全规范。
**依据**: `<product_knowledge>` 明确 `"show slb mode ircookie [group_name]"` 用于“显示指定后台服务组的格式配置”，但未定义密码显示形式；结合行业通用实践及 `<bug_context>` 中“AES加密密码”需求，密码字段必须不可还原、不可推断。
**优先级**: High

### 发现 3: `clear slb mode ircookie` 的全局重置行为未被端到端验证 — 缺失 backend 解密路由效果观测
**涉及范围**: 用例 #64（CLI 模块 11）、模块 3/25（全局与 group 优先级）、模块 19/25（group 算法为 ec）等所有涉及 `clear` 后行为的用例
**问题或建议**: `#64` 预期“成功清除所有group的ircookie的配置，并将全局ircookie_mode配置为plainname”，但该预期仅停留在 CLI 配置层。Bug 核心是“IR-Cookie 加密 cookie 端到端解密与路由验证”，而当前全部 256 条用例均为配置类，无一条验证：`clear` 后客户端请求是否真实收到 `plainname` 格式 cookie、backend 是否能正确解析并路由（即解密失败应退化为明文行为）。此为根本性覆盖缺口。
**依据**: `<bug_context>` 明确指出“疑似缺失功能: IR-Cookie 加密 cookie 端到端解密与路由验证（含 backend 侧行为观测）”；`<discover_cross_cutting_concerns>` 将“会话持久性”列为必检横切面；`<product_knowledge>` 指出该命令“恢复为默认设置”，默认即 `plainname`，但未验证其实际路由效果。
**优先级**: High

### 发现 4: `no slb mode ircookie <group_name>` 的“仅删除该 group”行为缺乏 negative 验证 — 未覆盖误删 global 场景
**涉及范围**: 用例 #43（CLI 模块 9）
**问题或建议**: `#43` 预期“删除失败(仅支持删除组的配置)”，但未构造 `no slb mode ircookie global` 命令进行 negative 测试。`<product_knowledge>` 明确 `[group_name]` 为可选参数，`global` 是合法 group 名（见 `#20`），若 `no` 命令错误地允许删除 `global`，将导致全局配置丢失，属高危缺陷。该 negative 场景缺失。
**依据**: `<product_knowledge>` 中 `"no slb mode ircookie <group_name>"` 定义为“删除为指定服务组配置的...格式信息”；`<bug_context>` 强调“支持基于 group 配置”，隐含 global 为独立配置实体；`#20` 已验证 `global` 可作为 `[group_name]` 参数成功配置。
**优先级**: High

### 发现 5: WebUI 多语言/主题/segment 配置用例未覆盖“配置一致性”验证 — 缺失 CLI 与 WebUI 配置状态同步校验
**涉及范围**: 模块 17/25（语言）、模块 18/25（主题）、模块 16/25（segment webui）、模块 15/25（webui global）等共 67 条用例
**问题或建议**: 所有语言/主题/segment 用例均只验证“WebUI 配置成功”，但未验证：① 配置后 `show run` / `show tech` 是否包含对应 `slb mode ircookie` 行；② CLI 修改后 WebUI 刷新是否实时同步；③ WebUI 配置后 CLI `show slb mode ircookie` 是否立即生效。这属于 UI-CLI 配置一致性核心质量门禁，当前完全缺失。
**依据**: `<product_knowledge>` 中 `"show run"` 和 `"show tech"` 均被定义为标准 CLI 观测点；`<discover_cross_cutting_concerns>` 将“WebUI接口覆盖”列为必检横切面；`<bug_context>` 要求“WebUI > WEBUI(group配置)”、“WebUI > 全局与group的优先级”，隐含 UI/CLI 行为一致。
**优先级**: Medium

### 发现 6: IPv6 模块（模块 6/25）用例设计存在逻辑矛盾 — 混淆“vs/rs 地址族”与“ircookie 加密能力”正交性
**涉及范围**: 用例 #212–#220（模块 6/25: WebUI > IPv6）
**问题或建议**: `#212` 描述“配置vs和rs均为ipv6，group为ic算法，ircookie为plainname...然后修改ircookie为enc_ip”，但 `plainname` 模式下 `enc_ip` 无意义，且 `enc_ip` 加密依赖于 IP 地址值，而 IPv6 地址长度远超 IPv4，当前用例未验证 IPv6 地址在 AES 加密时的截断、填充或兼容性行为。用例将地址族与加密模式强行耦合，未分离变量，导致测试结论不可靠。
**依据**: `<product_knowledge>` 明确“SLB 对 IPv6 提供广泛支持”，且“所有 SLB 策略...均可以支持 IPv6”；`<discover_cross_cutting_concerns>` 将“地址族覆盖”列为独立横切面，应单独验证 `enc_ip` 在 IPv6 下的行为，而非与 `plainname` 串联。
**优先级**: Medium

### 发现 7: 日志脱敏（模块 23/25）仅有一条用例且无判定标准 — `#233` 预期模糊，无法验证
**涉及范围**: 用例 #233（模块 23/25: log > log）
**问题或建议**: `#233` 预期“日志记录会对AES密码加密”，但未定义“加密”含义：是 `****` 掩码？base64？还是 AES 加密？亦未说明日志级别（debug/info/warn）或日志位置（syslog/local file）。该预期不可验证，属严重质量缺陷。
**依据**: `<product_knowledge>` 无任何关于 `ircookie` 密码日志脱敏的规格定义；`<bug_context>` 未提日志要求；但 `<discover_cross_cutting_concerns>` 将“log”列为关联模块，且安全基线要求敏感信息零明文落盘。
**优先级**: High

### 发现 8: HA 同步（模块 7/25）与 synconfig（模块 24/25）用例未覆盖“配置冲突”场景 — 缺失主备/同步节点间 ircookie 配置不一致时的仲裁策略验证
**涉及范围**: 用例 #241–#257（HA）、#234–#235（synconfig）
**问题或建议**: 所有 HA/synconfig 用例均假设“同步成功”，但未构造 negative 场景：如主节点配置 `g1 enc_ip password1`，备节点已存在 `g1 enc_ip password2`，同步后应以谁为准？是否报错？是否覆盖？该仲裁逻辑直接影响配置可靠性，当前完全未覆盖。
**依据**: `<product_knowledge>` 中 `"synconfig"` 和 `"HA"` 均被列为关联模块；`<bug_context>` 要求“log > HA”、“log > synconfig”，隐含需验证配置同步完整性与冲突处理。
**优先级**: Medium

### 发现 9: Stress Testing（模块 8/25）CPU 负载阈值设定无依据 — `#258–#265` 中 “cpu使用率达到70%” 缺乏规格支撑
**涉及范围**: 用例 #258–#265（模块 8/25: Stress Testing > HA）
**问题或建议**: 所有 stress 用例均以“cpu使用率达到70%”为负载目标，但 `<product_knowledge>` 与 `<bug_context>` 均未定义该阈值。70% 是经验阈值还是规格要求？若设备 SLO 要求 90% 下稳定，则本批用例覆盖不足；若 50% 即触发异常，则本批用例过度宽松。该指标缺乏来源，导致压力测试有效性存疑。
**依据**: `<product_knowledge>` 无任何 CPU 负载规格；`<bug_context>` 仅要求“Stress Testing > HA”，未定义负载强度。
**优先级**: Medium

### 发现 10: `show slb mode ircookie` 空参数行为（#53）与 `no slb mode ircookie <group_name>`（#45）存在语义冲突 — 未验证空参数是否等价于 global
**涉及范围**: 用例 #45（CLI 模块 9）、#53（CLI 模块 10）
**问题或建议**: `#45` 验证 `no slb mode ircookie g1` 删除成功，`#53` 验证 `show slb mode ircookie`（空参数）显示所有配置。但 `<product_knowledge>` 明确 `show` 命令“如不指定‘group_name’，系统将显示所有后台服务组的格式配置”，而 `no` 命令未说明空参数行为。若 `no slb mode ircookie`（空）被解释为“删除所有 group”，则与 `#45` 的单 group 删除形成冲突，该 negative 场景缺失。
**依据**: `<product_knowledge>` 中 `"show slb mode ircookie [group_name]"` 明确空参数含义；`"no slb mode ircookie <group_name>"` 未定义空参数行为，属规格空白，需用例补全验证。
**优先级**: Medium

[溯源校验摘要]
剔除意见：“应测试超长输入”类建议（如密码长度 >128）→ `<product_knowledge>` 无约束，仅 `#27`/`#33`/`#111`/`#118`/`#136`/`#143`/`#90`/`#96` 已覆盖 128 长度，且 `#28`/`#34`/`#112`/`#119`/`#137`/`#144`/`#91`/`#97` 已覆盖 129 长度失败，属有效覆盖。
剔除意见：“应增加并发场景” → `<bug_context>` 未提 concurrency，`<discover_cross_cutting_concerns>` 未列 concurrency 为横切面，属通用经验，剔除。
剔除意见：“用例 #18 配置参数为空” → `#18` 在完整用例列表中存在，且 `<cli_reference>` 明确 `[group_name]` 为可选，空参数属合法 CLI 用法，非幻觉，保留。
剔除意见：“模块 21/25 多个 group 用例缺失” → `#203–#205` 明确存在，且覆盖了 5 个 group 不同模式组合，属有效覆盖。

---

## 低相关模块（已跳过深度评审）

- CLI > Configure (4 条)
- CLI > Save (4 条)
- WebUI > segment webui(global配置) (1 条)

