# 测试用例 Bug-to-Case 评审: List Template

- 运行ID: a8286234df20
- 运行开始时间(UTC): 2026-04-01T02:19:08Z
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

**评审失败**: Error code: 500 - {'detail': "Error code: 400 - {'error': {'message': 'Access denied, please make sure your account is in good standing. For details, see: https://help.aliyun.com/zh/model-studio/error-code#overdue-payment', 'type': 'Arrearage', 'param': None, 'code': 'Arrearage'}, 'id': '224c8981-4e34-9884-a89c-b1c10c9d8485', 'request_id': '224c8981-4e34-9884-a89c-b1c10c9d8485'}"}

