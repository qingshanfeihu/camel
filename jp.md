# JP VPS（Seeagood 式 Nginx 前置 SNI 分流）— 运维备忘

本文汇总 **45.137.180.97**（主机名 `jp.qingshanfeihu.uk`）上已实施的改造、端口与域名、客户端要点、回滚与 Cloudflare 注意项。  
**私钥与 SSH 私钥文件内容请勿提交仓库**；下文仅列路径与已公开的 Reality 公钥等运行所需参数。

> 本文档独立于 `INAGENT` 主项目，仅供个人 VPS 备忘。

---

## 1. 登录与标识

| 项 | 值 |
|----|-----|
| 公网 IP | `45.137.180.97` |
| 主机名 | `jp.qingshanfeihu.uk`（仅作机器标识与 **x-ui 面板**域名，见下） |
| SSH 用户 | `root` |
| 本机私钥路径（示例） | `C:\Users\jiang\Desktop\rado_ssh_key.txt` |

---

## 2. 软件版本与环境（曾实测）

| 项 | 值 |
|----|-----|
| Nginx | `nginx-n.wtf/1.29.6`（含 `stream` 动态模块、`stream_ssl_preread`、`http_v2`、`http_v3`） |
| x-ui | `2.8.11` |
| Xray（随 x-ui） | `26.3.27` |
| 规格 | 约 1 vCPU、~925 MiB RAM、根盘约 10 GiB；`ulimit -n` 曾测为 1024；已为 nginx 配置 `LimitNOFILE=65535` 的 systemd drop-in |

---

## 3. 改造目标拓扑（Seeagood 概念）

- **公网 443**：仅 **Nginx `stream`** + `ssl_preread`，按 **SNI** 分流。
- **直连 Reality / Vision / xhttp+Reality**：公网 443 上 **仅** SNI `a_jp.qingshanfeihu.eu.org` → TCP 转发至 **`127.0.0.1:1443`**（Xray）。**已不再**将 `*.qingshanfeihu.uk` 纳入 stream 分流或 Reality 伪装站。
- **CDN xhttp+TLS**：SNI 为 **`b_jp.qingshanfeihu.eu.org`** → 转发至 **`127.0.0.1:8000`**（Nginx 终止 TLS，`grpc_pass` 至 Xray xhttp）。
- **Xray**：**不再监听 `0.0.0.0:443`**；主入站 **`127.0.0.1:1443`**（VLESS + Reality + Vision + **fallback → `127.0.0.1:1234`**）；xhttp 入站 **`127.0.0.1:1234`**。
- **Reality `target`**：`127.0.0.1:18443`（本机 Nginx `server_name a_jp...` + 对应证书，作伪装站点）。

