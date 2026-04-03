"""从 guiNDB 读取 Seeagood 节点，用 xray + SOCKS + curl 测 generate_204。"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from typing import Any

DEFAULT_DB = os.environ.get(
    "SEEAGOOD_V2RAYN_DB", r"C:\v2rayN-windows-64\guiConfigs\guiNDB.db"
)
DEFAULT_XRAY = os.environ.get(
    "SEEAGOOD_XRAY_EXE", r"C:\v2rayN-windows-64\bin\xray\xray.exe"
)
TEST_URL = "https://www.gstatic.com/generate_204"

REGIONS: dict[str, tuple[int, int, tuple[str, int], str]] = {
    "jp": (
        5600010000000000001,
        5600010000000000005,
        ("127.0.0.1", 10808),
        "JP",
    ),
    "us": (
        5600020000000000001,
        5600020000000000005,
        ("127.0.0.1", 10809),
        "US",
    ),
}


def _parse_extra(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _alpn_list(s: str | None) -> list[str] | None:
    if not s or not str(s).strip():
        return None
    parts = [x.strip() for x in str(s).replace(",", " ").split() if x.strip()]
    return parts or None


def build_stream_settings(row: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    net = (row.get("Network") or "tcp").lower()
    sec = (row.get("StreamSecurity") or "none").lower()
    ss: dict[str, Any] = {"network": net, "security": sec}

    if sec == "reality":
        ss["realitySettings"] = {
            "show": False,
            "serverName": row.get("Sni") or "",
            "fingerprint": row.get("Fingerprint") or "chrome",
            "publicKey": row.get("PublicKey") or "",
            "shortId": row.get("ShortId") or "",
            "spiderX": row.get("SpiderX") or "/",
        }
    elif sec == "tls":
        tls: dict[str, Any] = {
            "serverName": row.get("Sni") or "",
            "fingerprint": row.get("Fingerprint") or "chrome",
        }
        alpn = _alpn_list(row.get("Alpn"))
        if alpn:
            tls["alpn"] = alpn
        ss["tlsSettings"] = tls

    if net == "tcp":
        ss["tcpSettings"] = {"header": {"type": "none"}}
    elif net == "xhttp":
        xh: dict[str, Any] = {
            "path": row.get("Path") or "/",
            "mode": (row.get("HeaderType") or "auto"),
        }
        rh = row.get("RequestHost")
        if rh:
            xh["host"] = rh
        elif row.get("Sni"):
            xh["host"] = row["Sni"]
        ds = extra.get("downloadSettings")
        if ds:
            xh["downloadSettings"] = ds
        ss["xhttpSettings"] = xh
    else:
        raise ValueError(f"unsupported network: {net}")

    return ss


def row_to_xray_config(
    row: dict[str, Any], socks: tuple[str, int]
) -> dict[str, Any]:
    extra = _parse_extra(row.get("Extra"))
    user: dict[str, Any] = {"id": row["Id"], "encryption": "none"}
    if row.get("Flow"):
        user["flow"] = row["Flow"]

    outbound: dict[str, Any] = {
        "tag": "proxy",
        "protocol": "vless",
        "settings": {
            "vnext": [
                {
                    "address": row["Address"],
                    "port": int(row["Port"]),
                    "users": [user],
                }
            ]
        },
        "streamSettings": build_stream_settings(row, extra),
    }

    return {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {
                "listen": socks[0],
                "port": socks[1],
                "protocol": "socks",
                "settings": {"udp": True},
            }
        ],
        "outbounds": [outbound],
    }


def load_rows(db_path: str, lo: int, hi: int) -> list[dict[str, Any]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM ProfileItem WHERE IndexId >= ? AND IndexId <= ? ORDER BY IndexId",
        (str(lo), str(hi)),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _curl_once(socks: tuple[str, int]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "curl",
            "-sS",
            "-o",
            "NUL",
            "-w",
            "%{http_code}",
            "--connect-timeout",
            "35",
            "--max-time",
            "70",
            "-x",
            f"socks5://{socks[0]}:{socks[1]}",
            TEST_URL,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )


def run_one(xray_exe: str, cfg: dict[str, Any], socks: tuple[str, int]) -> tuple[bool, str]:
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        path = f.name
    last_err = ""
    try:
        for attempt in range(1, 4):
            r = subprocess.run(
                [xray_exe, "run", "-test", "-c", path],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if r.returncode != 0:
                return False, f"config test failed: {r.stderr or r.stdout}"

            proc = subprocess.Popen(
                [xray_exe, "run", "-c", path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
                if sys.platform == "win32"
                else 0,
            )
            time.sleep(4.0 if attempt == 1 else 5.0)
            try:
                cr = _curl_once(socks)
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()

            code = (cr.stdout or "").strip()
            if cr.returncode == 0 and code == "204":
                if attempt > 1:
                    return True, f"HTTP 204 OK (attempt {attempt})"
                return True, "HTTP 204 OK"
            last_err = f"curl exit {cr.returncode} http={code!r} {(cr.stderr or '').strip()}"
            time.sleep(2.0)

        return False, last_err
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def run_region(
    db: str,
    xray: str,
    key: str,
) -> bool:
    lo, hi, socks, label = REGIONS[key]
    rows = load_rows(db, lo, hi)
    if len(rows) != 5:
        print(
            f"Expected 5 {label} profiles, got {len(rows)}. Run seeagood_v2rayn_seed.py first.",
            file=sys.stderr,
        )
        return False

    print(f"--- {label} SOCKS={socks[0]}:{socks[1]} ---\n")
    ok_all = True
    for row in rows:
        label_row = row.get("Remarks") or row["IndexId"]
        try:
            cfg = row_to_xray_config(row, socks)
        except Exception as e:
            print(f"[FAIL] {label_row}\n  build: {e}")
            ok_all = False
            continue
        ok, msg = run_one(xray, cfg, socks)
        tag = "OK" if ok else "FAIL"
        print(f"[{tag}] {label_row}\n  {msg}")
        if not ok:
            ok_all = False
    return ok_all


def main() -> int:
    p = argparse.ArgumentParser(description="Seeagood JP/US connectivity via temp xray + SOCKS.")
    p.add_argument(
        "regions",
        nargs="*",
        choices=["jp", "us", "all"],
        help="jp | us | all（省略则测全部）",
    )
    args = p.parse_args()
    regions = list(args.regions) if args.regions else ["all"]
    if "all" in regions:
        keys = ["jp", "us"]
    else:
        keys = [k for k in ("jp", "us") if k in regions]

    db = DEFAULT_DB
    xray = DEFAULT_XRAY
    if not os.path.isfile(db):
        print(f"Missing DB: {db}", file=sys.stderr)
        return 2
    if not os.path.isfile(xray):
        print(f"Missing xray: {xray}", file=sys.stderr)
        return 2

    print(f"DB={db}\nXray={xray}\n")
    ok = True
    for k in keys:
        if not run_region(db, xray, k):
            ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
