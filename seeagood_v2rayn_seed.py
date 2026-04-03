"""
将 JP + US 各 5 条 Seeagood 节点写入 v2rayN guiNDB.db。
IndexId：5600010000000000001–5（JP）、5600020000000000001–5（US）。
运行前建议关闭 v2rayN。备份：guiNDB.db.bak_seeagood_<时间戳>。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import time

DEFAULT_DB = os.environ.get(
    "SEEAGOOD_V2RAYN_DB", r"C:\v2rayN-windows-64\guiConfigs\guiNDB.db"
)

PROFILE_COLUMNS = [
    "IndexId",
    "ConfigType",
    "ConfigVersion",
    "Address",
    "Port",
    "Ports",
    "Id",
    "AlterId",
    "Security",
    "Network",
    "Remarks",
    "HeaderType",
    "RequestHost",
    "Path",
    "StreamSecurity",
    "AllowInsecure",
    "Subid",
    "IsSub",
    "Flow",
    "Sni",
    "Alpn",
    "CoreType",
    "PreSocksPort",
    "Fingerprint",
    "DisplayLog",
    "PublicKey",
    "ShortId",
    "SpiderX",
    "Mldsa65Verify",
    "Extra",
    "MuxEnabled",
    "Cert",
    "CertSha",
    "EchConfigList",
    "EchForceQuery",
]

# --- JP（jp.md）---
JP_IP = "45.137.180.97"
JP_DIRECT = "a_jp.qingshanfeihu.eu.org"
JP_CDN = "b_jp.qingshanfeihu.eu.org"
JP_VISION_UUID = "b7a34d65-db76-4260-9591-f8fc848e23f0"
JP_XHTTP_UUID = "baedd5e2-6dfa-4260-af05-a1ac9e77d269"
JP_PUBLIC_KEY = "SUe7X9ULHXHBnFE_gbodfAW-EL8qHK6LzJjDlvlQOlM"
JP_SHORT_ID = "029e9c351e"
JP_PATH = "/baedd5e2"
JP_INDEX = [f"560001000000000000{i}" for i in range(1, 6)]

JP_EXTRA_03 = json.dumps(
    {
        "downloadSettings": {
            "address": JP_IP,
            "port": 443,
            "network": "xhttp",
            "security": "reality",
            "realitySettings": {
                "show": False,
                "serverName": JP_DIRECT,
                "fingerprint": "chrome",
                "publicKey": JP_PUBLIC_KEY,
                "shortId": JP_SHORT_ID,
                "spiderX": "/",
            },
            "xhttpSettings": {"host": "", "path": JP_PATH, "mode": "auto"},
        }
    },
    separators=(",", ":"),
)

JP_EXTRA_05 = json.dumps(
    {
        "downloadSettings": {
            "address": JP_CDN,
            "port": 443,
            "network": "xhttp",
            "security": "tls",
            "tlsSettings": {
                "serverName": JP_CDN,
                "allowInsecure": False,
                "alpn": ["h2"],
                "fingerprint": "chrome",
            },
            "xhttpSettings": {
                "host": JP_CDN,
                "path": JP_PATH,
                "mode": "auto",
            },
        }
    },
    separators=(",", ":"),
)

# --- US（us.md）---
US_IP = "154.26.183.67"
US_DIRECT = "us.qingshanfeihu.uk"
US_CDN = "pro.qingshanfeihu.uk"
US_VISION_UUID = "a34ad5cc-9ccb-4ce3-9bfd-1dbd14fbde8c"
US_XHTTP_UUID = "98b697dd-4f48-4fff-8fe2-8d3aa63116ae"
US_PUBLIC_KEY = "jAV7Prindr-qJpN6pVzOiCY5Ra0Aeq1hBKUvlWq4138"
US_SHORT_ID = "0a1b2c3d4e"
US_PATH = "/d4e8c2f1uspr"
US_INDEX = [f"560002000000000000{i}" for i in range(1, 6)]

US_EXTRA_03 = json.dumps(
    {
        "downloadSettings": {
            "address": US_IP,
            "port": 443,
            "network": "xhttp",
            "security": "reality",
            "realitySettings": {
                "show": False,
                "serverName": US_DIRECT,
                "fingerprint": "chrome",
                "publicKey": US_PUBLIC_KEY,
                "shortId": US_SHORT_ID,
                "spiderX": "/",
            },
            "xhttpSettings": {"host": "", "path": US_PATH, "mode": "auto"},
        }
    },
    separators=(",", ":"),
)

US_EXTRA_05 = json.dumps(
    {
        "downloadSettings": {
            "address": US_CDN,
            "port": 443,
            "network": "xhttp",
            "security": "tls",
            "tlsSettings": {
                "serverName": US_CDN,
                "allowInsecure": False,
                "alpn": ["h2"],
                "fingerprint": "chrome",
            },
            "xhttpSettings": {
                "host": US_CDN,
                "path": US_PATH,
                "mode": "auto",
            },
        }
    },
    separators=(",", ":"),
)


def _base(
    index_id: str,
    address: str,
    port: int,
    uid: str,
    network: str,
    remarks: str,
    header_type: str,
    request_host: str | None,
    path: str,
    stream_security: str,
    flow: str | None,
    sni: str,
    alpn: str,
    public_key: str | None,
    short_id: str | None,
    spider_x: str | None,
    extra: str | None,
) -> dict:
    return {
        "IndexId": index_id,
        "ConfigType": 5,
        "ConfigVersion": 2,
        "Address": address,
        "Port": port,
        "Ports": None,
        "Id": uid,
        "AlterId": 0,
        "Security": "none",
        "Network": network,
        "Remarks": remarks,
        "HeaderType": header_type,
        "RequestHost": request_host,
        "Path": path,
        "StreamSecurity": stream_security,
        "AllowInsecure": "false",
        "Subid": None,
        "IsSub": None,
        "Flow": flow,
        "Sni": sni,
        "Alpn": alpn,
        "CoreType": None,
        "PreSocksPort": None,
        "Fingerprint": "chrome",
        "DisplayLog": 1,
        "PublicKey": public_key,
        "ShortId": short_id,
        "SpiderX": spider_x,
        "Mldsa65Verify": None,
        "Extra": extra,
        "MuxEnabled": None,
        "Cert": None,
        "CertSha": None,
        "EchConfigList": None,
        "EchForceQuery": None,
    }


def jp_profiles() -> list[dict]:
    ids = JP_INDEX
    return [
        _base(
            ids[0],
            JP_IP,
            443,
            JP_VISION_UUID,
            "tcp",
            "[Seeagood-01] Vision+Reality (a_jp)",
            "none",
            None,
            "",
            "reality",
            "xtls-rprx-vision",
            JP_DIRECT,
            "",
            JP_PUBLIC_KEY,
            JP_SHORT_ID,
            "/",
            None,
        ),
        _base(
            ids[1],
            JP_IP,
            443,
            JP_XHTTP_UUID,
            "xhttp",
            "[Seeagood-02] XHTTP+Reality (a_jp)",
            "auto",
            None,
            JP_PATH,
            "reality",
            None,
            JP_DIRECT,
            "h2",
            JP_PUBLIC_KEY,
            JP_SHORT_ID,
            "/",
            None,
        ),
        _base(
            ids[2],
            JP_CDN,
            443,
            JP_XHTTP_UUID,
            "xhttp",
            "[Seeagood-03] CDN-up-TLS Reality-down",
            "auto",
            JP_CDN,
            JP_PATH,
            "tls",
            None,
            JP_CDN,
            "h2",
            None,
            None,
            None,
            JP_EXTRA_03,
        ),
        _base(
            ids[3],
            JP_CDN,
            443,
            JP_XHTTP_UUID,
            "xhttp",
            "[Seeagood-04] XHTTP+TLS+CDN (b_jp)",
            "auto",
            JP_CDN,
            JP_PATH,
            "tls",
            None,
            JP_CDN,
            "h2",
            None,
            None,
            None,
            None,
        ),
        _base(
            ids[4],
            JP_IP,
            443,
            JP_XHTTP_UUID,
            "xhttp",
            "[Seeagood-05] Reality-up CDN-down",
            "auto",
            None,
            JP_PATH,
            "reality",
            None,
            JP_DIRECT,
            "h2",
            JP_PUBLIC_KEY,
            JP_SHORT_ID,
            "/",
            JP_EXTRA_05,
        ),
    ]


def us_profiles() -> list[dict]:
    ids = US_INDEX
    return [
        _base(
            ids[0],
            US_IP,
            443,
            US_VISION_UUID,
            "tcp",
            "[Seeagood-US-01] Vision+Reality (us)",
            "none",
            None,
            "",
            "reality",
            "xtls-rprx-vision",
            US_DIRECT,
            "",
            US_PUBLIC_KEY,
            US_SHORT_ID,
            "/",
            None,
        ),
        _base(
            ids[1],
            US_IP,
            443,
            US_XHTTP_UUID,
            "xhttp",
            "[Seeagood-US-02] XHTTP+Reality (us)",
            "auto",
            None,
            US_PATH,
            "reality",
            None,
            US_DIRECT,
            "h2",
            US_PUBLIC_KEY,
            US_SHORT_ID,
            "/",
            None,
        ),
        _base(
            ids[2],
            US_CDN,
            443,
            US_XHTTP_UUID,
            "xhttp",
            "[Seeagood-US-03] CDN-up-TLS Reality-down",
            "auto",
            US_CDN,
            US_PATH,
            "tls",
            None,
            US_CDN,
            "h2",
            None,
            None,
            None,
            US_EXTRA_03,
        ),
        _base(
            ids[3],
            US_CDN,
            443,
            US_XHTTP_UUID,
            "xhttp",
            "[Seeagood-US-04] XHTTP+TLS+CDN (pro)",
            "auto",
            US_CDN,
            US_PATH,
            "tls",
            None,
            US_CDN,
            "h2",
            None,
            None,
            None,
            None,
        ),
        _base(
            ids[4],
            US_IP,
            443,
            US_XHTTP_UUID,
            "xhttp",
            "[Seeagood-US-05] Reality-up CDN-down",
            "auto",
            None,
            US_PATH,
            "reality",
            None,
            US_DIRECT,
            "h2",
            US_PUBLIC_KEY,
            US_SHORT_ID,
            "/",
            US_EXTRA_05,
        ),
    ]


def main() -> int:
    db_path = DEFAULT_DB
    if not os.path.isfile(db_path):
        print(f"Missing: {db_path}", file=sys.stderr)
        return 2
    ts = time.strftime("%Y%m%d_%H%M%S")
    bak = f"{db_path}.bak_seeagood_{ts}"
    shutil.copy2(db_path, bak)
    print("Backup:", bak)

    all_profiles = jp_profiles() + us_profiles()
    all_ids = JP_INDEX + US_INDEX

    conn = sqlite3.connect(db_path)
    ph = ",".join("?" * len(all_ids))
    conn.execute(f"DELETE FROM ProfileItem WHERE IndexId IN ({ph})", all_ids)
    conn.execute(f"DELETE FROM ProfileExItem WHERE IndexId IN ({ph})", all_ids)
    ins_pi = (
        f"INSERT INTO ProfileItem ({','.join(PROFILE_COLUMNS)}) "
        f"VALUES ({','.join('?' * len(PROFILE_COLUMNS))})"
    )
    ins_pe = (
        "INSERT INTO ProfileExItem (IndexId, Delay, Speed, Sort, Message) VALUES (?,?,?,?,?)"
    )
    for i, p in enumerate(all_profiles):
        conn.execute(ins_pi, tuple(p[c] for c in PROFILE_COLUMNS))
        sort_base = 110 if p["IndexId"].startswith("560001") else 210
        conn.execute(
            ins_pe,
            (p["IndexId"], -1, 0.0, sort_base + (i % 5), ""),
        )
    conn.commit()
    conn.close()
    print(f"Inserted JP {JP_INDEX[0]} … {JP_INDEX[-1]} + US {US_INDEX[0]} … {US_INDEX[-1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
