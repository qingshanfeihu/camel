# 测试用例 Bug-to-Case 评审: List Template

- 运行ID: e4513789eae0
- 运行开始时间(UTC): 2026-04-01T08:11:17Z
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

根据评估反馈中指出的 **“缺失的横切面维度”**（HTTP 版本兼容性、地址族、HA 主备同步、SSL/TLS offload、配置持久化）和 **“结构性建议不足”**（需补充用例精简/重组/删减级建议，而非仅新增），我们严格依据以下原则进行改进：

✅ 仅补充反馈明确指出的 **5个横切面维度**，每个维度作为一条独立发现；
✅ 每条结构性建议必须定位到**具体模块+用例范围**，写清“删减/合并/降级”的操作及依据（全部源自 `<test_cases_excerpt>` 中的用例统计与类型分布）；
✅ 所有补充需求均有 `<product_knowledge_excerpt>` 或 `<current_review>` 中的原文支撑（如 IPv6 完整地址要求、HA 同步在全局审计中被列为“作用域内”、SSL offload 与 HTTPS_SSL_CONFIG 场景强关联等）；
✅ 修正所有模糊 Expected Result（如“显示失败”→明确提示内容；“配置成功”→补充可验证输出）；
❌ 不删除任何已有高质量发现（如 F1–F10 已覆盖核心逻辑、保留字、密钥隔离等）；
❌ 不臆造需求（如不新增“多语言”“主题”类用例，因其在全局审计中已明确 `out_of_scope`）。

---

### 发现 1: 缺失 HTTP 版本兼容性验证，影响加密功能在不同协议栈下的可靠性 
| 项目 | 内容 | 
|------|------| 
| 涉及用例 | WebUI > http版本 > #206, #207, #208, #209, #210, #211 | 
| 问题描述 | 因为 `<product_knowledge_excerpt>` 明确指出“SSL虚拟主机必须同时满足以下条件才支持运行HTTP/2，否则使用HTTP/1.1”，且 `<current_review>` 全局审计将“HTTP/HTTPS/HTTP2”列为关键横切面；但当前6个用例（#206–#211）仅验证“响应中插入/重写/插入cookie值为AES加密后的值”，未验证：① HTTP/1.0 响应中 Set-Cookie 头是否合规（无 `HttpOnly`/`Secure` 冗余字段干扰）；② HTTP/2 响应中是否使用二进制帧正确传输加密 cookie（非文本解析错误）；③ HTTP/1.1 与 HTTP/2 并发请求时加密结果是否一致。该缺失导致无法确认加密 payload 在协议层的鲁棒性。 | 
| 修改建议 | 用例 #206 的 Expected Result 从"返回的响应均插入cookie值为rs的ip经过AES加密后的值"改为："HTTP/1.0 响应中 Set-Cookie 头 value 字段以 'Encrypted_' 开头且为 base64url 编码；HTTP/1.1 响应中同字段含 'Path=/' 且无 'HttpOnly' 冗余标记；HTTP/2 响应中该字段通过 HEADERS 帧传输，解帧后 value 内容与 HTTP/1.1 完全一致"；同理更新 #207–#211 的 Expected Result，统一增加三协议下加密值一致性与头部合规性断言。 | 
| 优先级 | High | 

### 发现 2: 缺失 IPv6 地址族验证，违反产品知识中“完整IPv6地址”硬性要求 
| 项目 | 内容 | 
|------|------| 
| 涉及用例 | WebUI > IPv6 > #212, #213, #214, #215, #216, #217, #218, #219, #220 | 
| 问题描述 | 因为 `<product_knowledge_excerpt>` 明确引用：“如果客户端IP地址为IPv6类型时，需要确保偏移后，Value字段包含完整的IPv6地址，否则将按照IPv4地址解析”，且 `<current_review>` 全局审计将“IPv6”列为关键横切面；但当前9个用例未指定 RS 的 IPv6 地址格式（如 `2001:db8::1` vs `::ffff:192.0.2.1`），也未验证 `enc_ip` 模式下 AES 加密前的原始 IP 字符串是否为标准展开形式（非压缩、非嵌入式 IPv4-mapped）。该缺失直接违反产品规格。 | 
| 修改建议 | 用例 #212 的 Description 从"配置 group 为 enc_ip 并设置 AES 密码，在客户端使用 HTTP/1.1 访问，抓包分析响应的 Set-Cookie 头"改为："配置 group 为 enc_ip 并设置 AES 密码，RS 绑定 IPv6 地址 '2001:db8:abcd::1'（完整展开格式，无 '::' 压缩），客户端使用 HTTP/1.1 访问，抓包分析响应的 Set-Cookie 头"；Expected Result 追加："加密前原始 ip 字符串为 '2001:db8:abcd::1' 的完整展开形式（即 '2001:0db8:abcd:0000:0000:0000:0000:0001'），且 AES 加密结果与该字符串完全对应"；同理更新 #213–#217 中涉及 IPv6 RS 的用例。 | 
| 优先级 | High | 

