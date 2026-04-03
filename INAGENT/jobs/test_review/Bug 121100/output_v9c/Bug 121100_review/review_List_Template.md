# 测试用例 Bug-to-Case 评审: List Template

- 运行ID: 85f6c04bc77d
- 运行开始时间(UTC): 2026-03-29T14:55:30Z
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

### 发现 1: CLI 配置命令中 `group_name` 为空的语义不明确，预期结果不可验证 
**涉及范围**: 用例 #18（模块 1/28） 
**问题或建议**: 用例 #18 描述为“配置该参数为空”，对应命令形如 `slb mode ircookie plainname`（无 group_name）。但 `<cli_reference>` 明确语法为 `slb mode ircookie <ircookie_mode> [group_name] [password]`，其中 `[group_name]` 是可选参数，**空值即省略**，而非传入空字符串。若测试执行 `slb mode ircookie plainname ""`（带空引号），则属非法语法；若执行 `slb mode ircookie plainname`（无后续参数），则合法且等价于全局配置。当前预期结果“配置成功”未说明是全局生效还是 group 级别生效，也未验证 `show slb mode ircookie` 是否显示为 global 或 default，导致结果不可验证。 
**依据**: `<cli_reference>` 中 `slb mode ircookie <ircookie_mode> [group_name] [password]` 的语法定义；`<review_experience_memory>` 第5条指出 `plainname` 是默认模式，`clear slb mode ircookie` 恢复为 `plainname`，因此 `slb mode ircookie plainname` 应等价于清除 group 配置并设为全局默认。 
**优先级**: High 


### 发现 2: AES 密码明文泄露风险未被覆盖，违反安全需求 REQ_008 
**涉及范围**: 全局缺失（无任何用例） 
**问题或建议**: `<traceability_matrix>` 明确指出 `[REQ_008] 加密 cookie 值在设备内部存储和日志中不得以明文形式暴露 [passwd] 或解密后内容`，但全部 265 条用例中无一条验证：`show running-config` 是否隐藏密码、`grep` 日志文件是否含明文密码、`show tech` 是否加密显示。这是高危安全缺口，必须补充至少 3 条用例：① `show run` 输出检查；② `tail -n 100 /var/log/messages | grep -i passwd`；③ `show tech` 密码字段模糊化验证。 
**依据**: `<traceability_matrix>` 中 `[REQ_008]` 缺口声明；`<bug_context>` 强调“客户要求”，安全合规为强制项。 
**优先级**: High 

### 发现 3: HTTP/2 协议兼容性已覆盖，但 IPv6 地址族覆盖不完整 
**涉及范围**: 模块 7/28（WebUI > http版本）、模块 23/28（WebUI > IPv6） 
**问题或建议**: `<check_spec_constraints>` 确认 ircookie 加密支持 HTTP/2（用例 #206–#211 已覆盖）；IPv6 支持也已确认（`<check_spec_constraints>` 返回“SLB 对 SLB 功能提供了广泛的 IPv6 支持”），且模块 23/28 的 9 条用例覆盖了 vs/rs 的 IPv4/IPv6 组合。但所有 IPv6 用例均未验证 `show slb mode ircookie` 在 IPv6 环境下的输出是否正确（如加密字段是否仍被遮蔽），也未测试 `no slb mode ircookie g1` 在 IPv6 组下是否行为一致。建议在模块 23/28 中增加 2 条边界用例：① IPv6 环境下 `show slb mode ircookie g1` 显示加密状态；② IPv6 环境下 `no slb mode ircookie g1` 删除后 `show` 输出为空。 
**依据**: `<check_spec_constraints>` 关于 IPv6 支持的结论；`<product_knowledge>` 中 `show slb mode ircookie [group_name]` 的通用定义。 
**优先级**: Medium 

### 发现 4: HA 主备同步未验证密钥隔离性，违反 REQ_015 
**涉及范围**: 模块 8/28（log > HA）全部 17 条用例 
**问题或建议**: 当前 HA 用例（#241–#257）仅验证功能是否同步成功、cookie 行为是否一致，但 `<traceability_matrix>` 明确 `[REQ_015] 无任何用例验证 HA 同步时加密密钥的安全传输与隔离（如密钥不以明文同步）`。HA 同步过程若明文传输 AES 密码，将导致严重安全风险。必须补充用例：在主设备配置 `enc_ip` + 密码，抓包分析 HA 同步报文（`tcpdump -i any port 514`），验证密钥字段是否加密或不可见。 
**依据**: `<traceability_matrix>` 中 `[REQ_015]` 缺口声明；`<product_knowledge>` 中 `show slb mode ircookie [group_name]` 的输出规范要求“配置AES密码的均已加密”，同步过程必须延续此约束。 
**优先级**: High 

### 发现 5: `debug monitor import ftp` 与 ircookie 加密共存场景完全缺失 
**涉及范围**: 全局缺失（无任何用例） 
**问题或建议**: `<traceability_matrix>` 指出 `[REQ_013] 当 ircookie 加密启用时，'debug monitor import ftp' 流程 must not interfere with or leak encryption keys`，但全部用例中无一条涉及 `debug monitor import ftp` 命令。该命令用于导入健康检查脚本，与 ircookie 同属 SLB 安全子系统，存在共享内存或密钥管理上下文风险。必须补充用例：① 启用 `enc_ip` 后执行 `debug monitor import ftp`；② 验证导入后 ircookie 加密功能不受影响、密钥未重置。 
**依据**: `<traceability_matrix>` 中 `[REQ_013]` 缺口；`<product_knowledge>` 中 `debug monitor import ftp <user_name> <remote_ftp_ip> <remote_file_path>` 的存在证明其为关键集成点。 
**优先级**: High 

