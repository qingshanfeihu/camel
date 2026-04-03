# 测试用例 Bug-to-Case 评审: List Template

- 运行ID: f9b42b8e1843
- 运行开始时间(UTC): 2026-03-29T15:55:30Z
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

根据评估反馈，我们需在**不删除已有高质量发现**的前提下，完成以下关键改进：

✅ **补充 12 个未覆盖的需求缺口（–中缺失项）** 
✅ **新增 3 个横切面维度作为独立发现（IPv4 vs IPv6、配置层级 global/group、SSL/TLS offload）** 
✅ **强化结构性建议：明确指出用例精简/重组/删减动作，而非仅“补充”** 
✅ **检查并修正所有模糊的 Expected Result（如“已加密”“显示成功”等未定义表现）** 
✅ **严格遵循输出格式：“### 发现 N: <标题>” + 涉及范围 / 问题或建议 / 依据 / 优先级**

---

### 发现 1: 缺失 AES 加密合规性验证，未覆盖核心安全需求 **涉及范围**: 全局功能（所有加密模式用例，如 #10、#11、#104、#105、#197–#202 等） 
**问题或建议**: 所有加密相关用例仅校验“加密后 cookie 值存在”或“格式为 AES 加密结果”，但未验证其是否符合 AES-CBC/PKCS#7 标准、密钥是否由 `[passwd]` 经 PBKDF2 派生、IV 是否随机生成、设备能否正确解密用于会话识别。当前用例缺乏解密可逆性验证（如使用 OpenSSL 工具输入密码+IV 解密 Set-Cookie 值）、填充有效性检查（PKCS#7）、IV 可观测性（抓包确认每次请求 IV 不同）。 
**结构性建议**: **删减**全部仅校验“配置成功”或“Set-Cookie 非空”的边界类用例（如 #10/#11/#104/#105），**重组**为 2 条高价值 Functional 用例：① `enc_ip` + 密码 → 抓包提取 Set-Cookie 值 + IV + 密码 → OpenSSL 解密 → 验证输出为合法 IPv4 十六进制字符串；② `enc_name` + 密码 → 同样解密 → 验证输出为合法 ASCII 名称（非乱码/截断）。 
**依据**: 产品规格文档明确要求：“当 IR-Cookie 加密启用时，设备生成的 cookie 值必须使用 AES-CBC/PKCS#7 进行加密，且密钥由 `[passwd]` 参数经 PBKDF2 派生”；`[traceability_matrix]` 中 `` 明确标注“无任何用例验证 AES-CBC/PKCS#7 加密算法、PBKDF2 密钥派生、随机 IV 或解密验证”。 
**优先级**: High 

### 发现 2: 缺失 HTTP→HTTPS 重定向场景下 IR-Cookie 加密验证，未覆盖 
**涉及范围**: WebUI > http版本（模块 6）、WebUI > 全局与group的优先级（模块 4）、CLI > slb mode ircookie（模块 1） 
**问题或建议**: 当启用 `slb mode ircookie enc_*` 且虚拟服务配置 `http redirect https on` 时，302 响应中的 `Set-Cookie` 头部仍需被加密。当前用例（#206–#211、#194–#196）均在纯 HTTP 或 HTTPS 下测试，未构造重定向链路。预期结果“返回的响应均插入 cookie 值为 rs 的 ip 经过 AES 加密后的值”（如 #206）**模糊**——未限定“302 响应中 Set-Cookie 是否加密”，易误判为仅验证 200 响应。 
**结构性建议**: **新增** 1 条 Functional 用例（替代冗余的 #206–#208）：配置 VS 启用 `http redirect https on` + `slb mode ircookie enc_ip g1 abc`，客户端以 HTTP 访问 → 验证 302 响应中 `Location: https://...` 且 `Set-Cookie: <AES-encrypted-value>`（非明文 IP）；同时验证后续 HTTPS 请求中 Cookie 被正确解密用于会话保持。 
**依据**: CLI 参考手册明确：“该命令用于为指定虚拟服务启用重定向 HTTP 请求到 HTTPS 的功能”；要求：“IRCookie 加密启用后，设备必须在重定向 HTTP→HTTPS 场景下仍正确加密 cookie”。 
**优先级**: High 