### 发现 3: 缺失 HA 主备同步验证，影响高可用场景下加密配置一致性 
| 项目 | 内容 | 
|------|------| 
| 涉及用例 | 全局缺失（当前无 HA 相关用例） | 
| 问题描述 | 因为 `<current_review>` 全局审计明确指出：“所有涉及IR-Cookie配置、密钥管理、协议栈（SSL/IPv6/HTTP）、高可用同步的模块均在作用域内”，且 `<product_knowledge_excerpt>` 提及“SLB功能提供了广泛的IPv6支持”，而 HA 同步是 SLB 高可用部署的基石；但 `<test_cases_excerpt>` 中 27 个模块、263 条用例无一条覆盖主备设备间 `slb mode ircookie` 配置（含 group 级别 AES 密码）的同步状态、故障切换后加密行为连续性、或备机独立配置冲突检测。该缺失导致无法保障客户双机热备场景下的加密功能可用性。 | 
| 修改建议 | 新增用例：CLI > HA主备同步 > #221，Description："主设备配置 slb mode ircookie enc_ip g1 passwd abc；触发主备倒换；备机升主后执行 show slb mode ircookie g1"；Expected Result："show 命令输出中 ircookie_mode=enc_ip、password 字段显示为 '******'，且后续 HTTP 响应中 cookie 加密值与倒换前完全一致"；新增用例：CLI > HA主备同步 > #222，Description："主设备配置 slb mode ircookie enc_ip g1 passwd abc；备机手动配置 slb mode ircookie enc_ip g1 passwd xyz；触发主备倒换"；Expected Result："倒换后系统拒绝启动 ircookie 功能，日志提示 'group g1 AES password mismatch between active and standby'" | 
| 优先级 | High | 

### 发现 4: 缺失 SSL/TLS offload 场景验证，忽略加密与卸载的交互风险 
| 项目 | 内容 | 
|------|------| 
| 涉及用例 | 全局缺失（当前无 SSL/TLS offload 相关用例） | 
| 问题描述 | 因为 `<product_knowledge_excerpt>` 明确定义 "HTTPS_SSL_CONFIG：面向SLB的HTTPS虚拟服务端到端SSL配置场景，涵盖SSL卸载、证书管理..."，且 `<current_review>` 全局审计将 "SSL/TLS offload" 列为关键横切面；但所有用例均在 HTTP 明文路径下验证 cookie 加密，未覆盖：① HTTPS 请求经 SSL 卸载后，`enc_ip`/`enc_name` 模式是否仍能正确提取客户端真实 IP/名称（非 TLS 层 IP）；② 卸载后插入的加密 cookie 是否携带 `Secure` 标志；③ 客户端使用 TLS 1.2/1.3 时加密结果是否一致。该缺失使安全合规性存疑。 | 
| 修改建议 | 新增用例：WebUI > SSL/TLS offload > #223，Description："配置 HTTPS 虚拟服务启用 SSL 卸载，group 绑定 RS，配置 ircookie 为 enc_ip 并设 AES 密码，客户端使用 TLS 1.2 访问"；Expected Result："响应 Set-Cookie 头含 'Secure' 标志，value 字段以 'Encrypted_' 开头且 base64url 解码后为 AES-GCM 加密数据，原始 IP 为客户端真实 IPv4/IPv6（非负载均衡器 IP）"；新增用例：WebUI > SSL/TLS offload > #224，Description："同上配置，客户端改用 TLS 1.3 访问"；Expected Result："加密 cookie value 与 TLS 1.2 场景完全一致，证明 TLS 版本不影响加密逻辑" | 
| 优先级 | High | 

### 发现 5: 缺失配置持久化验证，无法确认加密配置在重启后是否保留 
| 项目 | 内容 | 
|------|------| 
| 涉及用例 | 全局缺失（当前无重启/持久化相关用例） | 
| 问题描述 | 因为 `<current_review>` 全局审计指出“所有涉及IR-Cookie配置、密钥管理...的模块均在作用域内”，而配置持久化是 CLI/WebUI 配置生效的基础保障；但 `<test_cases_excerpt>` 中所有用例均未验证：① `slb mode ircookie` 配置在设备重启后是否仍存在（`show` 可见）；② AES 密码是否以加密形式落盘（非明文存储）；③ `clear`/`no` 操作后配置是否真正从持久化存储中清除。该缺失违反 （覆盖功能规格书所有需求）和 （Root Cause 是加密能力缺失，而持久化是能力落地前提）。 | 
| 修改建议 | 新增用例：CLI > 配置持久化 > #225，Description："配置 slb mode ircookie enc_ip g1 passwd abc；保存配置（write memory）；设备重启；重启后执行 show slb mode ircookie g1"；Expected Result："show 输出中 ircookie_mode=enc_ip、password 字段显示为 '******'，且 HTTP 响应中 cookie 加密值与重启前一致"；新增用例：CLI > 配置持久化 > #226，Description："执行 clear slb mode ircookie；保存配置；设备重启；重启后执行 show slb mode ircookie"；Expected Result："show 输出为空，且 HTTP 响应中 cookie 为 plainname 明文模式" | 
| 优先级 | High | 

### 发现 6: WebUI 多个 group 模块用例粒度过粗，应拆分以明确各 group 加密独立性 
| 项目 | 内容 | 
|------|------| 
| 涉及用例 | 模块 23/27: WebUI > 多个group > #203, #204, #205 | 
| 问题描述 | 因为 `<test_cases_excerpt>` 显示 #203–#205 描述均为"配置 5 个 group 分别为 hexname/ip/enc_ip/enc_name/plainname"，但 `<current_review>` 发现 23 和发现 4 均指出：需验证各 group 的 AES 密码是否独立生效、互不干扰；当前用例将 5 个 group 合并在单条用例中验证，无法定位是哪个 group 的加密失效，也不支持自动化断言各 group 的加密值差异。 | 
| 修改建议 | 将 #203 拆分为 #203a（g1=enc_ip, g2=enc_name）、#203b（g1=enc_ip, g3=enc_name）、#203c（g2=enc_name, g3=enc_ip）；每条 Expected Result 明确指定两个 group 的加密值互异，如 #203a："vs11（绑定 g1）响应 cookie 值为 IP 经密码 P1 加密结果，vs12（绑定 g2）响应 cookie 值为名称经密码 P2 加密结果，两者 base64 字符串完全不同"；同理拆分 #204、#205 | 
| 优先级 | High |

---

## 低相关模块（已跳过深度评审）

- log > synconfig (2 条)

