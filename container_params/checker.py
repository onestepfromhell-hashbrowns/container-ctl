"""
container_params.checker — 5 大架構反模式靜態與動態檢查器 (Anti-Pattern Checker)

檢測 5 大對抗式反模式：
1. AP-1: 業務代碼直接拼裝絕對路徑 (/workspace, /opt/<slug>, /home/<user>)
2. AP-2: 環境分支污染業務邏輯 (if env == 'dev', if tier == 'prod', if os.getenv('APP_ENV') == 'dev')
3. AP-3: 寫死特定版本端點與埠號 (http://svc-dev-..., http://localhost:8001)
4. AP-4: 控制腳本路徑寫死 (control.sh PID/Log 未使用動態變數)
5. AP-5: 資料庫憑證與密鑰硬編碼 (postgresql://user:pass@...)
"""

from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional, Sequence


class AntiPatternType(str, Enum):
    AP1_ABSOLUTE_PATH = "AP1_ABSOLUTE_PATH"
    AP2_ENV_BRANCHING = "AP2_ENV_BRANCHING"
    AP3_HARDCODED_ENDPOINT = "AP3_HARDCODED_ENDPOINT"
    AP4_CONTROL_SCRIPT_PATH = "AP4_CONTROL_SCRIPT_PATH"
    AP5_HARDCODED_CREDENTIALS = "AP5_HARDCODED_CREDENTIALS"


@dataclass
class AntiPatternViolation:
    pattern_type: AntiPatternType
    file_path: Path
    line_number: int
    line_content: str
    message: str
    severity: str = "error"  # error | warning

    def to_dict(self) -> dict:
        return {
            "type": self.pattern_type.value,
            "file": str(self.file_path),
            "line": self.line_number,
            "content": self.line_content.strip(),
            "message": self.message,
            "severity": self.severity,
        }


# Regex 規則表
_PATH_PATTERNS = [
    (re.compile(r"['\"]/(?:workspace|opt/[a-zA-Z0-9_-]+|home/[a-zA-Z0-9_-]+)/[^'\"]*['\"]"), "硬編碼絕對路徑引用"),
]

_ENDPOINT_PATTERNS = [
    (re.compile(r"https?://(?:svc-(?:dev|prod)-[a-zA-Z0-9_.-]+|localhost:\d{2,5}|127\.0\.0\.1:\d{2,5})"), "寫死特定版本端點或 localhost 埠號"),
]

_CREDENTIAL_PATTERNS = [
    (re.compile(r"postgres(?:ql)?://[a-zA-Z0-9_]+:[^@\s\"'\$]+@[a-zA-Z0-9_.-]+:\d+/"), "硬編碼資料庫連線帳號密碼"),
    (re.compile(r"(?:api_key|secret_key|jwt_secret)\s*=\s*['\"][a-zA-Z0-9_\-]{8,}['\"]", re.IGNORECASE), "硬編碼連線金鑰或 Secret"),
]

_ENV_BRANCH_PATTERNS = [
    (re.compile(r"""(?:os\.getenv|os\.environ\.get)\s*\(\s*['"](?:APP_ENV|ENVIRONMENT|TIER|ENV)['"]\s*\)\s*==\s*['"](?:dev|development|prod|production)['"]"""), "業務邏輯中存在環境分支判定 (違反 Environment-Blind 原則)"),
]

_SKIP_DIRS = {
    ".git",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    ".pytest_cache",
    "dist",
    "build",
    "tests",
    "test",
    ".scratch",
    "docs",
}
_SKIP_EXTS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".svg",
    ".pyc",
    ".lock",
    ".so",
    ".bin",
    ".md",
    ".rst",
    ".txt",
}



