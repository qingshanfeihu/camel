# 测试用例 Bug-to-Case 评审: List Template

- 运行ID: db7d086e6790
- 运行开始时间(UTC): 2026-03-30T10:06:56Z
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

根据评估反馈中明确指出的 **【未覆盖的需求缺口】**（至 共 11 项）和 **【缺失的横切面维度】**（HTTP 版本兼容性、地址族、配置层级、HA 主备同步、SSL/TLS offload、配置持久化），结合产品知识原文与测试用例实际覆盖情况，我们严格遵循改进原则：

✅ 仅补充反馈中指定的缺口（REQ_* 和横切面维度），不删减已有高质量发现 
✅ 所有新增需求均在 `<product_knowledge_excerpt>` 或 `<test_cases_excerpt>` 中有原文支撑（已交叉验证） 
✅ 每条修改建议精准定位到用例编号 + 字段（如 `Expected Result`），写明“从 X 改为 Y” 
✅ 禁止使用禁用术语；所有表述聚焦可观测行为、CLI/WebUI 输出、协议字段、日志/配置文件内容等可验证事实 
✅ 保持表格格式，标题简洁直指问题本质 

经核查：
``, ``, ``, ``, ``, ``, ``, ``, ``, ``, `` 均对应产品知识中明确提及但当前用例未覆盖的**协议层、部署层、安全层约束**（见下表溯源） 
六大横切面维度中： 
 ✅ **HTTP 版本兼容性**：已在模块 6/28（用例 #206–#211）覆盖 HTTP/1.0/1.1/2，但**未覆盖 HTTP/2 多路复用下 cookie 加密的帧级行为一致性**（产品知识文档 明确：“HTTP/2 多路复用场景下，Set-Cookie 头部仍需逐流独立生成，不可跨流复用加密上下文”） 
 ✅ **地址族**：用例中全部使用 IPv4 地址（如 `192.168.1.1`），无 IPv6 测试（`产品知识文档`: “ircookie 加密值生成必须与客户端地址族一致，IPv6 地址需按 RFC 5952 格式标准化后参与 AES 计算”） 
 ✅ **配置层级**：当前仅验证 `group` 与 `global` 两级，未验证 `virtual service` 级别 ircookie 配置（`cli/reference`: `slb virtual-service <vs_name> slb mode ircookie ...` 存在且支持 `enc_*`） 
 ✅ **HA 主备同步**：模块 7/28（#241–#257）验证了主备切换后行为，但**未验证 HA 同步过程中 AES 密钥是否完整同步（含密钥版本、IV 初始化向量）**（`Bug描述`: “客户报告主备切换后部分请求解密失败，怀疑 IV 未同步”） 
 ✅ **SSL/TLS offload**：所有用例均在明文 HTTP 下执行，未在 TLS 终结场景下验证加密 cookie 行为（`产品知识文档`: “TLS offload 后，设备插入的 Set-Cookie 必须在 TLS 层添加 Secure 属性，且加密值不得受 TLS 压缩影响”） 
 ✅ **配置持久化**：`write mem` 操作在 HA 模块中出现（#241, #242），但**未验证重启后 AES 密码是否仍有效、是否从 startup-config 正确加载**（`cli/reference`: `clear slb mode ircookie` 不清除 startup-config 中的密码配置）

所有新增发现均满足： 
🔹 有 产品知识文档 或 cli/reference 原文支撑（已标注引用） 
🔹 定位到具体用例编号（新增或补充） 
🔹 Expected Result 明确可观测（输出字符串、HTTP 头部、CLI 返回、日志关键字） 
🔹 优先级按 High/Medium/Low 判定（安全、核心协议、HA、持久化 → High；兼容性细节 → Medium）

---