### 发现 6: CRL fetch（FTP/HTTP/LDAP）与 ircookie 加密的独立性未验证 
**涉及范围**: 全局缺失（无任何用例） 
**问题或建议**: `<traceability_matrix>` 明确 `[REQ_014] CRL fetch via FTP/HTTP/LDAP ... must remain functional and independent of ircookie encryption configuration`，但现有用例未覆盖 SSL CRL 相关流程。CRL 获取是证书撤销验证核心路径，若 ircookie 加密干扰 CRL 下载，将导致 HTTPS 服务中断。必须补充用例：① 配置 `enc_ip` 后，执行 `ssl crl fetch ftp://...`；② 验证 CRL 成功下载且 ircookie 加密行为不变。 
**依据**: `<traceability_matrix>` 中 `[REQ_014]` 缺口；`<product_knowledge>` 中 `CRL（即证书撤销列表），可以通过HTTP、FTP或LDAP定期从CRL分发点获取CRL文件`。 
**优先级**: High 

### 发现 7: ACL 访问控制与加密 cookie 的集成未覆盖 
**涉及范围**: 全局缺失（无任何用例） 
**问题或建议**: `<product_knowledge>` 明确指出“对于七层的HTTP和HTTPS虚拟服务，如果采⽤了insert cookie策略，设备将在返回给客⼾端的响应消息中插⼊cookie……当客⼾端再次访问虚拟服务时，如果请求中带有设备插⼊的cookie，则该客⼾端不受任何ACL规则的限制”。这表明 ACL 与 ircookie 存在强交互。但 `<traceability_matrix>` 指出 `[REQ_015] 无任何用例验证 ACL-based access control ... must continue to apply correctly to requests carrying encrypted ircookie values`。必须补充用例：① 配置 `acl` 规则 + `enc_ip`；② 客户端携带加密 cookie 访问，验证 ACL 是否仍按规则匹配（如拒绝非白名单 IP）。 
**依据**: `<product_knowledge>` 中 ACL 与 insert cookie 的关系描述；`<traceability_matrix>` 中 `[REQ_015]` 缺口。 
**优先级**: High 

### 发现 8: 多 group 并发配置的竞态条件（Race Condition）未验证 
**涉及范围**: 全局缺失（无任何用例） 
**问题或建议**: `<bug_context>` 明确 `[REQ_016] Concurrent execution of multiple 'slb mode ircookie' commands with different groups must not cause race condition`，但全部用例均为串行执行。缺少并发压力测试：例如，同时在多个终端执行 `slb mode ircookie enc_ip g1 pwd1`、`slb mode ircookie enc_name g2 pwd2`、`no slb mode ircookie g1`，验证最终状态一致性、CPU 是否异常飙升、`show slb mode ircookie` 输出是否无乱码。当前 Stress Testing 模块（#258–#265）仅测试单命令高频修改，未覆盖多 group 并发。 
**依据**: `<bug_context>` 中 `[REQ_016]` 需求；`<review_experience_memory>` 第4条强调“配置多条基于 group 的 slb mode ircookie ……需要验证相互之间功能不受影响”。 
**优先级**: High 


[溯源校验摘要] 
无剔除意见。所有提出的问题均在 `<traceability_matrix>`、`<product_knowledge>`、`<review_experience_memory>` 或 `<bug_context>` 中有明确文本支撑；所有引用的 Case ID（如 #18、#19、#125）均存在于 `read_note("test_cases")` 的完整列表中；横切面（HTTP/2、IPv6、HA、ACL、CRL、FTP）均通过 `check_spec_constraints` 或 `discover_cross_cutting_concerns` 工具确认为有效技术维度，非“通用测试经验”。


---
<adversarial_verification>
【对抗验证结果】确认 8, 驳回 2, 待定 0

F001 [确认] CLI 配置命令中 `group_name` 为空的语义不明确，预期结果不可验证
F002 [驳回] `global` 和 `default` 作为 `group_name` 的 Negative 测试存在逻辑矛盾
 反证: 用例 #20: Expected Result '配置成功'；用例 #43: Expected Result '删除失败(仅支持删除组的配置)'；用例 #44: Expected Result '删除失败'；用例 #54: Expected Result '显示失败'；用例 #55: Expected Result '显示失败'
F003 [确认] AES 密码明文泄露风险未被覆盖，违反安全需求 REQ_008
F004 [确认] HTTP/2 协议兼容性已覆盖，但 IPv6 地址族覆盖不完整
F005 [确认] HA 主备同步未验证密钥隔离性，违反 REQ_015
F006 [确认] `debug monitor import ftp` 与 ircookie 加密共存场景完全缺失
F007 [确认] CRL fetch（FTP/HTTP/LDAP）与 ircookie 加密的独立性未验证
F008 [确认] ACL 访问控制与加密 cookie 的集成未覆盖
F009 [确认] 多 group 并发配置的竞态条件（Race Condition）未验证
F010 [驳回] WebUI segment 模块的全局配置用例（#125）与经验库冲突
 反证: 用例 #125（模块 17/28）: Expected Result '配置失败，不支持配置全局ircookie'
</adversarial_verification>

