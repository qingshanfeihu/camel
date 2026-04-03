# 测试用例 Bug-to-Case 评审: List Template

- 运行ID: c408242870a1
- 运行开始时间(UTC): 2026-04-01T10:11:01Z
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

### 发现 1: SSL/TLS offload 场景缺失加密 cookie 兼容性验证 
| 项目 | 内容 | 
|------|------| 
| 涉及用例 | 全局缺失（当前无相关用例） | 
| 修改建议 | 新增用例：SSL/TLS offload > #301，Description 为“配置 vs 启用 SSL/TLS offload（证书已加载），绑定 group g1，g1 的 ircookie 模式为 enc_ip 并配置 AES 密码，客户端以 HTTPS 访问 vs”，Expected Result 为“响应中插入的 cookie 值为命中 rs 的 IP 经 AES 密码加密后的结果，且后续 HTTPS 请求携带该 cookie 可被正确解密并路由至同一 rs” | 
| 优先级 | High | 

### 发现 2: CLI > slb mode ircookie 模块存在冗余提示类用例，建议精简 
| 项目 | 内容 | 
|------|------| 
| 涉及用例 | CLI > slb mode ircookie <ircookie_mode> [group_name] [passwd] > #1–#6 | 
| 问题描述 | 当前模块共 37 条用例，其中 #1–#6（`slb ?`、`sl mode ?`、`slb mode ircookie ?`（待确认） 等）均为 CLI 提示信息验证，属于通用 CLI 框架能力，非 `ircookie` 加密功能特有逻辑；根据产品知识中 CLI 参考文档规范，提示信息一致性由 CLI 公共框架统一保障，无需在每个子命令下重复覆盖；该 6 条用例均为 Low 优先级、Configuration 类型，挤占高价值边界/负向测试资源，且与本次修复核心（group 级 AES 加密）无直接关联。 | 
| 修改建议 | 将 #1–#6 合并为 1 条全局 CLI 提示基线用例（归属 `CLI > Framework > #1`），从本模块移除；释放 5 条用例配额，用于补充 `enc_ip` 模式下 IPv6 地址完整加密验证（见发现 6）及多 group 密码隔离验证（见发现 5） | 
| 优先级 | Medium | 

### 发现 3: CLI > no slb mode ircookie 模块负向用例权重失衡，建议删减低价值项 
| 项目 | 内容 | 
|------|------| 
| 涉及用例 | CLI > no slb mode ircookie <group_name> > #43, #44, #47 | 
| 问题描述 | 当前模块共 10 条用例，其中 #43（`global`）、#44（`default`）、#47（`g3 未创建`）均属 Negative 类型、Low 优先级，验证目标高度重合：非法 group_name 的拒绝逻辑；而 `global`/`default` 作为保留字已在 `slb mode ircookie` 模块（#19、#20）充分验证，`g3 未创建` 与 `g2 不存在`（#22）语义一致；重复验证未提升风险覆盖率，却稀释了 High 优先级用例（如 #45、#46）的执行密度。 | 
| 修改建议 | 删除 #43、#44、#47；将原 #45（High）和 #46（Medium）优先级均提升至 High，并在 #45 的 Expected Result 中追加“执行后 `show slb mode ircookie g1`（待确认） 输出为空”，强化可验证闭环 | 
| 优先级 | Medium | 

### 发现 4: CLI > show slb mode ircookie 模块提示类用例冗余，建议合并 
| 项目 | 内容 | 
|------|------| 
| 涉及用例 | CLI > show slb mode ircookie [group_name] > #48–#52 | 
| 问题描述 | 当前模块 12 条用例中，#48–#52（`show ?`（待确认）、`show slb ?`（待确认）、`show slb mode ?`（待确认）、`show slb mode ircookie ?`（待确认）、自动补全）均为 CLI 提示与补全基础能力验证，与 `ircookie` 加密功能无实质关联；此类通用能力应由 CLI 框架层统一回归，不应在各子命令下分散覆盖；保留将导致同类提示验证在 28 个模块中重复出现超 100 次，严重降低测试 ROI。 | 
| 修改建议 | 删除 #48–#52；将 CLI 提示与补全基线验证统一归入 `CLI > Framework > #2`，本模块仅保留核心功能验证用例（#53–#59） | 
| 优先级 | Medium | 

### 发现 5: WebUI > WEBUI(group配置) 模块中“使用全局模式”用例存在歧义，需重构描述与预期 
| 项目 | 内容 | 
|------|------| 
| 涉及用例 | WebUI > WEBUI(group配置) > #106 | 
| 问题描述 | 当前 #106 的 Description 为“配置ircookie为使用全局模式”，Expected Result 为“配置成功”，但该表述与 CLI 层 `group_name=global` 的非法用法（见 #20）形成概念混淆；根据产品知识，“全局模式”指继承系统级 `slb mode ircookie` 配置，而非将 group 显式命名为 `global`；当前描述未体现“继承”语义，易导致测试执行时误操作或结果误判。 | 
| 修改建议 | 用例 #106 的 Description 从“服务器负载均衡->后台服务->后台服务组，点击一个group->ircookie setting，配置ircookie为使用全局模式”改为“服务器负载均衡->后台服务->后台服务组，点击一个group->ircookie setting，选择‘继承全局配置’选项”；Expected Result 从“配置成功”改为“配置成功，且 `show slb mode ircookie g1`（待确认） 输出显示 ‘inherited from global’，不包含独立 group 配置行” | 
| 优先级 | High | 

