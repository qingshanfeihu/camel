# 测试用例 Bug-to-Case 评审: List Template

- 运行ID: ffb0594b7949
- 运行开始时间(UTC): 2026-03-29T14:43:15Z
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

### 发现 1: REQ_007 控制字符与非法密码输入未覆盖
**涉及范围**: CLI > slb mode ircookie <ircookie_mode> [group_name] [passwd]（用例 #23–#37）
**问题或建议**: 当前所有密码边界测试（如 #24、#26、#27、#30、#32、#33）均使用可打印 ASCII 字符，未覆盖控制字符（TAB/CR/LF/NUL）、空格及非 ASCII 字符（如中文、Unicode 符号）的拒绝行为。REQ_007 明确要求校验并拒绝此类非法输入，但无任何用例验证。
**依据**: `<bug_context>` 中明确指出：“当 [passwd] 包含控制字符、空格或非 ASCII 字符时，slb mode ircookie 应校验并拒绝非法输入”；`<traceability_matrix>` 确认该需求缺口为“无用例验证”。
**优先级**: High

### 发现 2: REQ_010 ircookie 与 icookie 模式共存正交性缺失
**涉及范围**: CLI > slb mode ircookie / slb mode icookie 集成点
**问题或建议**: 无任何用例验证 `slb mode ircookie enc_ip` 与 `slb mode icookie on` 同时启用时的行为：二者应互不干扰，即加密 cookie 插入与明文 cookie 插入可并存且各自生效。当前用例仅独立验证各模式，未构造共存场景（如先配 `icookie on`，再配 `ircookie enc_ip`，观察响应中是否同时存在两种 cookie 或行为隔离）。
**依据**: `<product_knowledge>` 多次引用 `slb mode icookie` 与 `slb mode ircookie` 并列存在；`<traceability_matrix>` 明确 REQ_010 缺口为“无任何用例验证共存配置及行为正交性”。
**优先级**: High

### 发现 3: REQ_011 加密 Cookie 端到端会话保持闭环缺失
**涉及范围**: WebUI > http版本 / WebUI > IPv6 / WebUI > 修改AES密码（用例 #206–#211, #212–#220, #197–#202）
**问题或建议**: 所有功能用例均验证“设备插入加密 cookie”，但未验证“客户端携带该加密 cookie 再次请求 → 设备成功解密 → 实现会话保持”的完整闭环。例如，用例 #206 仅断言“返回响应中插入 AES 加密值”，未构造二次请求并验证后端服务命中一致性（persistence hit count 或 rs session affinity）。
**依据**: `<product_knowledge>` 定义 `slb mode ircookie` 用于“cookie值中后台服务信息的格式”，其根本目标是支撑会话保持；`<traceability_matrix>` 明确 REQ_011 缺口为“无用例验证客户端携带加密 cookie 后设备成功解密并实现会话保持”。
**优先级**: High

### 发现 4: REQ_012 clear 操作对已建立会话影响未验证
**涉及范围**: CLI > clear slb mode ircookie（用例 #64）、WebUI > 修改AES密码（用例 #197–#202）
**问题或建议**: 用例 #64 仅验证“clear 后全局恢复为 plainname”，但未验证 clear 前已建立的加密会话连接是否仍有效（即已解密的 session 缓存是否保留），以及新插入 cookie 是否确实明文化。需补充场景：1) 配置 `enc_ip` 并触发会话；2) 执行 `clear slb mode ircookie`；3) 对同一客户端再次发送请求，验证：a) 响应中 cookie 为明文（非 AES）；b) 会话仍命中原 rs（缓存未失效）。
**依据**: `<change_impact>` 明确要求“clear 后已建立的加密会话连接不受影响（即已解密的 session 缓存仍有效），但新插入 cookie 不再加密”；`<traceability_matrix>` 确认此为高优先级缺口。
**优先级**: High

### 发现 5: REQ_015 show 输出审计元信息缺失验证
**涉及范围**: CLI > show slb mode ircookie [group_name]（用例 #48–#59）
**问题或建议**: 用例 #53、#56 等仅验证“显示成功”及“内容可见”，未验证 `show slb mode ircookie` 输出是否包含加密算法标识（如 `aes-128-cbc`）、密钥派生方式（如 `pbkdf2-sha256`）及迭代次数（`100000`）。这些是安全审计必需字段，但当前无任何用例解析并比对输出文本。
**依据**: `<change_impact>` 明确要求“show 输出应包含...等可审计元信息”；`<traceability_matrix>` 确认该缺口存在。
**优先级**: High

