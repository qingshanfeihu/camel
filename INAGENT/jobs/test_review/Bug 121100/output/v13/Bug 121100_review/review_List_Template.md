# 测试用例 Bug-to-Case 评审: List Template

- 运行ID: 4ae076dc65b8
- 运行开始时间(UTC): 2026-04-01T08:57:52Z
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

### 发现 1: 缺失“透明模式启用前提”验证用例 
| 项目 | 内容 |
|------|------|
| 涉及用例 | CLI > slb mode ircookie <ircookie_mode> [group_name] [passwd] > 用例 #7–#37 |
| 问题描述 | 根据产品知识文档中明确的规格约束“该命令仅在透明模式下生效”，所有当前37条CLI配置用例均未设置前置条件验证设备是否处于透明模式，也未测试非透明模式下执行命令的拒绝行为（如返回 `Error: not in transparent mode`）。这导致无法确认修复后行为是否符合设计要求。 |
| 修改建议 | 新增用例：CLI > slb mode ircookie <ircookie_mode> [group_name] [passwd] > 用例 #38；Description 字段改为“验证 ircookie 命令仅在透明模式下生效：先执行 `no slb transparent` 切换为非透明模式，再执行 `slb mode ircookie plainname g1`；然后执行 `slb transparent` 切回透明模式，再次执行相同命令”；Expected Result 改为“非透明模式下执行失败，提示 'Error: not in transparent mode'；透明模式下执行成功” |
| 优先级 | High |

### 发现 2: `no slb mode ircookie <group_name>` 对 `global` 的预期结果与修复说明冲突 
| 项目 | 内容 |
|------|------|
| 涉及用例 | CLI > no slb mode ircookie <group_name> > 用例 #43 |
| 问题描述 | 当前用例 #43 预期 `no slb mode ircookie global` 删除失败，理由为“仅支持删除组的配置”，但根据 Bug描述 中明确说明的“`[group_name] option, default is global`”，`global` 是该命令默认作用域，`no` 命令作为 `slb mode ircookie` 的逆操作，应支持清除全局配置。当前预期与修复逻辑不一致。 |
| 修改建议 | 用例 #43 的 Expected Result 从“删除失败(仅支持删除组的配置)”改为“删除成功，清除全局 ircookie 配置，并恢复为 plainname” |
| 优先级 | High |

### 发现 3: `clear slb mode ircookie` 的预期结果缺乏可验证性 
| 项目 | 内容 |
|------|------|
| 涉及用例 | CLI > clear slb mode ircookie > 用例 #64 |
| 问题描述 | 当前 Expected Result “成功清除所有group的ircookie的配置，并将全局ircookie_mode配置为plainname”不可直接观测验证——未要求执行 `show slb mode ircookie` 或 `show run` 等命令确认状态变更，导致该用例无法被自动化或人工复现验证。 |
| 修改建议 | 用例 #64 的 Expected Result 从“成功清除所有group的ircookie的配置，并将全局ircookie_mode配置为plainname”改为“成功执行 clear 命令；执行 `show slb mode ircookie` 后显示全局 ircookie_mode 为 plainname，且无 password 字段；对任意已存在 group 执行 `show slb mode ircookie <group_name>` 仍显示其原有配置（即 group 配置未被清除）” |
| 优先级 | Medium |

### 发现 4: `group_name` 命名规范负面测试缺失关键字符组合 
| 项目 | 内容 |
|------|------|
| 涉及用例 | CLI > slb mode ircookie <ircookie_mode> [group_name] [passwd] > 用例 #19, #20, #22 |
| 问题描述 | 根据产品知识文档中 `ircookie 配置中 group_name 应符合 CLI 命名规范（如长度、字符集）`，当前仅覆盖 `default`、`global`、`g2`（不存在）三类场景，未覆盖前导/尾随空格、制表符、空字符（`\x00`）、换行符（`\n`）、超长名称（>64 字符）等 CLI 命名规范典型拒绝场景。 |
| 修改建议 | 新增用例：CLI > slb mode ircookie <ircookie_mode> [group_name] [passwd] > 用例 #39；Description 字段改为“验证 group_name 命名规范：分别配置 group_name 为 'g1 '（尾随空格）、' g1'（前导空格）、'g1\t'（含制表符）、'g1\x00'（含空字符）、长度为65的字符串（如 g12345678901234567890123456789012345678901234567890123456789012345）”；Expected Result 改为“全部配置失败，提示 'Invalid group name' 或类似 CLI 解析错误” |
| 优先级 | High |

### 发现 5: HTTP 版本兼容性测试未覆盖 CLI 层面 
| 项目 | 内容 |
|------|------|
| 问题描述 | 根据产品知识文档“设备对SLB功能提供了广泛的IPv6支持……从协议角度看，HTTP/1.0、HTTP/1.1、HTTP/2 均支持 ircookie 加密行为”，当前所有 CLI 用例均未关联 HTTP 协议版本上下文，也未验证不同 HTTP 版本下 ircookie 配置是否影响命令解析或运行时行为（如 header 解析差异导致 cookie 插入异常）。WebUI 模块虽有 HTTP 版本测试（用例 #206–#211），但 CLI 层面完全缺失。 |
| 修改建议 | 新增用例：CLI > slb mode ircookie <ircookie_mode> [group_name] [passwd] > 用例 #40；Description 字段改为“验证 HTTP 版本兼容性：在虚拟服务启用 HTTP/1.0、HTTP/1.1、HTTP/2 三种协议版本下，分别执行 `slb mode ircookie enc_ip g1 abc` 并验证配置成功”；Expected Result 改为“三种 HTTP 版本配置下均执行成功，且后续 `show slb mode ircookie g1` 显示配置一致” |
| 优先级 | High |

