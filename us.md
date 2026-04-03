# US VPS（DMIT / Seeagood 同构）— 运维备忘

> 与 [jp.md](jp.md) 逻辑一致；域名与证书为 **qingshanfeihu.uk** 子域。

## 标识

| 项 | 值 |
|----|-----|
| 公网 IP | `154.26.183.67` |
| SSH | `ssh -i C:\SynologyDrive\VPS\DMIT\id_rsa.pem root@154.26.183.67` |
| 直连 SNI（灰云） | `us.qingshanfeihu.uk` |
| CDN 入口（橙云） | `pro.qingshanfeihu.uk` |
| 证书 | `/root/cert/qingshanfeihu.uk/fullchain.pem`、`privkey.pem`（SAN 含 `*.qingshanfeihu.uk`） |

## 端口与分流

- **443/tcp**：Nginx `stream` + `ssl_preread`：`us` → `127.0.0.1:1443`，`pro` → `127.0.0.1:8000`，`default` → `127.0.0.1:1`
- **443/udp**：Nginx **HTTP/3（QUIC）** 伪装（`server_name` 含 `us` / `pro`，证书 **`/root/cert/qingshanfeihu.uk/`**）；与 TCP `stream` **并存**；**不**替代 xhttp 的 h2 `grpc_pass`
- **127.0.0.1:18443**：`server_name us.qingshanfeihu.uk`，Reality **target** 伪装
- **127.0.0.1:8000**：`server_name pro.qingshanfeihu.uk`，`location /d4e8c2f1uspr` → `grpc_pass` → `127.0.0.1:1234`
- **127.0.0.1:1443 / 1234**：Xray（x-ui 入站 id=2 / id=3）
- **x-ui 面板**：仍为 x-ui 自带端口（如 `8443`），**不**经上述 stream `map`

## x-ui 入站摘要

- **id=2**：`127.0.0.1:1443`，VLESS + Reality + Vision + fallback → `1234`；`serverNames` 仅 `us.qingshanfeihu.uk`；`target` `127.0.0.1:18443`
- **id=3**：`127.0.0.1:1234`，xhttp，path **`/d4e8c2f1uspr`**
- **Vision UUID**：`a34ad5cc-9ccb-4ce3-9bfd-1dbd14fbde8c`
- **xhttp UUID**：`98b697dd-4f48-4fff-8fe2-8d3aa63116ae`
- **Reality PublicKey**：`jAV7Prindr-qJpN6pVzOiCY5Ra0Aeq1hBKUvlWq4138`（shortId 示例：`0a1b2c3d4e`，另有多个 shortId 在库中）

## 备份

- 改造前配置目录：`/root/backup_seeagood_us_/`

## Cloudflare（须自行核对控制台）

- `us.qingshanfeihu.uk`：**关闭**代理（灰云）
- `pro.qingshanfeihu.uk`：**开启**代理（橙云）
- SSL：**完全（严格）**
- 对 xhttp 路径 **`/d4e8c2f1uspr`**：**绕过缓存**
- 须放行 **UDP 443**（IPv4 / IPv6），否则 QUIC 探测失败

## HTTP/3 自检

- **VPS 上**（需 OpenSSL 支持 `-quic`）：  
  `echo | openssl s_client -connect 154.26.183.67:443 -quic -alpn h3 -servername us.qingshanfeihu.uk`
- **Windows 本机**：自带 `curl` 一般 **无** `--http3`。可用 **Git for Windows** 的 `openssl.exe`（通常带 `-quic`）：  
  `& 'C:\Program Files\Git\usr\bin\openssl.exe' s_client -connect 154.26.183.67:443 -quic -alpn h3 -servername us.qingshanfeihu.uk`  
  仓库根目录脚本：**`seeagood_h3_local_check.ps1`**（含 US + JP 各一段 IPv4；若需单独测 IPv6，可自行用 `nslookup -type=AAAA` 换地址）。
- **IPv6 QUIC**：若握手失败，除 DMIT 侧 UDP 外，还需本机/运营商 **UDP 443 v6** 可达。

## 本机 v2rayN

- **5 条** US 测试节点：`IndexId` `5600020000000000001` … `5600020000000000005`，备注 `[Seeagood-US-01]` … `[Seeagood-US-05]`（与 JP 的 `560001…` 由同一脚本写入）。
- **JP + US 一并写入**：`python seeagood_v2rayn_seed.py`（会先备份 `guiNDB.db.bak_seeagood_<时间戳>`）
- 自动化连通性（临时 xray + SOCKS **`127.0.0.1:10809`** + curl）：`python seeagood_connectivity_test.py us`；测全部：`python seeagood_connectivity_test.py`

**勿**将私钥、`privkey.pem` 提交 Git。
