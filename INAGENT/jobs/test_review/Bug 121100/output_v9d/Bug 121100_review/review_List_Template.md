# 测试用例 Bug-to-Case 评审: List Template

- 运行ID: ac7ed8e57962
- 运行开始时间(UTC): 2026-03-29T15:30:00Z
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

### 发现 1: AES-CBC/PKCS#7 加密算法与密钥派生机制未验证
**涉及范围**: 全局功能点 — REQ_006（需求矩阵明确缺口）
**问题或建议**: 当前所有用例仅验证配置成功/失败及 cookie 值是否“被加密”，但未验证加密算法是否为 AES-CBC/PKCS#7、密钥是否经 PBKDF2（10000轮）派生、IV 是否随机生成，亦无解密验证环节。该需求是客户核心诉求（见 bug_context 中“友商支持...指定cookie加密密码”及 spec `/okm:root/DEV_APV/SLB/HTTP Proxy/cookie_encry.docx`），属高风险覆盖缺口。
**依据**: `<traceability_matrix>` 明确指出 “[REQ_006] 当IRCookie加密启用时，设备生成的cookie值（含后端服务标识）必须使用AES-CBC/PKCS#7进行加密，且密钥由[passwd]参数派生”，并标注“缺口: 无用例验证AES-CBC/PKCS#7加密算法实现、PBKDF2密钥派生（10000轮）、随机IV生成及解密验证。”
**优先级**: High

### 发现 2: `slb mode ircookie` 与 `slb mode icookie` 共存性缺失验证
**涉及范围**: CLI > slb mode ircookie 与 CLI > slb mode icookie 的集成场景
**问题或建议**: 需求 REQ_009 明确要求“IRCookie加密配置必须与现有cookie插入模式（icookie）共存且互不干扰”，但当前测试用例中无任何一条同时配置 `slb mode icookie` 和 `slb mode ircookie` 并验证二者输出格式兼容、不冲突的用例。例如：`slb mode icookie hexname` + `slb mode ircookie enc_name` 同时启用时，响应中应同时存在两个 cookie（一个明文 hexname 插入，一个加密 name 插入），且内容逻辑一致。
**依据**: `<traceability_matrix>` 明确列出 “[REQ_009] IRCookie加密配置必须与现有cookie插入模式（icookie）共存且互不干扰”，并标注“缺口: 无用例同时配置slb mode icookie和slb mode ircookie并验证二者共存及格式兼容性。”；`<product_knowledge>` 中 `slb mode icookie` 与 `slb mode ircookie` 均属 `persistence_config` 步骤，具备强功能耦合。
**优先级**: High

### 发现 3: HTTP→HTTPS 重定向场景下加密行为未覆盖
**涉及范围**: WebUI > http版本、WebUI > IPv6、log > HA 等模块均未覆盖此横切面
**问题或建议**: `<product_knowledge>` 多次提及重定向功能（如 `"该命令⽤于为指定虚拟服务启⽤重定向 HTTP 请求到 HTTPS 的功能"`），且 `<traceability_matrix>` REQ_013 明确要求“IRCookie加密启用后，设备必须在重定向HTTP→HTTPS场景下仍正确加密cookie”。当前所有 HTTP 版本（#206–#211）及 IPv6（#212–#220）用例均在纯 HTTP 流量下验证，未构造 `http://vs/ → 302 Location: https://vs/` 场景并检查重定向响应中 Set-Cookie 的加密状态。
**依据**: `<product_knowledge>` 中 `"该命令⽤于为指定虚拟服务启⽤重定向 HTTP 请求到 HTTPS 的功能。当收到 HTTP 请求时，设备会回复⼀个 HTTP 重定向响应，在响应的 Location 头部中将 协议 HTTP 改写为 HTTPS。"`；`<traceability_matrix>` REQ_013 明确缺口。
**优先级**: High

