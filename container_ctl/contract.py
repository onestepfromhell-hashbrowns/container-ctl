"""配置契約（ADR-009 P2；對標 Helm values.schema.json，JSON Schema 2020-12）。

`.deploy/contract.json`：
  {"schema_version": 1,
   "env": { <JSON Schema：env 物件>,  屬性可加 "x-secret-file": true },
   "services": { "<svc>": {"env": <JSON Schema>} }}   # 選填，覆蓋頂層對該服務的 env schema
`x-secret-file: true` 的鍵：名稱須以 _FILE 結尾，值須落在該服務宣告的某個掛載點（container_path）之下。
契約描述「環境必須提供什麼」，不含任何環境值；跨環境共用，各環境的值由 base／overlay 提供並於此驗證。
"""

from __future__ import annotations

import hashlib
import json
import subprocess


class ContractError(Exception):
    pass


def read_contract(repo: str, ref: str) -> dict | None:
    found = subprocess.run(["git", "-C", repo, "show", f"{ref}:.deploy/contract.json"], capture_output=True, text=True)
    if found.returncode != 0:
        return None
    try:
        data = json.loads(found.stdout)
    except ValueError as exc:
        raise ContractError(f"contract.json 不是有效 JSON：{exc}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ContractError("contract.json 須為 object 且 schema_version=1")
    return data


def contract_hash(contract: dict) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def env_schema_for(contract: dict, service: str) -> dict | None:
    return ((contract.get("services") or {}).get(service) or {}).get("env") or contract.get("env")


def check_contract(cfg: dict, contract: dict | None) -> list[str]:
    """回傳訊息列表（[ERROR]／[WARN]）。contract 為 None → 缺契約 WARN。"""
    name = cfg.get("name", "unknown")
    if contract is None:
        return [f"[WARN] {name}: 專案沒有 .deploy/contract.json；環境值未經契約驗證（stable 專案應補）"] if cfg.get("tier") == "prod" else []
    schema = env_schema_for(contract, name)
    if not schema:
        return []
    try:
        import jsonschema
    except ImportError:
        return [f"[WARN] {name}: 宿主缺 jsonschema，略過契約驗證"]
    env = cfg.get("env") or {}
    out = []
    validator = jsonschema.Draft202012Validator(schema)
    for err in sorted(validator.iter_errors(env), key=lambda e: list(e.path)):
        where = ".".join(str(p) for p in err.path) or "(env)"
        out.append(f"[ERROR] {name}: env 違反契約 {where}：{err.message}")
    mounts = [str(v.get("container_path") or "") for v in cfg.get("volumes") or [] if isinstance(v, dict)]
    for key, prop in (schema.get("properties") or {}).items():
        if not (isinstance(prop, dict) and prop.get("x-secret-file")):
            continue
        if not key.endswith("_FILE"):
            out.append(f"[ERROR] {name}: 契約將 {key} 標為機密檔，鍵名須以 _FILE 結尾")
        value = env.get(key)
        if value and not any(value == m or value.startswith(m.rstrip("/") + "/") for m in mounts if m):
            out.append(f"[ERROR] {name}: env {key}={value} 不在任何宣告的掛載點之下（機密檔須由 volumes 掛入）")
    return out