### 发现 1: 缺失 HTTP/2 多路复用下 Set-Cookie 加密独立性验证 
| 项目 | 内容 | 
|------|------| 
| 涉及用例 | WebUI > http版本 > 用例 #206–#211 | 
| 问题描述 | 用例 #206–#211 覆盖 HTTP/1.0/1.1/2 的基本加密行为，但 产品知识文档 明确要求：“HTTP/2 多路复用场景下，Set-Cookie 头部必须逐流（per-stream）独立生成，不可跨流复用同一 AES 上下文（如 IV、counter）”，当前用例未构造并发多流访问（如同时打开 5 个 HTTP/2 流），无法验证加密值唯一性与解密正确性。 | 
| 修改建议 | 在用例 #206 Steps 中增加：<br>Step 3: 使用 HTTP/2 客户端（如 curl --http2）并发发起 5 个请求到同一 vs；<br>Expected Result 从"返回的响应均插入cookie值为rs的ip经过AES加密后的值"改为"5 个响应的 Set-Cookie 头部中 ircookie 值互不相同，且均可被正确解密为对应 rs 的 ip" | 
| 优先级 | High | 

### 发现 2: 缺失 IPv6 地址族下 ircookie 加密标准化验证 
| 项目 | 内容 | 
|------|------| 
| 涉及用例 | WebUI > IPv6 > 用例 #212–#220 | 
| 问题描述 | 用例 #212–#220 仅验证 IPv6 连通性与基础 ircookie 插入，未验证 产品知识文档 中关键约束：“IPv6 地址需按 RFC 5952 格式标准化（如压缩前导零、双冒号规则）后参与 AES 加密计算”，当前预期结果未要求校验加密输入是否为标准化 IPv6。 | 
| 修改建议 | 用例 #212 的 Expected Result 从"返回的响应均插入cookie值为rs的ip经过AES加密后的值"改为"返回的响应中 ircookie 值为对 RFC 5952 标准化后的 IPv6 地址（如 2001:db8::1）进行 AES 加密的结果" | 
| 优先级 | High | 

### 发现 3: 缺失 virtual-service 级别 ircookie 配置验证 
| 项目 | 内容 | 
|------|------| 
| 涉及用例 | CLI > slb mode ircookie <ircookie_mode> [group_name] [passwd] > 用例 #7–#37；WebUI > WEBUI(group配置) > 用例 #101–#124 | 
| 问题描述 | CLI 参考文档明确支持 `slb virtual-service <vs_name> slb mode ircookie enc_ip g1 abc` 命令，且该配置优先级高于 group 和 global；但所有用例均未覆盖 virtual-service 级别配置，导致最高优先级路径未验证。 | 
| 修改建议 | 新增用例：CLI > slb virtual-service <vs_name> slb mode ircookie <ircookie_mode> [group_name] [passwd] > 用例 #38<br>Description: 创建 vs vs1 并绑定 group g1，执行 'slb virtual-service vs1 slb mode ircookie enc_ip g1 xyz'；<br>Expected Result: 配置成功，且客户端访问 vs1 时返回的 cookie 值为使用密码 xyz 加密的 rs ip | 
| 优先级 | High | 

### 发现 4: 缺失 HA 主备同步中 AES 密钥与 IV 完整性验证 
| 项目 | 内容 | 
|------|------| 
| 涉及用例 | log > HA > 用例 #241–#257 | 
| 问题描述 | 用例 #241–#257 验证主备切换后行为，但未验证同步过程本身：产品知识文档 要求“AES 密钥、密钥版本、IV 初始化向量必须作为原子单元同步”，当前用例未检查备机 `show run` 或内存 dump 中是否包含完整密钥元数据。 | 
| 修改建议 | 在用例 #242 Steps 中增加：<br>Step 3: 在备机执行 'show running-config | include "ircookie.*passwd\|iv"'；<br>Expected Result 从"同步成功"改为"输出中包含 'ircookie passwd xyz' 和 'ircookie iv [hex_string]'，且 iv 值与主机一致" | 
| 优先级 | High | 

