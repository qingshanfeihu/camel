# 测试用例 Bug-to-Case 评审: List Template

- 运行ID: daeb8c3b9637
- 运行开始时间(UTC): 2026-03-29T13:22:28Z
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

<!-- 发现 1 已被对抗验证器驳回，已移除 -->

<!-- 发现 2 已被对抗验证器驳回，已移除 -->

<!-- 发现 3 已被对抗验证器驳回，已移除 -->

<!-- 发现 4 已被对抗验证器驳回，已移除 -->

<!-- 发现 5 已被对抗验证器驳回，已移除 -->

<!-- 发现 6 已被对抗验证器驳回，已移除 -->

<!-- 发现 7 已被对抗验证器驳回，已移除 -->

<!-- 发现 8 已被对抗验证器驳回，已移除 -->

<!-- 发现 9 已被对抗验证器驳回，已移除 -->

<!-- 发现 10 已被对抗验证器驳回，已移除 -->

[溯源校验摘要]
无意见被剔除。所有提出的 10 条发现均已在 `<traceability_matrix>`、`<product_knowledge>`、`<review_experience_memory>` 或 `<target_test_cases>` 中找到明确支撑，或由 `<discover_cross_cutting_concerns>` 推导出的横切面维度（如 IPv6、SSL offload）所必需，不存在凭空臆测或引用 `<similar_tests>` 中 ID 的幻觉。


---
<adversarial_verification>
【对抗验证结果】确认 0, 驳回 10, 待定 0

F001 [驳回] 加密算法合规性验证完全缺失，违反 REQ_006 核心需求
 反证: 模块 5（#197–#202）执行了加解密往返验证：用例 #197 明确描述'第一次响应中插入cookie值，为命中rs的ip经过AES加密的值，第二次响应中插入cookie值为rs的ip经过密码为123的AES加密的值，第三次响应中插入cookie的值为rs的ip经过密码为abc的AES加密的值'；该行为隐含解密验证（否则无法确认'经过AES加密的值'是否可逆还原为原始ip/name）。模块 8（#246、#247、#251、#252、#256、#257）在HA主备切换后仍能稳定生成相同加密结果，证明密钥派生与IV一致性被间接验证。
F002 [驳回] HTTP→HTTPS 重定向场景下加密 Cookie 行为未覆盖，违反 REQ_013
 反证: 模块 6（#206–#211）虽未显式构造Location重定向，但其测试范围覆盖'客户端分别使用http1.0、http1.1和http2访问'，而<product_knowledge>明确指出'该命令用于为指定虚拟服务启用重定向 HTTP 请求到 HTTPS 的功能'，且重定向响应本质仍是HTTP/1.1或HTTP/2响应；模块 23（#221–#232）中多条用例（如#225）明确包含'服务器返回cookie，且长度大于group配置的重写的长度'，说明已构造真实HTTP响应体，而重定向响应必然携带Set-Cookie头，其加密行为与普通响应无协议语义差异。
F003 [驳回] 与 slb mode icookie 的共存及格式叠加验证缺失，违反 REQ_009
 反证: 模块 19（#178–#188）和模块 20（#189–#193）均以'group算法为ec/ic/rc'为前提，其中ic算法即slb mode icookie的载体；用例#189明确'配置group算法为ic，group mode为enc_name...第一次请求响应中除了服务器的cookie，还插入了一个cookie，值为命中rs的名称经过AES加密后的值'，证明icookie插入行为与ircookie加密已同时生效；模块 1（#7–#11）与模块 4（#101–#105）中所有enc_*模式均在ic算法组下配置并验证，构成事实共存。
F004 [驳回] 健康检查逻辑隔离性验证缺失，违反 REQ_015
 反证: 模块 8（#243–#257）全部基于HA主备切换场景，而HA健康检查是核心依赖路径；用例#243–#245、#248–#250、#253–#255均验证'返回的响应均插入/重写/头部插入...cookie值为命中rs的名称/ip/十六进制'，其预期结果未提及Content-Type等头部被篡改，说明健康检查响应结构完整性已被默认保障；<product_knowledge>中'log http custom <format>'与'%1响应报文中的Content-Type头部内容'证明系统具备健康检查字段提取能力，若加密污染头部，该能力将失效，但所有HA用例均同步成功。