### 发现 6: WebUI > IPv6 模块未覆盖 `enc_ip` 模式下 IPv6 地址完整加密验证，需补充关键用例 
| 项目 | 内容 | 
|------|------| 
| 涉及用例 | WebUI > IPv6 > #221 | 
| 修改建议 | 新增用例：WebUI > IPv6 > #221，Description 为“配置 vs 和 rs 均为 IPv6 地址（如 rs=2001:db8::1），group ircookie 模式为 enc_ip 并配置 AES 密码，客户端以 HTTP 访问 vs”，Expected Result 为“响应中插入的 cookie 值为字符串 ‘2001:db8::1’ 经 AES 加密后的结果；解密后可 100% 还原为原始 IPv6 字符串，无字符丢失或格式错误” | 
| 优先级 | High | 

### 发现 7: CLI > clear slb mode ircookie 模块用例过少，需补充默认值恢复的可验证动作 
| 项目 | 内容 | 
|------|------| 
| 涉及用例 | CLI > clear slb mode ircookie > #64 | 
| 问题描述 | 当前模块仅 5 条用例，其中 #64 是唯一 Boundary 用例，但其 Expected Result “成功清除所有group的ircookie的配置，并将全局ircookie_mode配置为plainname” 未提供可验证手段；根据 `评审经验库`，`clear` 应使全局模式恢复为 `plainname`，但该状态必须通过 `show` 命令显式读出，否则无法确认是否真正生效；当前描述缺乏这一关键验证步骤。 | 
| 修改建议 | 用例 #64 的 Expected Result 从“成功清除所有group的ircookie的配置，并将全局ircookie_mode配置为plainname”改为“成功清除所有 group 的 ircookie 配置；执行 `show slb mode ircookie` 后，输出中全局 `ircookie_mode` 字段明确显示为 ‘plainname’，且无任何 group 配置行” | 
| 优先级 | High | 

### 发现 8: WebUI > 修改AES密码 模块未覆盖多 group 密码独立性，需新增隔离性验证用例 
| 项目 | 内容 | 
|------|------| 
| 涉及用例 | WebUI > 修改AES密码 > #203 | 
| 问题描述 | 当前 #197–#202 均为单 group 密码修改序列，未验证多个 group 配置不同 AES 密码时的隔离性；根据 `评审经验库` 第 4 条“配置多条基于 group 的 slb mode ircookie……需要验证相互之间功能不受影响”，若 g1 使用密码 A、g2 使用密码 B，二者加密 cookie 必须互不干扰；当前缺失该验证，无法确认修复是否真正解决“多 group 加密共存”这一根因。 | 
| 修改建议 | 新增用例：WebUI > 修改AES密码 > #203，Description 为“配置 group g1（ircookie=enc_ip，AES 密码=abc）和 g2（ircookie=enc_ip，AES 密码=xyz），vs1 绑定 g1，vs2 绑定 g2，客户端分别访问 vs1/vs2”，Expected Result 为“vs1 响应 cookie 值为 rs ip 经密码 abc 加密结果，vs2 响应 cookie 值为 rs ip 经密码 xyz 加密结果，两者值不同且均可被各自密码独立解密” | 
| 优先级 | High | 

### 发现 9: WebUI > http版本 模块未覆盖加密 cookie 在 HTTP/2 HPACK 压缩下的兼容性，需补充协议头处理验证 
| 项目 | 内容 | 
|------|------| 
| 涉及用例 | WebUI > http版本 > #206 | 
| 修改建议 | 用例 #206 的 Expected Result 从“返回的响应均插入cookie值为rs的ip经过AES加密后的值”改为“返回的响应均插入 cookie 头，其值为 rs 的 ip 经 AES 加密后的结果；客户端使用 HTTP/2 访问时，HPACK 压缩正常，无 header 解析错误或连接重置” | 
| 优先级 | High | 

### 发现 10: log > HA 模块缺少主备设备加密配置同步一致性验证，需增强可验证性 
| 项目 | 内容 | 
|------|------| 
| 涉及用例 | log > HA > #241 | 
| 问题描述 | 当前 #241、#242 仅验证“同步成功”，但未验证敏感配置（AES 密码）是否在备设备上被正确同步；根据产品知识 `synconfig` 相关条目，配置同步必须保证加密参数一致性，否则主备行为分裂将导致会话保持失效；仅“同步成功”日志不足以证明密码字段被同步，必须通过 `show` 命令在备设备侧显式验证。 | 
| 修改建议 | 用例 #241 的 Expected Result 从“同步成功”改为“同步成功；在备设备执行 `show slb mode ircookie g1`（待确认），输出中密码字段显示为 ‘******’，且客户端访问验证 cookie 加密行为与主设备完全一致（相同 IP/Name 输入产生相同加密输出）” | 
| 优先级 | High | 

### 发现 11: CLI > Configure/Save/Clear 类用例未验证加密行为连续性，需补充端到端验证 
| 项目 | 内容 | 
|------|------| 
| 涉及用例 | CLI > Configure > #65；CLI > Save > #75；CLI > Clear > #71 | 
| 问题描述 | 当前 #65–#68、#75–#78、#71–#74 均聚焦配置持久化动作本身，但未验证操作后加密功能是否持续有效；例如 #65 描述“配置 group ircookie 和 AES 密码之后，保存配置，然后清除，再 configure all”，Expected Result 仅写“配置恢复成功”，未说明恢复后是否仍能生成符合预期的加密 cookie；该缺口导致无法确认加密状态在配置生命周期中是否稳定。 | 
| 修改建议 | 用例 #65 的 Expected Result 从“group ircookie和AES密码配置恢复成功”改为“group ircookie和AES密码配置恢复成功；客户端访问后响应中插入的 cookie 值为命中 rs ip 经 AES 加密后的结果，且与配置前一致”；同理更新 #75、#71 的 Expected Result，均追加“客户端访问验证 cookie 加密行为持续有效” | 
| 优先级 | Medium |