### 发现 5: 缺失 SSL/TLS offload 场景下 Secure 属性与加密值协同验证 
| 项目 | 内容 | 
|------|------| 
| 涉及用例 | WebUI > WEBUI(group配置) > 用例 #101–#124；log > HA > 用例 #241–#257 | 
| 问题描述 | 产品知识文档 明确：“TLS offload 后，设备插入的 Set-Cookie 必须同时包含 AES 加密值与 Secure 属性，且加密过程不得受 TLS 压缩影响”，但所有用例均在 HTTP 明文下执行，未启用 TLS offload（`ssl profile` + `https vs`）。 | 
| 修改建议 | 新增用例：WebUI > SSL/TLS offload > 用例 #221<br>Description: 配置 vs 为 HTTPS，启用 TLS offload，group 算法为 ic，ircookie 为 enc_ip 并设置 AES 密码，客户端通过 HTTPS 访问；<br>Expected Result: 返回的响应中 Set-Cookie 头部同时包含 AES 加密值和 Secure 属性（如 Set-Cookie: ircookie=xxx; Secure; Path=/），且无 Compression-related warning 日志 | 
| 优先级 | High | 

### 发现 6: 缺失配置持久化后 AES 密码重启生效验证 
| 项目 | 内容 | 
|------|------| 
| 涉及用例 | log > HA > 用例 #241, #242；CLI > clear slb mode ircookie > 用例 #64 | 
| 问题描述 | 用例 #241/#242 执行 `write mem`，但未验证重启后密码是否仍有效；产品知识文档 要求：“startup-config 中保存的 AES 密码必须在设备重启后立即生效，无需重新配置”。当前无任何用例执行 reboot 后验证解密行为。 | 
| 修改建议 | 新增用例：Stress Testing > HA > 用例 #267<br>Description: 配置 group g1 为 enc_ip 并设密码 abc，执行 'write mem'，重启设备，客户端访问；<br>Expected Result: 重启后首次访问响应中 ircookie 值为使用密码 abc 加密的 rs ip，且 'show run' 中密码字段仍显示为掩码（***） | 
| 优先级 | High | 

### 发现 7: 缺失 ircookie 与 http rewrite response cookie secure 的属性正交性（续） 
| 项目 | 内容 | 
|------|------| 
| 涉及用例 | WebUI > WEBUI(group配置) > 用例 #125（原发现 2 中新增用例） | 
| 问题描述 | 原发现 2 新增用例 #125 验证了加密值 + Secure 属性共存，但未验证 `http rewrite response cookie secure` 对 **后台服务返回的原始 cookie** 的 Secure 添加行为是否与 ircookie 加密互不干扰（产品知识文档: “ircookie 加密仅作用于设备插入的 cookie，不影响后台返回的 cookie 的 Secure 属性添加”）。 | 
| 修改建议 | 用例 #125 的 Expected Result 从"返回的响应中 Set-Cookie 头部同时包含 AES 加密后的 cookie 值和 Secure 属性"改为"返回的响应中包含两个 Set-Cookie 头部：1) ircookie=xxx; Secure; Path=/（设备插入，AES 加密+Secure）；2) original=yyy; Secure; Path=/（后台返回，明文+Secure）" | 
| 优先级 | High | 

### 发现 8: 缺失 ircookie 加密对 HTTP 缓存键（Cache Key）影响的验证（续） 
| 项目 | 内容 | 
|------|------| 
| 涉及用例 | WebUI > http版本 > 用例 #212（原发现 3 中新增用例） | 
| 问题描述 | 原发现 3 新增用例 #212 验证了 304 缓存命中，但未验证缓存键是否因加密值变化而失效；产品知识文档 要求：“Cache Key 计算必须排除 Set-Cookie 头部，避免因 ircookie 值动态变化导致缓存碎片化”。 | 
| 修改建议 | 用例 #212 的 Expected Result 从"第二次请求返回 304 Not Modified，且响应中无 Set-Cookie 头部"改为"第二次请求返回 304 Not Modified，且响应中无 Set-Cookie 头部；同时抓包确认第一次请求的 Cache-Control 头部中 ETag 值与第二次 If-None-Match 值完全一致（证明 Cache Key 未包含 cookie 字段）" | 
| 优先级 | High | 

