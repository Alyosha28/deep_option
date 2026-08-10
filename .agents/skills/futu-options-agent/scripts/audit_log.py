#!/usr/bin/env python3
"""GOAI 期权智能终端 - 审计留痕工具。

追加 JSONL 记录到审计日志，并使用 SHA-256 哈希链提供基础篡改迹象检测。
这是本地原型，不替代文件锁、签名、外部锚定或独立 verifier。

用法:
  echo '{"underlying":"HK.00700"}' | python audit_log.py --event scenario_parsed
"""
import argparse
import datetime
import hashlib
import json
import pathlib
import sys


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[4]
AUDIT_ROOT = PROJECT_ROOT / "research" / "audit"
DEFAULT_LOG = AUDIT_ROOT / "audit_log.jsonl"
MAX_PAYLOAD_BYTES = 64 * 1024
SECRET_KEY_FRAGMENTS = {
    "api_key", "authorization", "client_secret", "cookie", "password",
    "private_key", "secret", "token", "trade_password",
}
IDENTIFIER_KEYS = {"acc_id", "account", "account_id", "broker_order_id", "order_id"}


def canonical(payload):
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sanitize(payload):
    if isinstance(payload, dict):
        clean = {}
        for key, value in payload.items():
            normalized = str(key).lower()
            if any(fragment in normalized for fragment in SECRET_KEY_FRAGMENTS):
                clean[key] = "[REDACTED]"
            elif normalized in IDENTIFIER_KEYS:
                clean[key] = "[REDACTED_ID]"
            else:
                clean[key] = sanitize(value)
        return clean
    if isinstance(payload, list):
        return [sanitize(value) for value in payload]
    return payload


def resolve_log_path(raw_path):
    path = pathlib.Path(raw_path).resolve()
    audit_root = AUDIT_ROOT.resolve()
    try:
        path.relative_to(audit_root)
    except ValueError as exc:
        raise ValueError(f"Audit log must stay under {audit_root}") from exc
    return path


def main():
    ap = argparse.ArgumentParser(description="Append an audit record with a SHA-256 hash chain.")
    ap.add_argument("--event", required=True, help="Audit event name, e.g. scenario_parsed")
    ap.add_argument("--log", default=str(DEFAULT_LOG), help="Audit log path")
    args = ap.parse_args()

    raw_payload = sys.stdin.buffer.read(MAX_PAYLOAD_BYTES + 1)
    if raw_payload:
        if len(raw_payload) > MAX_PAYLOAD_BYTES:
            raise ValueError("Audit payload exceeds 64 KiB")
        payload = json.loads(raw_payload.decode("utf-8"))
    else:
        payload = {}
    payload = sanitize(payload)

    log_path = resolve_log_path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    prev_hash = ""
    if log_path.exists():
        with open(log_path, "r", encoding="utf-8-sig") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    prev_hash = json.loads(line)["hash"]

    record = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "event": args.event,
        "payload": payload,
        "prev_hash": prev_hash,
    }
    record["hash"] = hashlib.sha256(canonical(record).encode("utf-8")).hexdigest()
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    print(json.dumps({"log": str(log_path), "prev_hash": prev_hash, "hash": record["hash"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
