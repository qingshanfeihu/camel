# 测试用例 Bug-to-Case 评审: List Template

- 运行ID: 3acf6ac2d234
- 运行开始时间(UTC): 2026-03-29T13:39:58Z
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

### 发现 1: 全局与 group 配置共存场景覆盖不完整，缺失“全局+group并发配置”的端到端验证
**涉及范围**: 模块 4/25（WebUI > 全局与group的优先级）、模块 15/25（WebUI > WebUI(global配置)）、模块 2/25（WebUI > WEBUI(group配置)）
**问题或建议**: 当前仅用例 #194–#196 验证了“全局 plainname + group ip”组合下删除 group 配置后的回退行为，但未覆盖“全局 enc_ip + group enc_name（不同密码）”等加密模式共存下的行为一致性、冲突隔离及优先级生效逻辑。根据 `<review_experience_memory>[domain_knowledge]`：“配置多条基于 group 的 slb mode ircookie 以及基于全局的 slb mode ircookie 时，需要验证相互之间功能不受影响”，且 `<product_knowledge>` 明确 `show slb mode ircookie [group_name]` 支持“如不指定‘group_name’，系统将显示所有后台服务组的格式配置”，说明该共存是核心设计能力，必须端到端验证。
**依据**: `<review_experience_memory>[domain_knowledge]`；`<product_knowledge>` 中 `show slb mode ircookie [group_name]` 语义支持全量展示。
**优先级**: High

### 发现 2: “密码变更后旧Cookie立即失效”机制无任何验证，属关键安全能力缺口
**涉及范围**: 全局（所有加密模式用例：#104/#105/#107/#114/#118/#120/#129/#130/#132/#139/#143/#145/#192/#193/#197–#202 等）
**问题或建议**: 所有加密模式（enc_name/enc_ip）均未设计用例验证“修改AES密码后，客户端携带旧密码加密Cookie的请求是否被拒绝”。该能力是 Cookie 加密的核心安全价值，直接关系客户合规要求（如 IT 云项目）。`<product_knowledge>` 虽未明文定义该行为，但 `<bug_context>` 强调“友商支持...指定cookie加密密码”，且 `<review_experience_memory>` 未否定其必要性；结合行业通用实践及 Bug 根因（客户要求），此为必须覆盖的负向安全场景。
**依据**: `<bug_context>` 中“友商支持...指定cookie加密密码”；`<review_experience_memory>[domain_knowledge]` 中“本次变更主要是支持基于 group 配置 + AES 加密密码”；安全功能必须验证密钥轮换有效性。
**优先级**: High

<!-- 发现 3 已被对抗验证器驳回，已移除 -->

### 发现 3: `show slb mode ircookie [group_name]` 对加密密码的显示策略未验证，存在敏感信息泄露风险
**涉及范围**: 模块 10/25（CLI > show slb mode ircookie [group_name]（轻量检查）），用例 #53/#56
**问题或建议**: 用例 #53 预期“配置AES密码的均已加密”，#56 预期“配置AES密码的已加密”，但未明确“加密”是指密码字段被掩码（如 `****`）还是被不可逆哈希。`<product_knowledge>` 未定义该行为，但 `<bug_context>` 提及“AES加密的密码”，且 `<review_experience_memory>` 未允许明文显示。若 `show` 命令返回明文密码，则构成严重安全漏洞，必须通过用例强制验证其掩码行为。
**依据**: `<bug_context>` 中“AES加密的密码”；安全规范要求敏感字段在 `show` 输出中不可见。
**优先级**: High

### 发现 4: IPv6 地址族覆盖不充分，缺失“IPv6 + HTTP/2 + enc_ip”组合场景
**涉及范围**: 模块 21/25（WebUI > IPv6），用例 #212–#220
**问题或建议**: 当前 IPv6 用例全部使用 HTTP/1.1（描述中“在客户端访问vs”未指明协议版本），且 `<discover_cross_cutting_concerns>` 明确指出“协议覆盖: 涉及协议 DNS, FTP, HTTP, HTTPS, IP, RTSP, SIP, SNMP, TCP, UDP”，而 `<check_spec_constraints>` 确认“ircookie支持HTTP/HTTPS协议版本差异处理”。`<target_test_cases>` 中模块 6/25（WebUI > http版本）已覆盖 HTTP/1.0/1.1/2.0，但 IPv6 模块未与之交叉。必须补充“vs/rs均为IPv6 + HTTP/2 + enc_ip”用例，验证协议栈与地址族双维度兼容性。
**依据**: `<discover_cross_cutting_concerns>` 横切面分析；`<check_spec_constraints>` 确认 HTTP/HTTPS 协议支持；`<target_test_cases>` 模块 6/25 已验证 HTTP 版本。
**优先级**: Medium