### 发现 6: REQ_016 性能压测指标未量化
**涉及范围**: Stress Testing > HA（用例 #258–#265）
**问题或建议**: 所有压力用例（如 #258）仅预期“系统运行正常”，未定义可测量的性能指标：CPU 使用率增幅是否 <15%？Cookie 插入延迟是否 ≤10ms？CPS 是否 ≥1000？缺乏量化验收标准，无法判定性能是否达标。
**依据**: `<change_impact>` 明确要求“不导致 CPU 使用率异常升高（<15% 增幅）或 cookie 插入延迟超 10ms”；`<traceability_matrix>` 确认“无性能压测用例”。
**优先级**: High

### 发现 7: REQ_018 RFC 6265 Set-Cookie 长度兼容性缺失
**涉及范围**: WebUI > 修改AES密码 / WebUI > group算法为ec（用例 #197–#202, #181, #182, #185, #186）
**问题或建议**: 当 AES 密码较长（如 128 字符）且后端服务名/IP 较长时，加密后 cookie 值可能超过 RFC 6265 规定的 4096 字节上限。当前用例未验证超限时的截断、分片或错误处理行为（如是否拒绝配置、是否静默截断、是否返回 HTTP 500）。
**依据**: `<change_impact>` 明确要求“设备向客户端响应中插入的 Set-Cookie 头部长度不超过 4096 字节（RFC 6265 兼容）”；`<product_knowledge>` 引用 `Set-Cookie头部包含...`，证实协议约束存在。
**优先级**: High

### 发现 8: REQ_020 与安全 Cookie（secure flag）功能正交性缺失
**涉及范围**: WebUI > http版本 / CLI > slb mode ircookie（用例 #206–#211）
**问题或建议**: `<product_knowledge>` 明确存在 `http rewrite response cookie secure` 功能，且 `slb mode ircookie` 与之同属会话保持范畴。但无任何用例验证二者共存：例如，启用 `http rewrite response cookie secure on` 并同时配置 `ircookie enc_ip`，应确保 Set-Cookie 头部同时包含 `secure` 标志与 AES 加密值，且互不影响。
**依据**: `<product_knowledge>` 提供 `http rewrite response cookie secure` CLI 参考；`<traceability_matrix>` 明确 REQ_020 为未覆盖需求。
**优先级**: Medium

### 发现 9: CLI Negative 用例预期结果模糊（大小写敏感性）
**涉及范围**: CLI > slb mode ircookie <ircookie_mode>（用例 #12–#16）
**问题或建议**: 用例 #12（`PLAINNAME`）等预期为“配置失败”，但未说明失败原因。CLI 参考中所有合法模式均为小写（`plainname`），大小写敏感是默认行为。预期结果应明确为“命令拒绝执行，提示未知模式”，而非笼统“配置失败”，否则无法与日志比对。
**依据**: `<cli_reference>` 中所有示例均为小写；`<review_experience_memory>` 强调“`plainname` 是默认模式”，未提及其他变体；`<bug_context>` Fixed Details 亦仅列小写形式。
**优先级**: Medium


<!-- 原发现 10 已被对抗验证器驳回(高置信度)，已移除 -->

### 发现 10: IPv6 地址族支持未覆盖加密 Cookie 解析闭环
**涉及范围**: WebUI > IPv6（用例 #212–#220）
**问题或建议**: 用例 #212–#220 验证了 IPv6 场景下加密 cookie 的插入，但未验证“客户端携带 IPv6 地址加密 cookie 回传 → 设备正确解密并匹配 IPv6 rs”的会话保持闭环。IPv6 地址长度远超 IPv4，对加密/解密路径构成额外压力，需端到端验证。
**依据**: `<product_knowledge>` 明确 SLB “对SLB功能提供了广泛的IPv6支持”；`<discover_cross_cutting_concerns>` 将 IPv6 列为必须检查的横切面；`<traceability_matrix>` 已覆盖 IPv6 插入，但未覆盖解析闭环。
**优先级**: Medium

### 发现 11: HTTP/HTTP2 协议版本兼容性未覆盖加密 Cookie 解析闭环
**涉及范围**: WebUI > http版本（用例 #206–#211）
**问题或建议**: 用例 #206–#211 仅验证不同 HTTP 版本下加密 cookie 的插入，未验证“客户端在 HTTP/2 下携带加密 cookie → 设备在 HTTP/1.1 后端转发时能否正确解密并维持会话”。协议栈转换是常见集成点，需补充跨版本解析验证。
**依据**: `<discover_cross_cutting_concerns>` 将“协议覆盖”列为首要横切面；`<product_knowledge>` 提及“HTTP和HTTP2健康检查”，证实多协议共存；`<traceability_matrix>` 仅覆盖插入，未覆盖跨协议解析。
**优先级**: Medium

