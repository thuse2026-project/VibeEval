"""Provision a one-use candidate invitation without adding an admin issuance UI."""

import argparse
from datetime import datetime

from .store import EntryStore


def main():
    parser = argparse.ArgumentParser(description="预配置考生一次性入场凭证")
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--exam-id", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--duration-minutes", type=int, default=60)
    parser.add_argument("--valid-hours", type=int, default=24)
    args = parser.parse_args()
    try:
        token, expires = EntryStore().invite(
            args.candidate_id, args.name, args.exam_id, args.title,
            args.duration_minutes, args.valid_hours,
        )
    except ValueError as error:
        parser.error(str(error))
    print(f"首次入场截止：{datetime.fromtimestamp(expires).astimezone().isoformat(timespec='seconds')}")
    print("请私下发给对应考生，凭证仅在此输出，服务端只存哈希：")
    print(token)
    print("入场页面：http://127.0.0.1:5173/entry")


if __name__ == "__main__":
    main()