参考讨论：[Xray-core #4118](https://github.com/XTLS/Xray-core/discussions/4118)。

---

## 4. 域名与 DNS（Cloudflare）

| 域名 | 用途 | CF 代理 |
|------|------|---------|
| `a_jp.qingshanfeihu.eu.org` | 直连 SNI（灰云） | 关闭 |
| `b_jp.qingshanfeihu.eu.org` | CDN 入口（橙云） | 开启 |
| `jp.qingshanfeihu.uk` | **仅** x-ui Web 面板（示例：`https://jp.qingshanfeihu.uk:2053/...`），由 **x-ui 自带 TLS** 提供，不经 nginx 443 stream | 按你原解析（通常直连面板端口） |

**已移除**：`jpb.qingshanfeihu.uk`、`jp.qingshanfeihu.uk` 作为 **443 / Reality target（18443）** 的用途；若客户端仍填上述 UK 域名为 SNI，需改为 **`a_jp.qingshanfeihu.eu.org`**。

证书 **SAN**：`*.qingshanfeihu.eu.org` 已覆盖 `a_jp` / `b_jp`。

**CF 必做**

- **SSL/TLS**：完全（严格）。
- **Cache Rules**：对 xhttp 路径（当前 **`/baedd5e2`**）**绕过缓存**，避免 CDN 缓存破坏 gRPC/xhttp。
- **UDP 443**（IPv4 / IPv6）：须放行，否则本机/边缘对 **QUIC（HTTP/3 伪装）** 的探测会失败。

---

## 5. 监听端口一览

| 端口 | 进程 | 说明 |
|------|------|------|
| 443/tcp | nginx | `stream` SNI 分流 |
| 443/udp | nginx | **HTTP/3（QUIC）** 伪装站点（`a_jp` / `b_jp` 证书）；与 TCP `stream` **并存**，**不**替代 xhttp 的 h2 `grpc_pass` |
| 80 | nginx | 301 → HTTPS |
| 127.0.0.1:8000 | nginx | `b_jp` TLS + `grpc_pass` → 1234 |
| 127.0.0.1:18443 | nginx | Reality **target** 伪装（`a_jp` 证书） |
| 127.0.0.1:1443 | xray | VLESS Reality Vision + fallback |
| 127.0.0.1:1234 | xray | VLESS xhttp（path 见下） |
| 2053 / 2096 | x-ui | 面板 HTTPS / 订阅 HTTPS |

---

## 6. Nginx 要点（与现网一致的结构）

1. **文件最顶端**须 `include /etc/nginx/modules-enabled/*.conf;`（加载 `ngx_stream_module.so`），否则无 `stream` 指令。
2. **`stream`**：`map $ssl_preread_server_name` 仅 **`a_jp` → 1443**、**`b_jp` → 8000**；未知 SNI → `127.0.0.1:1`（**无** `jpb`/`jp` `.uk` 映射）。
3. **`http`（公网 UDP 443）**：`listen 443 quic` + `[::]:443 quic`，`server_name a_jp… b_jp…`，`http3 on`，证书同 **`/root/cert/qingshanfeihu.eu.org/`**；**勿**在此 `server` 上加公网 `listen 443 ssl`（TCP），以免与 `stream` 冲突。
4. **`http`**：`127.0.0.1:18443` **仅一套** `server_name a_jp.qingshanfeihu.eu.org`（证书 `/root/cert/qingshanfeihu.eu.org/`），作 Reality **target** 伪装。
5. **`127.0.0.1:8000`**：`server_name b_jp.qingshanfeihu.eu.org`；`ssl_certificate` 使用 **`/root/cert/qingshanfeihu.eu.org/`**；`location /baedd5e2 { grpc_pass grpc://127.0.0.1:1234; ... }`。
6. 小内存机参数示例：`http2_max_concurrent_streams 256`、`grpc_buffer_size 16k` 等（见当时模板）。

**HTTP/3 自检**

- **VPS 上**（需 OpenSSL 支持 `-quic`）：  
  `echo | openssl s_client -connect 45.137.180.97:443 -quic -alpn h3 -servername a_jp.qingshanfeihu.eu.org`
- **Windows 本机**：自带 `curl` 一般无 `--http3`；可用 **Git for Windows** 的 `openssl.exe`（通常带 `-quic`）：  
  `& 'C:\Program Files\Git\usr\bin\openssl.exe' s_client -connect 45.137.180.97:443 -quic -alpn h3 -servername a_jp.qingshanfeihu.eu.org`  
  仓库根目录脚本：**`seeagood_h3_local_check.ps1`**（含 JP + US 各一段 IPv4 探测）。
- **IPv6 QUIC**：若失败，除 VPS 侧 UDP 外，还需本机/运营商 **UDP 443 v6** 可达；可用 `nslookup -type=AAAA a_jp.qingshanfeihu.eu.org` 核对 AAAA 后按需改命令中的地址。

请以现机 **`/etc/nginx/nginx.conf`** 为准。

---

## 7. x-ui / Xray 数据库要点

- **库路径**：`/etc/x-ui/x-ui.db`（入站存于 `inbounds` 表）。
- **入站 id=2（逻辑）**：`listen 127.0.0.1`，`port 1443`；`fallbacks.dest` = **`127.0.0.1:1234`**；`realitySettings.target` = **`127.0.0.1:18443`**；`serverNames` **仅** **`a_jp.qingshanfeihu.eu.org`**（已与 nginx 18443 单站一致）。
- **入站 id=3（逻辑）**：`127.0.0.1:1234`，xhttp，**path `/baedd5e2`**，`tag` 形如 `inbound-127.0.0.1:1234`。

Vision 用户与 xhttp 用户 **UUID 不同**（勿混用）。

| 用途 | UUID（示例，与部署时一致） |
|------|-----------------------------|
| Vision + Reality | `b7a34d65-db76-4260-9591-f8fc848e23f0` |
| xhttp | `baedd5e2-6dfa-4260-af05-a1ac9e77d269` |

Reality **PublicKey**（客户端填写）：`SUe7X9ULHXHBnFE_gbodfAW-EL8qHK6LzJjDlvlQOlM`  
**shortId** 示例：`029e9c351e`（服务端另有多个 shortId 时客户端任选一个列表内值）。

---

## 8. 证书与 x-ui「20. Cloudflare SSL Certificate」

- **不必 Docker**；本方案为 **systemd + x-ui + 本机 nginx**。
- **续期**：可继续在 x-ui 的 CF SSL 中维护；**Nginx** 的 `ssl_certificate` / `ssl_certificate_key` 须指向 **x-ui 写入的同一路径**（如 `/root/cert/qingshanfeihu.eu.org/`）。
- **续期后**：建议执行 `systemctl reload nginx` 以加载新证书。

---

## 9. #4118 行为摘要（客户端预期）

- **xhttp + Reality**：上下行均为 **h2**，不要假设 h3。
- **`xhttpSettings.extra`** 可省略；精简时 **host / path / mode** 即可；path **无需** `?ed=2560`。
- **xhttp+TLS+CDN** 与 **xhttp+Reality** 使用 **不同 SNI**（本方案 `b_jp` vs `a_jp`）。
- **上下行分离**不绕过被墙 IP；含 Reality 直连 VPS 的模式在 IP 被墙时可能失败，需 **全程 `b_jp` + TLS**。
- **Nginx**：`grpc_pass` 反代 xhttp，**不支持 http/1.1**；CDN 侧客户端 **alpn 常用 h2**。
- **HTTP/3 与 xhttp**：**xhttp 流量**仍走 **TCP 443** 上 **h2 + grpc_pass**；**UDP 443** 上另设 **QUIC 伪装**（见 §5），与 stream/tcp 并存，**不替代** xhttp 路径。

---

## 10. VPS 上备份与回滚

改造时备份目录示例：`/root/backup_seeagood_20260401_125508/`（含旧 `nginx.conf`、`config.json`；建议同时备份 `x-ui.db`）。

**回滚步骤（在 VPS 上）**

1. `systemctl stop x-ui`
2. `cp /root/backup_seeagood_*/nginx.conf /etc/nginx/nginx.conf`
3. 恢复 `x-ui.db` 或 `config.json`（与备份方式一致）
4. `nginx -t && systemctl reload nginx`
5. `systemctl start x-ui`

UK 清理前可在 `/root/backup_nginx_uk_cleanup.conf`、`/root/backup_xui_uk_cleanup.db` 取回旧配置。

---

## 11. 本机 v2rayN（Windows）

- **Xray 核心路径**：`C:\v2rayN-windows-64\bin\xray\xray.exe`（非 `bin\xray.exe`）。
- **配置库**：`C:\v2rayN-windows-64\guiConfigs\guiNDB.db`。
- **一键写入 JP+US 各 5 条**（会先备份 `guiNDB.db.bak_seeagood_<时间戳>`）：  
  `python seeagood_v2rayn_seed.py`  
  环境变量 `SEEAGOOD_V2RAYN_DB` 可覆盖库路径。
- **JP 列表名与 IndexId**：
  - `[Seeagood-01]` … `[Seeagood-05]`（含义同下）
  - `5600010000000000001` … `5600010000000000005`
- **US 节点**由同一脚本写入（`560002…`，备注 `[Seeagood-US-0x]`），详见 [us.md](us.md)。

**客户端统一**：对外端口均为 **`443`**（由 nginx stream 再转到 1443/8000）。

---

## 12. 本机连通性测试（自动化）

- `python seeagood_connectivity_test.py`：默认 **JP + US** 各测 5 条（临时起 xray + SOCKS）。
- **SOCKS**：JP **`127.0.0.1:10808`**，US **`127.0.0.1:10809`**（与 v2rayN 常驻端口错开，仅测试进程占用）。
- 仅测一端：`python seeagood_connectivity_test.py jp` 或 `… us`。
- 期望经代理访问 `https://www.gstatic.com/generate_204` 为 **HTTP 204**。环境变量 `SEEAGOOD_XRAY_EXE` 可覆盖 Xray 路径。**勿将含密钥的 JSON 提交 Git**。

---

## 13. 安全与仓库

- **勿**将 SSH 私钥、`privkey.pem`、x-ui 数据库明文提交 Git。
- UUID、公钥、域名、路径已在生产使用；若轮换，需同步 **x-ui、nginx、客户端与 v2rayN 数据库中的对应项**。

---

## 14. 变更记录（文档侧）

- **2026-04-01**：Seeagood 式改造落地；说明曾整理为 `jp_vps.md`。
- **2026-04-01（晚）**：从 nginx **stream map** 与 **127.0.0.1:18443** 中移除 `jpb.qingshanfeihu.uk`、`jp.qingshanfeihu.uk`；x-ui 入站 Reality **`serverNames`** 同步为仅 `a_jp.qingshanfeihu.eu.org`。`jp.qingshanfeihu.uk` 保留作 **x-ui 面板**（如 `:2053`）主机名，不经 nginx 443。
- **2026-04-01**：文档更名为根目录 **`jp.md`**。
- **2026-04-01**：仓库根目录 **合并** v2rayN 与自检脚本：`seeagood_v2rayn_seed.py`、`seeagood_connectivity_test.py`、`seeagood_h3_local_check.ps1`；删除旧的 `us_v2rayn_seed.py`、`us_five_connectivity_test.py`、`us_h3_local_check.ps1`。CF 与文档 **§9** 补充 **HTTP/3（UDP 443）与 xhttp（TCP/h2）并存** 的说明。