### 发现 12: HA 主备切换后加密会话状态同步未验证
**涉及范围**: log > HA（用例 #246–#247, #251–#252, #256–#257）
**问题或建议**: 用例 #246 验证主备切换后“插入 cookie 值为 AES 加密”，但未验证：1) 切换前已建立的加密会话，在备机接管后是否仍能解密并命中原 rs；2) 切换过程中是否存在会话丢失或解密失败。HA 场景下 session cache 同步是关键风险点。
**依据**: `<product_knowledge>` 多次提及 HA 与 SLB 集成；`<change_impact>` 要求“高可用性”覆盖；`<traceability_matrix>` 未覆盖 HA 下的会话状态一致性。
**优先级**: High

### 发现 13: 多 group + 全局配置并存隔离性未验证
**涉及范围**: WebUI > 全局与group的优先级（用例 #194–#196）、WebUI > 多个group（用例 #203–#205）
**问题或建议**: 用例 #194–#196 验证了 group 优先于全局的覆盖关系，但未验证“多个 group 分别配置不同加密模式（如 g1=enc_ip, g2=enc_name）+ 全局=plainname”时，各 group 的加密行为是否完全隔离、互不干扰。缺少并发配置下的冲突与隔离验证。
**依据**: `<review_experience_memory>` 第4条明确要求“配置多条基于 group 的...需验证相互之间功能不受影响（配置并存/隔离验证）”；`<traceability_matrix>` 已覆盖单 group 优先级，但未覆盖多 group 并存。
**优先级**: Medium

### 发现 14: WebUI 配置的 AES 密码未验证持久化与重启恢复
**涉及范围**: WebUI > WEBUI(group配置)（用例 #101–#124）、CLI > Show（用例 #69–#70）
**问题或建议**: 用例 #69–#70 验证 `show run` 显示配置，但未验证 WebUI 配置的 AES 密码在 `write mem` 后是否真正持久化，以及设备重启后是否仍生效。密码若未落盘或重启丢失，将导致生产环境会话中断。
**依据**: `<review_experience_memory>` 未直接提及，但 `<product_knowledge>` 中 `show run` 与 `write mem` 是标准持久化链路；`<change_impact>` 要求“功能增强”需覆盖基础可靠性。
**优先级**: Medium

[溯源校验摘要] 
无剔除意见。所有提出的需求缺口（REQ_007/010/011/012/015/016/018/020）均在 `<traceability_matrix>` 中明确定义为“未覆盖”，且 `<bug_context>` 或 `<change_impact>` 提供原始依据；所有横切面（IPv6、HTTP/2、HA、多 group）均通过 `<discover_cross_cutting_concerns>` 或 `<product_knowledge>` 可推导；所有 CLI 语法与语义判断均基于 `<cli_reference>` 和 `<review_experience_memory>` 强约束。


---
<adversarial_verification>
【对抗验证结果】确认 14, 驳回 1, 待定 0

F001 [确认] REQ_007 控制字符与非法密码输入未覆盖
F002 [确认] REQ_010 ircookie 与 icookie 模式共存正交性缺失
F003 [确认] REQ_011 加密 Cookie 端到端会话保持闭环缺失
F004 [确认] REQ_012 clear 操作对已建立会话影响未验证
F005 [确认] REQ_015 show 输出审计元信息缺失验证
F006 [确认] REQ_016 性能压测指标未量化
F007 [确认] REQ_018 RFC 6265 Set-Cookie 长度兼容性缺失
F008 [确认] REQ_020 与安全 Cookie（secure flag）功能正交性缺失
F009 [确认] CLI Negative 用例预期结果模糊（大小写敏感性）
F010 [驳回] “global” 作为 group_name 的语义混淆
 反证: 用例 #20: Expected Result = '配置成功'; 用例 #43: Expected Result = '删除失败(仅支持删除组的配置)'; 用例 #54: Expected Result = '显示失败'
F011 [确认] IPv6 地址族支持未覆盖加密 Cookie 解析闭环
F012 [确认] HTTP/HTTP2 协议版本兼容性未覆盖加密 Cookie 解析闭环
F013 [确认] HA 主备切换后加密会话状态同步未验证
F014 [确认] 多 group + 全局配置并存隔离性未验证
F015 [确认] WebUI 配置的 AES 密码未验证持久化与重启恢复
</adversarial_verification>

---

## 低相关模块（已跳过深度评审）

- CLI > Configure (4 条)
- CLI > Save (4 条)