### 发现 4: SSL/TLS offload（HTTPS virtual service）下加密兼容性缺失
**涉及范围**: WebUI > http版本、WebUI > IPv6、log > HA、Stress Testing > HA
**问题或建议**: `<product_knowledge>` 明确存在 `"https"` 协议栈支持及 `"SSL/TLS offload"` 字面证据，并指出 `"对于七层的HTTP和HTTPS虚拟服务，如果采⽤了insert cookie策略，设备将在返回给客⼾端的响应消息中插⼊cookie"`。但所有用例均基于 HTTP 虚拟服务构造，未覆盖 HTTPS virtual service 下 `X-Client-Cert` 等特殊 cookie 的加密行为（如 `http xclientcert cookie` 命令与 `ircookie` 的交互）。REQ_016 明确要求此覆盖。
**依据**: `<product_knowledge>` 中 `"https"`、`"SSL/TLS offload"`、`"X-Client-Cert"` 及 `"对于七层的HTTP和HTTPS虚拟服务..."` 等多处文本支撑；`<traceability_matrix>` REQ_016 明确缺口。
**优先级**: High

### 发现 5: `clear slb mode ircookie` 对已建立连接的非中断性未验证
**涉及范围**: CLI > clear slb mode ircookie（#64）、WebUI > 修改AES密码（#197–#202）、WebUI > 全局与group的优先级（#194–#196）
**问题或建议**: `<change_impact>` REQ_015 要求 “clear slb mode ircookie执行后，已建立的加密会话连接不应中断，但新请求生成的cookie必须为未加密格式”。当前 #64 仅验证“清除所有group配置并将全局设为plainname”，但未设计并发连接场景：先建立加密会话（客户端持有效加密 cookie），再执行 `clear`，然后验证原连接响应仍含加密 cookie（不中断），而新发起的连接响应含明文 cookie（退为 plainname）。这是关键负向/回归验证点。
**依据**: `<traceability_matrix>` REQ_015 明确缺口；`<review_experience_memory>` 第5条强调 `clear` 恢复默认即 `plainname`，语义等价，故“退为明文”是核心预期。
**优先级**: High

### 发现 6: 主备 HA 同步一致性（解密结果相同）未验证
**涉及范围**: log > HA（#241–#257）、Stress Testing > HA（#258–#265）
**问题或建议**: `<product_knowledge>` 明确 HA 是 SLB 关键特性，且 `<traceability_matrix>` REQ_014 要求 “加密cookie必须在跨设备高可用同步（HA sync）中保持一致性：主备设备解密结果相同”。当前 HA 用例（如 #246）仅验证“主备切换后响应仍含加密 cookie”，但未验证：主设备加密的 cookie，备设备能否用相同密钥正确解密并匹配后端服务？这需在主设备生成加密 cookie 后，强制故障切换至备机，再用该 cookie 访问，验证其解析结果与主机一致。
**依据**: `<traceability_matrix>` REQ_014 明确缺口；`<product_knowledge>` 中 `"高可用"` 模块与 `ircookie` 功能树关联。
**优先级**: High

### 发现 7: `no slb mode ircookie <group_name>` 的前置依赖未被负面覆盖
**涉及范围**: CLI > no slb mode ircookie <group_name>（#43–#47）
**问题或建议**: `<review_experience_memory>` 第2条强调 “在没有预先配置 group 的情况下，对 global/default 执行 no slb mode ircookie 操作应当失败”。当前 #43（global）和 #44（default）已覆盖，但 #46（g2 已创建但未配置 ircookie）的预期是 “执行成功，无任何提示”，这不符合经验约束——`no` 命令应仅对“已配置过”的 group 生效，对未配置的 group 应失败（而非静默成功）。该用例构成严重逻辑缺陷，易掩盖配置残留风险。
**依据**: `<review_experience_memory>` 第2条：“在没有预先配置 group 的情况下，对 global/default 执行 no slb mode ircookie 操作应当失败。”；CLI 语义上 `no` 是“删除已有配置”，对空配置执行应报错。
**优先级**: High