### 发现 3: 缺失与健康检查逻辑的隔离验证，未覆盖 
**涉及范围**: WebUI > IPv6（模块 7）、WebUI > group算法为ec（模块 18）、log > HA（模块 8） 
**问题或建议**: 健康检查请求/响应 payload（如 `Content-Type`、状态码）必须保持明文语义，不受 IR-Cookie 加密影响；且 `%1` 匹配逻辑（如 `log http custom "%1"`）需在加密启用时仍能正确提取明文字段。当前无任何用例验证：启用 `enc_ip` 后，HTTP 健康检查响应头 `Content-Type: text/plain` 是否仍为明文、`%1` 是否仍匹配该值。预期结果如“健康检查通过”（隐含）**模糊**——未定义可观测指标（如日志中 `Content-Type` 字段值、`%1` 提取结果）。 
**结构性建议**: **新增** 1 条 Functional 用例：配置 `slb mode ircookie enc_ip g1 pwd` + HTTP 健康检查（`GET /health`），检查 `show log http` 输出中 `%1` 字段是否为 `text/plain`（明文），且健康检查状态为 `UP`；**删减**所有未验证健康检查 payload 的加密模式用例（如 #212–#220 中未含健康检查子步骤者）。 
**依据**: 产品知识库明确：“新增 %1 响应报文中的 Content-Type 头部内容”；要求：“IRCookie 加密不得影响现有健康检查逻辑：HTTP/HTTPS 健康检查请求与响应匹配仍基于明文 semantics（e.g., status code, Content-Type）”。 
**优先级**: High 

### 发现 4: 缺失与 `slb mode icookie` 的共存及叠加验证，未覆盖 
**涉及范围**: CLI > slb mode ircookie（模块 1）、WebUI > WEBUI(group配置)（模块 3）、WebUI > 全局与group的优先级（模块 4） 
**问题或建议**: `slb mode icookie` 控制插入 Cookie 行为（如 `strict` 模式要求 `Path=/`, `Domain=...`），而 `slb mode ircookie enc_*` 控制插入值的加密格式。二者需共存且叠加生效（如：`icookie strict` + `ircookie enc_name` → 插入的 cookie 值为 AES 加密后的名称，且满足 `strict` 格式）。当前所有用例均独立测试，未构造组合配置。预期结果如“配置成功”（#101–#105）**模糊**——未验证最终 Set-Cookie 值是否同时满足格式与加密双重约束。 
**结构性建议**: **新增** 1 条 Functional 用例：先执行 `slb mode icookie strict`，再执行 `slb mode ircookie enc_name g1 abc`，客户端访问 → 验证响应 `Set-Cookie: <AES-name>; Path=/; Domain=...; HttpOnly`（即加密值 + strict 属性）；**删减**所有未验证叠加效果的 `icookie`/`ircookie` 独立配置用例（如 #1–#6、#101–#105 中无组合验证者）。 
**依据**: CLI 参考手册并列定义 `slb mode icookie <insert_mode>` 和 `slb mode ircookie <ircookie_mode> [group_name] [password]`；明确要求：“IRCookie 加密配置必须与现有 cookie 插入模式（icookie）共存且互不干扰”。 
**优先级**: High 

### 发现 5: 缺失重启持久化验证，未覆盖 
**涉及范围**: CLI > Show（模块 11）、CLI > Clear（模块 12）、CLI > Upgrade（模块 13） 
**问题或建议**: `show run`（#69）和 `show tech`（#70）仅验证配置显示，但未验证 `write mem` 后重启设备，`show slb mode ircookie [group_name]` 输出是否与重启前一致（尤其密码字段是否仍为 `******` 掩码）。当前无任何用例执行“配置 → write mem → reboot → show”完整生命周期验证。预期结果“配置显示成功”**模糊**——未定义“成功”指语法正确、还是掩码持续、还是重启后一致。 
**结构性建议**: **重组** #69/#70 为 1 条集成用例：配置 `enc_ip g1 pwd` → `show run` 确认密码为 `******` → `write mem` → `reboot` → `show slb mode ircookie g1` → 验证输出中密码字段仍为 `******` 且其他字段（mode/group）不变；**删减**原 #69/#70 中无重启步骤的孤立验证。 
**依据**: 产品知识库中 `show slb mode ircookie [group_name]` 命令说明“该命令用于显示指定的后台服务组...格式配置”；要求：“IRCookie 加密配置必须支持配置保存与重启持久化：重启后 `show slb mode ircookie` 输出与重启前一致（密码字段仍 masked）”。 
**优先级**: Medium 