### 发现 6: IPv6 地址族支持未在 CLI 配置中验证 
| 项目 | 内容 |
|------|------|
| 修改建议 | 新增用例：CLI > slb mode ircookie <ircookie_mode> [group_name] [passwd] > 用例 #41；Description 字段改为“验证 IPv6 场景下 ircookie 配置有效性：创建 IPv6 VIP 的虚拟服务，配置 `slb mode ircookie ip g1`，并验证 `show slb mode ircookie g1` 输出中 cookie 值字段为合法 IPv6 地址格式（如 `2001:db8::1`）”；Expected Result 改为“配置成功；`show` 命令输出中 cookie_value 字段为 IPv6 地址的十六进制表示，不含 IPv4 兼容前缀或截断” |
| 优先级 | High |

### 发现 7: SSL/TLS offload 与 ircookie 的交互未验证 
| 项目 | 内容 |
|------|------|
| 修改建议 | 新增用例：WebUI > WEBUI(group配置) > 用例 #125；Description 字段改为“验证 SSL/TLS offload 下 ircookie 行为：创建启用 SSL 卸载的虚拟服务，后台服务组配置 `ircookie=enc_ip` 并设置 AES 密码，客户端使用 HTTPS 访问”；Expected Result 改为“响应中插入的 cookie 值为命中 RS 的 IPv4/IPv6 地址经 AES 加密后的 Base64 编码值，且无明文泄露” |
| 优先级 | High |

### 发现 8: 配置持久化未验证重启后 ircookie 状态保持 
| 项目 | 内容 |
|------|------|
| 修改建议 | 新增用例：CLI > slb mode ircookie <ircookie_mode> [group_name] [passwd] > 用例 #43；Description 字段改为“验证配置持久化：执行 `slb mode ircookie enc_name g1 abc` → `save config` → 重启设备 → 执行 `show slb mode ircookie g1`”；Expected Result 改为“重启后 `show` 命令返回 `ircookie_mode=enc_name` 且 `password` 字段存在（加密存储）” |
| 优先级 | High |

### 发现 9: 用例冗余建议：合并重复的密码边界测试 
| 项目 | 内容 |
|------|------|
| 涉及用例 | CLI > slb mode ircookie <ircookie_mode> [group_name] [passwd] > 用例 #23/#24/#26/#27/#28（enc_ip）与 #29/#30/#32/#33/#34（enc_name） |
| 问题描述 | 当前用例 #23–#28 和 #29–#34 分别对 `enc_ip` 和 `enc_name` 模式进行完全相同的密码边界测试（空密码、单字符、特殊字符、128/129 字节），属于重复覆盖。根据测试规范，同一类边界行为无需按 mode 拆分多套，应合并为通用密码验证用例，降低维护成本并提升执行效率。 |
| 修改建议 | 将用例 #23/#24/#26/#27/#28 和 #29/#30/#32/#33/#34 合并为一条新用例：CLI > slb mode ircookie <ircookie_mode> [group_name] [passwd] > 用例 #44；Description 字段改为“验证 enc_* 模式下密码通用边界：分别以 `enc_ip` 和 `enc_name` 模式，配置空密码、单字符、特殊字符、128 字节、129 字节密码”；Expected Result 改为“两种模式下，空密码/单字符/特殊字符/128 字节均配置成功，129 字节均配置失败”；原 #23–#34 共 12 条用例标记为 Deprecated |
| 优先级 | Medium |

### 发现 10: 回归范围可收窄：移除无关模块的重复验证 
| 项目 | 内容 |
|------|------|
| 涉及用例 | WebUI > http版本 > 用例 #206–#211；WebUI > IPv6 > 用例 #212–#217；log > HA > 用例 #218–#223；Stress Testing > HA > 用例 #224–#229 |
| 问题描述 | 评审报告中将 `WebUI > http版本`、`WebUI > IPv6`、`log > HA`、`Stress Testing > HA` 列为回归验证模块，但这些模块本身不涉及 ircookie 加密逻辑（如 `http版本` 仅验证日志格式、`log > HA` 验证日志同步而非配置同步），且其用例未与 ircookie 配置建立任何依赖或交互路径。根据“Bug 核心为 IR-Cookie 加密支持”的全局判断，此类模块回归验证属过度覆盖。 |
| 修改建议 | 移除 `WebUI > http版本`、`WebUI > IPv6`、`log > HA`、`Stress Testing > HA` 四个模块的全部回归用例（共 24 条）；回归验证范围聚焦于 `CLI > slb mode ircookie`、`CLI > show slb mode ircookie`、`CLI > no slb mode ircookie`、`CLI > clear slb mode ircookie`、`WebUI > WEBUI(group配置)`、`WebUI > 全局与group的优先级`、`WebUI > 修改AES密码` 七个核心模块 |
| 优先级 | Medium |

---

## 低相关模块（已跳过深度评审）

- CLI > Configure (4 条)
- CLI > Save (4 条)
- WebUI > segment webui(global配置) (1 条)

