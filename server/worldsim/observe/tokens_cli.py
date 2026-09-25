#!/usr/bin/env python3
"""obs-api token 管理 CLI（05 T-WEB-02；03 §8.3）。

用法（00 §1 A14；库路径 env `WSIM_OBS_TOKEN_DB`，默认 `var/observe/tokens.db`）：
  cd server && uv run python -m worldsim.observe.tokens_cli issue --label alice   # 签发（超 10 行容量拒发）
  cd server && uv run python -m worldsim.observe.tokens_cli revoke --token dev_xxx
  cd server && uv run python -m worldsim.observe.tokens_cli list
"""

from __future__ import annotations

import argparse
import sys

from .auth import CapacityError, TokenStore


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="obs-api token 管理（03 §8.3，10 行容量）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_issue = sub.add_parser("issue", help="签发 token")
    p_issue.add_argument("--label", required=True, help="用户标识（种子用户区分到人）")
    p_revoke = sub.add_parser("revoke", help="吊销（disabled=1，不可逆）")
    p_revoke.add_argument("--token", required=True)
    sub.add_parser("list", help="列出全部 token")
    args = ap.parse_args(argv)

    store = TokenStore()
    if args.cmd == "issue":
        try:
            token = store.issue(args.label)
        except CapacityError as e:
            print(f"FAIL: {e}", file=sys.stderr)
            return 1
        print(token)
        return 0
    if args.cmd == "revoke":
        if not store.revoke(args.token):
            print("FAIL: token 不存在", file=sys.stderr)
            return 1
        print(f"revoked {args.token}")
        return 0
    for row in store.list():
        flag = " (disabled)" if row["disabled"] else ""
        print(f"{row['token']}\t{row['user_label']}\t{row['issued_at']}{flag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