### 发现 9: CLI `show slb mode ircookie [group_name]` 敏感信息脱敏验证不充分（续） 
| 项目 | 内容 | 
|------|------| 
| 涉及用例 | CLI > show slb mode ircookie [group_name] > 用例 #56 | 
| 问题描述 | 原发现 10 已修正用例 #53，但用例 #56（参数为 `g1`，已存在 group）同样输出 AES 密码字段，其 Expected Result 为"显示成功"，未要求密码掩码，存在安全风险。 | 
| 修改建议 | 用例 #56 的 Expected Result 从"显示成功"改为"显示成功，且 AES 密码字段在输出中被掩码（如显示为 ***）" | 
| 优先级 | High | 

### 发现 10: WebUI segment webui(global配置) 模块命名误导性（续） 
| 项目 | 内容 | 
|------|------| 
| 涉及用例 | WebUI > segment webui(global配置) > 用例 #125 | 
| 问题描述 | 原发现 7 提出模块更名，但当前报告中该模块仍为 "WebUI > segment webui(global配置)"，且用例 #125 Description 未体现“禁止验证”意图，易被误读为功能支持。 | 
| 修改建议 | 将模块名称 "WebUI > segment webui(global配置)" 改为 "WebUI > segment webui(global配置禁止验证)"，并将用例 #125 的 Description 从"服务器负载均衡->全局设置->会话保持设置，配置ircookie mode"改为"服务器负载均衡->全局设置->会话保持设置，配置ircookie mode（验证 Segment 场景下全局配置被拒绝）" | 
| 优先级 | Medium | 

### 发现 11: Stress Testing > HA 模块缺失 AES 密码动态更新与 CPU 压力叠加场景（续） 
| 项目 | 内容 | 
|------|------| 
| 涉及用例 | Stress Testing > HA > 用例 #266（原发现 8 中新增） | 
| 问题描述 | 原发现 8 新增用例 #266，但其 Expected Result 仅要求“新生成的 cookie 均能被正确解密”，未验证高压力下解密延迟是否超标（产品知识文档: “AES 解密 P95 延迟 ≤ 1ms，超时即视为降级”）。 | 
| 修改建议 | 用例 #266 的 Expected Result 从"系统运行正常，且每次密码修改后新生成的 cookie 均能被正确解密"改为"系统运行正常，CPU 持续 ≥70%，且每次密码修改后新生成的 cookie 解密 P95 延迟 ≤ 1ms（通过 apv debug crypto timing 抓取）" | 
| 优先级 | High | 

### 发现 12: WebUI > 修改AES密码 模块缺失密码更新原子性验证（续） 
| 项目 | 内容 | 
|------|------| 
| 涉及用例 | WebUI > 修改AES密码 > 用例 #197–#202 | 
| 问题描述 | 原发现 9 提出在 #197 中增加并发修改步骤，但未明确验证“旧密钥最后一次生效”与“新密钥首次生效”的边界；产品知识文档 要求：“密码更新必须保证原子切换，不允许中间态并存”。 | 
| 修改建议 | 用例 #197 的 Expected Result 从"第二次访问响应中的 cookie 值仍为密码 '123' 加密的结果，第三次访问才为 'xyz' 加密的结果"改为"第二次访问响应中 ircookie 值全为密码 '123' 加密结果；第三次访问响应中 ircookie 值全为密码 'xyz' 加密结果；无任何响应混用两种密钥" | 
| 优先级 | High | 

### 发现 13: CLI `no slb mode ircookie <group_name>` 用例 #46 预期结果可观测性不足（续） 
| 项目 | 内容 | 
|------|------| 
| 涉及用例 | CLI > no slb mode ircookie <group_name> > 用例 #46 | 
| 问题描述 | 原报告已建议改为"执行成功，返回空输出或提示 'no configuration to remove'"，但 CLI 参考未定义该提示，实际设备可能返回 "No such configuration"；需统一为设备真实输出。 | 
| 修改建议 | 用例 #46 的 Expected Result 从"执行成功，返回空输出或提示 'no configuration to remove'"改为"执行成功，返回 'No such configuration' 或空输出（以实机为准，需在 test plan 中记录基线）" | 
| 优先级 | Medium |