class AntiPatternChecker:
    """代碼與設定檔反模式掃描器。"""

    def __init__(
        self,
        project_slug: Optional[str] = None,
        whitelist_paths: Optional[Sequence[str]] = None,
    ) -> None:
        self.project_slug = project_slug
        self.whitelist_paths = set(whitelist_paths or [])

    def is_whitelisted(self, file_path: Path) -> bool:
        p_str = file_path.as_posix()
        for wl in self.whitelist_paths:
            if wl in p_str:
                return True
        # 預設略過測試目錄 fixture 或自身路徑解析模組 (app_paths.py, paths.py)
        if "test" in p_str and ("fixtures" in p_str or "mock" in p_str):
            return True
        if file_path.name in ("app_paths.py", "paths.py", "path_resolver.py"):
            return True
        return False

    def scan_file(self, file_path: Path) -> list[AntiPatternViolation]:
        if self.is_whitelisted(file_path):
            return []

        violations: list[AntiPatternViolation] = []
        try:
            content = file_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return violations

        lines = content.splitlines()

        # 1. 腳本檔案 (control.sh, entrypoint.sh) 專屬檢查 AP-4
        if file_path.name in ("control.sh", "docker-entrypoint.sh", "entrypoint.sh") or file_path.suffix == ".sh":
            for lineno, line in enumerate(lines, 1):
                if line.strip().startswith("#"):
                    continue
                # 檢查是否直接寫死 PID_FILE=/workspace/... 而未使用 ${APP_RUNTIME_DIR:-...}
                if re.search(r"PID_FILE=['\"]?/(?:workspace|opt|run)/", line) and "${" not in line:
                    violations.append(
                        AntiPatternViolation(
                            pattern_type=AntiPatternType.AP4_CONTROL_SCRIPT_PATH,
                            file_path=file_path,
                            line_number=lineno,
                            line_content=line,
                            message="控制腳本直接寫死 PID/Log 實體路徑，未採用 ${APP_RUNTIME_DIR:-...} 變數推進",
                        )
                    )

        # 2. 正則文字掃描 AP-1, AP-3, AP-5
        for lineno, line in enumerate(lines, 1):
            sline = line.strip()
            if sline.startswith("#") or sline.startswith("//"):
                continue

            # AP-1
            for pat, msg in _PATH_PATTERNS:
                if pat.search(line):
                    violations.append(
                        AntiPatternViolation(
                            pattern_type=AntiPatternType.AP1_ABSOLUTE_PATH,
                            file_path=file_path,
                            line_number=lineno,
                            line_content=line,
                            message=f"{msg}：請改用 Path Resolver (如 app_paths.py)",
                        )
                    )

            # AP-3
            for pat, msg in _ENDPOINT_PATTERNS:
                if pat.search(line):
                    violations.append(
                        AntiPatternViolation(
                            pattern_type=AntiPatternType.AP3_HARDCODED_ENDPOINT,
                            file_path=file_path,
                            line_number=lineno,
                            line_content=line,
                            message=f"{msg}：服務交互端點必須由環境變數注入",
                        )
                    )

            # AP-5
            for pat, msg in _CREDENTIAL_PATTERNS:
                if pat.search(line):
                    violations.append(
                        AntiPatternViolation(
                            pattern_type=AntiPatternType.AP5_HARDCODED_CREDENTIALS,
                            file_path=file_path,
                            line_number=lineno,
                            line_content=line,
                            message=f"{msg}：密鑰與資料庫連線必須由外部環境注入，代碼預設值應為 None",
                        )
                    )

            # AP-2 (Regex 輔助)
            for pat, msg in _ENV_BRANCH_PATTERNS:
                if pat.search(line):
                    violations.append(
                        AntiPatternViolation(
                            pattern_type=AntiPatternType.AP2_ENV_BRANCHING,
                            file_path=file_path,
                            line_number=lineno,
                            line_content=line,
                            message=msg,
                        )
                    )

        # 3. 若為 Python 檔案，執行 AST 深層語法樹檢查 (AP-2)
        if file_path.suffix == ".py":
            try:
                tree = ast.parse(content, filename=str(file_path))
                violations.extend(self._scan_ast(tree, file_path, lines))
            except SyntaxError:
                pass

        return violations

    def _scan_ast(
        self, tree: ast.AST, file_path: Path, lines: list[str]
    ) -> list[AntiPatternViolation]:
        violations: list[AntiPatternViolation] = []

        class EnvBranchVisitor(ast.NodeVisitor):
            def visit_If(self, node: ast.If) -> None:
                # 檢查 if 條件中是否有 os.getenv('APP_ENV') == 'dev'
                cond_repr = ast.unparse(node.test) if hasattr(ast, "unparse") else ""
                if any(
                    k in cond_repr
                    for k in [
                        "APP_ENV",
                        "ENVIRONMENT",
                        "ENV",
                        "TIER",
                    ]
                ) and any(v in cond_repr for v in ["'dev'", '"dev"', "'prod'", '"prod"']):
                    lineno = node.lineno
                    line_text = lines[lineno - 1] if lineno <= len(lines) else cond_repr
                    violations.append(
                        AntiPatternViolation(
                            pattern_type=AntiPatternType.AP2_ENV_BRANCHING,
                            file_path=file_path,
                            line_number=lineno,
                            line_content=line_text,
                            message=f"AST 偵測到環境分支條件: '{cond_repr}'，違反 Environment-Blind 原則",
                        )
                    )
                self.generic_visit(node)

        visitor = EnvBranchVisitor()
        visitor.visit(tree)
        return violations


def check_codebase(
    root_dir: Path,
    project_slug: Optional[str] = None,
    whitelist_paths: Optional[Sequence[str]] = None,
) -> list[AntiPatternViolation]:
    """掃描整個代碼庫。"""
    checker = AntiPatternChecker(project_slug=project_slug, whitelist_paths=whitelist_paths)
    all_violations: list[AntiPatternViolation] = []

    for dirpath, dirnames, filenames in os.walk(root_dir):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            if ext in _SKIP_EXTS:
                continue
            fpath = Path(dirpath) / fname
            violations = checker.scan_file(fpath)
            all_violations.extend(violations)

    return all_violations