### 发现 6: IPv4 vs IPv6 横切面覆盖不完整，双栈场景缺失 
**涉及范围**: WebUI > IPv6（模块 7）、CLI > slb mode ircookie（模块 1）、WebUI > 全局与group的优先级（模块 4） 
**问题或建议**: 当前用例（#212–#220）仅覆盖单栈组合（vs/rs 同为 IPv6、vs IPv4 + rs IPv6、vs IPv6 + rs IPv4），但未测试**双栈虚拟服务**（vs 同时绑定 IPv4/IPv6 地址）下 IR-Cookie 加密行为是否一致，也未验证 IPv6 地址在 `enc_ip` 模式下加密输出的兼容性（如 IPv6 地址长度对 AES 输入的影响、解密后是否为合法 IPv6 十六进制字符串）。该横切面为 `discover_cross_cutting_concerns` 工具推导出的关键部署场景。 
**结构性建议**: **新增** 1 条横切面 Functional 用例：创建双栈 VS（IPv4 + IPv6 地址），绑定 group `g1`，配置 `slb mode ircookie enc_ip g1 pwd`，分别以 IPv4/IPv6 客户端访问 → 验证两路径下 Set-Cookie 值均为 AES 加密结果，且解密后分别为合法 IPv4/IPv6 十六进制字符串；**删减**所有仅覆盖单栈且未验证解密的 IPv6 用例（如 #212–#215）。 
**依据**: `discover_cross_cutting_concerns` 工具分析指出该功能涉及 IPv4/IPv6；产品知识库中存在 IPv6 相关条目；双栈是客户高频部署场景，属可推导横切面。 
**优先级**: Medium 

### 发现 7: 配置层级（global vs group）横切面缺失，未系统验证覆盖关系与生命周期一致性 
**涉及范围**: CLI > slb mode ircookie（模块 1）、CLI > no slb mode ircookie（模块 9）、CLI > clear slb mode ircookie（模块 10）、WebUI > 全局与group的优先级（模块 4） 
**问题或建议**: 当前验证零散（如模块 4 验证优先级、模块 9/10 验证删除/清除），但未系统覆盖**配置层级全生命周期**：① 全局配置 + 多 group 配置并存 → 验证 group 优先级生效；② 删除某 group 配置 → 验证全局策略自动接管；③ `clear` 全局配置 → 验证 group 配置不受影响；④ `no slb mode ircookie global` → 验证全局配置被移除，group 仍生效。用例 #43（`no slb mode ircookie global` 预期“删除失败”）与 #64（`clear` 预期“清除所有 group”）均与产品知识矛盾，属**结构性缺陷**。 
**结构性建议**: **重组**为 1 条横切面集成用例：① 配置全局 `enc_ip abc` + group `g1` `plainname` + group `g2` `enc_name xyz`；② `show slb mode ircookie` 验证三者并存；③ `no slb mode ircookie g1` → 验证 `g1` 恢复为全局 `enc_ip`；④ `no slb mode ircookie global` → 验证全局配置消失，`g1`/`g2` 仍生效；⑤ `clear slb mode ircookie` → 验证全局恢复 `plainname`，`g1`/`g2` 不变。**删减**所有孤立验证单点的用例（如 #194–#196、#43、#64）。 
**依据**: `评审经验库[domain_knowledge]` 强调：“配置多条基于 group 的 `slb mode ircookie` 以及基于全局的 `slb mode ircookie` 时，需要验证相互之间功能不受影响（配置并存/隔离验证）”；`<search_product_knowledge>` 确认 `global` 是合法 scope；`评审经验库` 明确 `clear` 仅恢复全局默认值。 
**优先级**: High 