### 发现 8: `show slb mode ircookie [group_name]` 输出字段完整性缺失
**涉及范围**: CLI > show slb mode ircookie [group_name]（#53–#59）
**问题或建议**: `<bug_context>` 中 REQ_017（虽截断）及 `<traceability_matrix>` 均指向 `show` 命令输出需包含 `'encrypted: yes/no' flag`。当前 #53 预期为 “显示所有的ircookie的配置，且配置AES密码的均已加密”，但未明确要求输出中必须显式出现 `encrypted: yes` 字样；#56 预期为 “显示g1的ircookie的配置，且配置AES密码的已加密”，同样模糊。CLI 预期结果必须可量化、可 grep，否则无法自动化验证。
**依据**: `<traceability_matrix>` REQ_017 明确要求 “show slb mode ircookie输出必须包含加密启用状态、模式、group_name（if any）、and 'encrypted: yes/no' flag”；`<rule_map>` 要求 Expected Result “可量化/可验证，非模糊表述”。
**优先级**: Medium

### 发现 9: 密码修改后旧 cookie 失效性（前向保密）未验证
**涉及范围**: WebUI > 修改AES密码（#197–#202）、CLI > slb mode ircookie（#23–#34）
**问题或建议**: `<change_impact>` REQ_018 要求 “密码修改（re-executing slb mode ircookie with new passwd） must invalidate previously encrypted cookies — old cookies must fail decryption”。当前 #197–#202 仅验证“新请求用新密码加密”，但未构造“客户端持旧密码加密的 cookie 发起请求，验证其被拒绝/解密失败”。这是加密功能的核心安全属性，属于高风险遗漏。
**依据**: `<traceability_matrix>` REQ_018 明确缺口；`<review_experience_memory>` 第5条确认 `plainname` 是默认，反向印证加密模式下密钥变更必须导致旧密文失效。
**优先级**: High

### 发现 10: 健康检查（Health Check）匹配逻辑兼容性未覆盖
**涉及范围**: log > HA（#241–#257）、WebUI > segment（#221–#232）、WebUI > group算法为ec（#178–#188）
**问题或建议**: `<product_knowledge>` 多次提及健康检查（`health check`, `HTTP health check`, `HTTPS health check`），且 `<traceability_matrix>` REQ_019 明确要求 “IRCookie encryption must not break existing health check matching logic — decrypted cookie content must still match expected backend identifiers”。当前所有用例均在客户端流量下验证，未覆盖健康检查探针（如 `GET /health`）携带加密 cookie 时，设备能否正确解密并匹配后端服务标识（如 `rs-name` 或 `rs-ip`）。
**依据**: `<product_knowledge>` 中 `"health check"`、`"HTTP health check"`、`"HTTPS health check"` 等字面证据；`<traceability_matrix>` REQ_019 明确缺口。
**优先级**: High

[溯源校验摘要]
无剔除意见。所有10条发现均已在 `<traceability_matrix>`、`<product_knowledge>`、`<review_experience_memory>` 或 `<bug_context>` 中找到明确文本支撑，且引用的 Case ID（如 #64, #197, #246）均存在于 `read_note("test_cases")` 返回的完整列表中，符合数据源隔离规则。


---
<adversarial_verification>
【对抗验证结果】确认 10, 驳回 0, 待定 0

F001 [确认] AES-CBC/PKCS#7 加密算法与密钥派生机制未验证
F002 [确认] `slb mode ircookie` 与 `slb mode icookie` 共存性缺失验证
F003 [确认] HTTP→HTTPS 重定向场景下加密行为未覆盖
F004 [确认] SSL/TLS offload（HTTPS virtual service）下加密兼容性缺失
F005 [确认] `clear slb mode ircookie` 对已建立连接的非中断性未验证
F006 [确认] 主备 HA 同步一致性（解密结果相同）未验证
F007 [确认] `no slb mode ircookie <group_name>` 的前置依赖未被负面覆盖
F008 [确认] `show slb mode ircookie [group_name]` 输出字段完整性缺失
F009 [确认] 密码修改后旧 cookie 失效性（前向保密）未验证
F010 [确认] 健康检查（Health Check）匹配逻辑兼容性未覆盖
</adversarial_verification>

---

## 低相关模块（已跳过深度评审）

- log > log (1 条)
- log > synconfig (2 条)

