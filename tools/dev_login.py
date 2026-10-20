"""打印本地 dev 管理员的登录信息与实时动态码（仅 dev；凭据来自 tools/dev.py bootstrap）。

用法：python tools/dev_login.py        持续刷新动态码，Ctrl-C 退出
      python tools/dev_login.py --once 只打印一次
"""

from __future__ import annotations

import pathlib
import re
import sys
import time

import pyotp

CRED = pathlib.Path(__file__).resolve().parents[1] / ".aip-data" / "dev-admin.txt"


def main() -> None:
    if not CRED.exists():
        sys.exit(f"未找到 {CRED}，请先运行 python tools/dev.py bootstrap")
    text = CRED.read_text(encoding="utf-8")
    login = re.search(r"^login_name=(.*)$", text, re.M)
    password = re.search(r"^password=(.*)$", text, re.M)
    secret = re.search(r"secret=([A-Z2-7]+)", text)
    if not (login and password and secret):
        sys.exit(f"{CRED} 格式不对")
    totp = pyotp.TOTP(secret.group(1))
    print("控制台：http://127.0.0.1:18081/console/  或  http://localhost:5173/console/（npm run dev）")
    print(f"用户名：{login.group(1).strip()}")
    print(f"口令：  {password.group(1).strip()}")
    print("动态码每 30 秒变化一次，每个只能用一次：")
    last = ""
    while True:
        code = totp.now()
        if code != last:
            left = 30 - int(time.time()) % 30
            print(f"  动态码 {code}（还剩 {left} 秒）", flush=True)
            last = code
        if "--once" in sys.argv:
            return
        time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