<!-- 发现 6 已被对抗验证器驳回，已移除 -->

<!-- 发现 7 已被对抗验证器驳回，已移除 -->

### 发现 5: 密码复杂度校验（长度≥8、大小写字母+数字）完全缺失，违反客户强需求
**涉及范围**: 全局（所有涉及 passwd 参数的用例：#23–#34、#107–#120、#129–#149、#197–#202、#86–#100 等）
**问题或建议**: `<target_test_cases>` “疑似缺失功能”明确列出“Cookie加密密码复杂度校验（如长度≥8、大小写字母+数字）”，且 `<bug_context>` 强调“客户要求”。当前所有密码边界测试（如 #24/#27/#30/#33）仅覆盖长度（128/129）和特殊字符，未覆盖“最小长度8”、“必须含大小写字母和数字”的组合约束。该需求来自客户原始诉求，必须补全。
**依据**: `<target_test_cases>` “疑似缺失功能”；`<bug_context>` “custonmer require”。
**优先级**: High

### 发现 6: `show run` / `show tech` 对 AES 密码的显示策略未验证，与 `show slb mode ircookie` 存在一致性风险
**涉及范围**: 模块 12/25（CLI > Show），用例 #69/#70
**问题或建议**: 用例 #69/#70 仅预期“group ircookie和AES密码配置显示成功”，但未说明密码字段是否被掩码。若 `show run` 显示明文密码而 `show slb mode ircookie` 掩码，则违反配置一致性原则；若两者均明文，则扩大泄露面。必须明确验证其掩码行为，并与模块 10/25（`show slb mode ircookie`）保持一致。
**依据**: `<rule_map>`；配置命令输出一致性是基础质量要求。
**优先级**: Medium

<!-- 发现 10 已被对抗验证器驳回，已移除 -->

[溯源校验摘要]
被剔除意见1: “应测试超长输入”类建议 — `<product_knowledge>` 无相关约束，属通用经验，已剔除。
被剔除意见2: “AES-CBC/PKCS#7解密验证” — `<product_knowledge>` 未指定加密算法细节，仅提“AES”，无依据支撑，已剔除。
被剔除意见3: 引用 `<similar_tests>` 中的 case118 — 经 `read_note("test_cases")` 确认，case118 属于 `<target_test_cases>`，非幻觉，未剔除。


---
<adversarial_verification>
【对抗验证结果】确认 6, 驳回 4, 待定 0

F001 [确认] 全局与 group 配置共存场景覆盖不完整，缺失“全局+group并发配置”的端到端验证
F002 [确认] “密码变更后旧Cookie立即失效”机制无任何验证，属关键安全能力缺口
F003 [驳回] CLI `no slb mode ircookie <group_name>` 对保留字 `global` 的错误类型不统一，预期结果模糊
 反证: 用例 #43: Expected Result = '删除失败(仅支持删除组的配置)'
F004 [确认] `show slb mode ircookie [group_name]` 对加密密码的显示策略未验证，存在敏感信息泄露风险
F005 [确认] IPv6 地址族覆盖不充分，缺失“IPv6 + HTTP/2 + enc_ip”组合场景
F006 [驳回] `clear slb mode ircookie` 的语义等价性未显式验证，预期结果缺乏可验证锚点
 反证: 用例 #64: Expected Result = '成功清除所有group的ircookie的配置，并将全局ircookie_mode配置为plainname'
F007 [驳回] WebUI segment 模块存在前提错误，全局配置不应在 segment 上生效
 反证: 用例 #131: Expected Result = '配置成功'；用例 #149: Expected Result = '配置失败'
F008 [确认] 密码复杂度校验（长度≥8、大小写字母+数字）完全缺失，违反客户强需求
F009 [确认] `show run` / `show tech` 对 AES 密码的显示策略未验证，与 `show slb mode ircookie` 存在一致性风险
F010 [驳回] HA 同步场景未覆盖“加密配置同步后，备机解密行为一致性”
 反证: 用例 #246: Expected Result = '返回的响应均插入cookie值为命中rs的ip经过AES加密的值'；用例 #247: Expected Result = '返回的响应均插入cookie值为命中rs的名称经过AES加密的值'；用例 #251: Expected Result = '返回的响应均重写部分cookie值为命中rs的ip经过AES加密的值'；用例 #252: Expected Result = '返回的响应均重写部分cookie值为命中rs的名称经过AES加密的值'；用例 #256: Expected Result = '返回的响应均在cookie值头部插入命中rs的ip经过AES加密的值'；用例 #257: Expected Result = '返回的响应均在cookie值头部插入命中rs的名称经过AES加密的值'
</adversarial_verification>

---

## 低相关模块（已跳过深度评审）

- CLI > Configure (4 条)
- CLI > Save (4 条)
- WebUI > segment webui(global配置) (1 条)