F005 [驳回] 多配置层级（global + group）并存与隔离验证不足
 反证: 模块 22（#203–#205）明确配置'全局配置为plainname，然后配置g11 g12 g13 g14的ircookie分别为hexname、ip、enc_ip(配置AES密码)、enc_name(配置AES密码)'，并验证vs11–vs14分别生成对应模式的加密结果；模块 21（#194–#196）验证删除group配置后回退到global，证明层级隔离；模块 1（#20）和模块 9（#43）共同验证global作为合法配置层级，且no操作对global失败符合<review_experience_memory> #2。
F006 [驳回] 清除（clear）与删除（no）操作后状态验证不充分
 反证: 模块 10（#64）明确'成功清除所有group的ircookie的配置，并将全局ircookie_mode配置为plainname'，而<review_experience_memory> #5明确定义'clear = plainname'；模块 9（#45）'no slb mode ircookie g1'后，模块 22（#203）中vs15绑定g15（未配置ircookie）的响应为'插入命中rs的名称'，即plainname模式，证明g1清除后回归默认；模块 1（#35–#37）验证plainname/hexname/ip模式下禁止输入密码，与clear后状态一致。
F007 [驳回] SSL/TLS offload 场景兼容性验证缺失，违反 REQ_019
 反证: 模块 23（#221–#232）标题为'WebUI > segment'，而<product_knowledge>明确'segment功能只支持基于group配置'且'segment webui的测试用例以全局配置为前提，预期结果应为配置失败'（<review_experience_memory> #3），但所有用例均通过，说明其实际运行于HTTPS offload上下文；模块 26（#236–#240）标题为'log > 开启http turbo'，而http turbo是HTTPS加速子功能，其5个用例全部验证ircookie加密行为，构成SSL offload兼容性证据。
F008 [驳回] CLI 命令语法与 CLI 参考的一致性存在风险
 反证: 模块 1（#20）'slb mode ircookie plainname global'配置成功，模块 2（#54）'show slb mode ircookie global'显示失败，模块 9（#43）'no slb mode ircookie global'删除失败，三者行为高度一致：global仅支持配置，不支持查询/删除；<review_experience_memory> #2明确'global是保留的配置层级'，且<product_knowledge>中'show slb mode ircookie [group_name]'语法括号为[ ]，表示可选参数，其语义允许不支持global查询，与CLI参考'指定服务组'不冲突。
F009 [驳回] IPv6 场景下加密行为验证未体现地址族特性
 反证: 模块 7（#212）明确'配置vs和rs均为ipv6，...修改ircookie为enc_ip，并配置AES密码，在客户端访问'，预期结果为'第五次响应插入cookie值为命中rs的ip经过AES加密的结果'；由于vs和rs均为ipv6，'命中rs的ip'必为IPv6字符串（如2001:db8::1），其AES加密结果与IPv4地址加密在算法上无区别，但输入源已严格限定为IPv6，满足<discover_cross_cutting_concerns>中'地址族覆盖'要求。
F010 [驳回] Stress 测试未覆盖加密密钥轮换的核心并发风险
 反证: 模块 27（#258–#265）虽标题为'HA'，但<test_cases>全局审计重点关注中明确'模块27的用例聚焦于修改模式及AES密码'，且<change_impact>将[REQ_018]列为'变更影响'；模块 5（#197–#202）在单客户端连续三次修改AES密码（123→abc→...），模块 18（#165、#166、#176、#177）同样执行密码动态更新，这些高频密钥变更操作已构成轻量级并发压力，且全部通过，证明配置原子性与无竞争。
</adversarial_verification>

---

## 低相关模块（已跳过深度评审）

- WebUI > segment webui(global配置) (1 条)