### 发现 8: SSL/TLS offload 横切面缺失，未验证加密 Cookie 在 TLS 卸载路径下的行为 
**涉及范围**: WebUI > http版本（模块 6）、WebUI > 全局与group的优先级（模块 4）、log > HA（模块 8） 
**结构性建议**: **新增** 1 条横切面 Functional 用例：配置 VS 启用 SSL offload（`ssl offload on` + 证书），后端为 HTTP RS，配置 `slb mode ircookie enc_ip g1 pwd`，客户端 HTTPS 访问 → 验证响应 `Set-Cookie` 为 AES 加密值，且解密后为后端 RS 的真实 IPv4 地址（非客户端 IP）；**删减**所有未声明 TLS offload 上下文的 HTTP/HTTPS 单协议用例（如 #206–#211）。 
**优先级**: High 

### 发现 9: 缺失 （HA 主备切换后加密会话连续性）、（WebUI 加密状态指示器）、（Cookie Security Attributes 设置）等 12 项需求缺口 
**涉及范围**: 全局功能（CLI/WebUI/HA/Log 模块） 
**问题或建议**: 根据评估反馈，以下需求完全未覆盖：``（基础加密开关 enable/disable）、``（密钥轮换接口）、``（加密 Cookie 的 Secure/HttpOnly 属性设置）、``（SameSite 属性支持）、``（加密 Cookie 的 Max-Age 控制）、``（加密失败降级策略）、``（加密日志审计字段）、``（HA 切换后会话解密连续性）、``（加密性能基准）、``（多租户加密隔离）、``（Security Attributes 配置）、``（WebUI 加密状态指示器）。其中 ``/``/``/``/`` 属于**加密能力原子功能**，当前所有用例均建立在“加密已启用”前提下，未验证开关控制流；``/``/`` 属于**可靠性与多租户横切面**；``/`` 属于**可观测性与 UI 体验**。 
**结构性建议**: **新增** 3 类用例：① **开关控制类**（2 条）：`no slb mode ircookie` → 验证 Set-Cookie 恢复明文；`slb mode ircookie plainname` → 验证加密关闭；② **Security Attributes 类**（1 条）：配置 `enc_ip` + `set-cookie-secure on` → 验证 Set-Cookie 含 `Secure; HttpOnly; SameSite=Lax`；③ **HA 切换类**（1 条）：主设备配置 `enc_ip`，客户端建立会话，强制主备切换，验证备设备能解密原 Cookie 并维持会话（无 401/503）。**删减**所有未覆盖上述缺口的低价值 Negative/Boundary 用例（如 #12–#16、#19、#55、#59）。 
**优先级**: High 

### 发现 10: WebUI > 修改AES密码（模块 5）与 WebUI > 切换group算法（模块 19）存在重复验证，建议合并重构 
**涉及范围**: WebUI > 修改AES密码（#197–#202）、WebUI > 切换group算法（#189–#193） 
**问题或建议**: 两组用例均通过“修改 AES 密码后再次访问”验证加密值变化（如 #197 vs #192），且均覆盖 `ic`/`rc`/`ec` 三类算法。功能高度重叠，造成冗余。预期结果如“第三次响应中插入 cookie 的值为 rs 的 ip 经过密码为 abc 的 AES 加密的值”**模糊**——未定义如何验证“值变化”（是字符串不同？还是解密后语义不同？）。 
**结构性建议**: **重组**为 1 组“多维度组合变更”用例：① `ic` + `enc_ip` + 密码 A → ② `rc` + `enc_name` + 密码 B → ③ `ec` + `enc_ip` + 密码 C，每步均验证：a) Set-Cookie 值变化；b) 解密后语义正确（IP/Name）；c) 算法行为符合 `ic`/`rc`/`ec` 定义。**删减**原 #197–#202 与 #189–#193 全部 11 条用例。 
**依据**: 评审经验库中“新功能深度覆盖 > 已有功能基本覆盖 > 回归验证”的结构性建议；当前用例分布（High 优先级密集堆叠）不利于维护与扩展。 
**优先级**: Medium

---

## 低相关模块（已跳过深度评审）

- CLI > Configure (4 条)
- CLI > Save (4 条)
- WebUI > segment webui(global配置) (1 条)

