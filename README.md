# container-ctl

**English summary.** `container-ctl` is a standalone Linux container lifecycle and Quadlet management toolkit: declaring and deploying `dev` and `prod` containers on rootless Podman + systemd Quadlet, generating `.container`/`.network` units, publishing and verifying OCI release artifacts, provisioning databases and secrets, running backups, managing SSH/WSL lifecycle, and executing disposable or session-isolated workloads via `container_ctl.exec_run` (a pure-stdlib subprocess interface that reads an `ExecSpec` from `stdin` and invokes rootless `podman`).
- **Install:** `pip install -e .` (or `pip install -e ".[doctor,scanner]"`). Entry points: `container-ctl`, `container-control`, `container-params`, `container-doctor`.
- **Requirements:** Python >= 3.11 (tests run on 3.11–3.13), `podman` for real deployments. Optional extras (`pyproject.toml`): `doctor` (`jsonschema`), `scanner` (`PyYAML`).
- **Test:** `python3 -m pytest tests container_control/tests container_params/tests tools/container-doctor/container_doctor/tests tools/scan-path-refs/tests -p no:cacheprovider -q`.
- **License:** Apache-2.0 (`LICENSE`). Security policy: `SECURITY.md`. Contribution guide: `CONTRIBUTING.md`.

---

通用專案容器化管控套件：dev/prod 宣告與部署、Quadlet 產生、發布與驗證、資料庫配置與升級、備份、SSH 生命週期、WSL 關機，以及拋棄式／工作階段隔離執行。單一 repo、單一 Python 套件 `container_ctl`。

## 執行面介面（`exec_run`）
隔離執行面由 `container_ctl/exec_run.py`（ExecSpec 由 stdin 傳入，只用標準庫、無套件內依賴）、`egress/`（egress 代理與白名單）與 `docs/EXEC-SPEC-INTERFACE.md` 組成：
- 這條介面是契約（子行程加 stdin ExecSpec），不依賴套件內其他模組。
- 可獨立部署 `container_ctl/__init__.py` 與 `exec_run.py` 至執行節點。

## 套件結構
- `container_ctl/`：CLI 主體（`container_ctl/cli/main.py`）與 `exec_run.py`。
- `container_control/`、`container_params/`、`tools/container-doctor/`、`tools/scan-path-refs/`、`standards/`、`dockerfiles/`、`ext-inject/`、`ide-inject/`、`egress/`。
- 設定：`/etc/container-ctl/container-ctl.conf`（範本見 `deploy/container-ctl.conf.template`）。

## 測試
`python3 -m pytest tests -p no:cacheprovider -q`。缺 `podman`、`jsonschema`、PyYAML 或 `extscope` CLI 時，相關測試自動 skip。
