"""Local examiner account provisioning; only salted password hashes are stored."""

import argparse
import getpass
import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path

DEFAULT_ACCOUNTS = Path(__file__).resolve().parents[3] / "runtime/admin-accounts.json"
ITERATIONS = 600_000


def accounts_path() -> Path:
    return Path(os.environ.get("ADMIN_ACCOUNTS_FILE", str(DEFAULT_ACCOUNTS)))


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), ITERATIONS)
    return f"{salt}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    salt, _ = encoded.split("$", 1)
    return hmac.compare_digest(hash_password(password, salt), encoded)


def load_accounts() -> dict:
    path = accounts_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Create or update a local examiner account")
    parser.add_argument("username")
    parser.add_argument("--name", required=True, help="Examiner display name")
    args = parser.parse_args()
    username, name = args.username.strip(), args.name.strip()
    if not username or not name or len(username) > 64 or len(name) > 80:
        parser.error("Username and name must be nonempty (max 64/80 characters)")
    password = getpass.getpass("Password (at least 10 characters): ")
    if len(password) < 10 or len(password) > 128:
        parser.error("Password must contain 10 to 128 characters")
    if password != getpass.getpass("Confirm password: "):
        parser.error("Passwords do not match")
    accounts = load_accounts()
    accounts[username] = {"name": name, "password_hash": hash_password(password)}
    path = accounts_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(accounts, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    print("Account saved. Restart the backend to apply changes.")


if __name__ == "__main__":
    main()
