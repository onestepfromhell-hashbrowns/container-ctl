# ADR-009: 映像（artifact）與部署配置（config）分離政策

- **日期**：2026-10-08
- **狀態**：Accepted（2026-10-08，使用者授權由 agent 決策；實作計畫見下〈落地〉）
- **起因**：ServiceA prod 0.3.5 上線時「改 3 個環境值」被迫走「發新版」；根因是政策讓「版本」同時代表映像、配置與單元身分。
- **關聯**：ADR-008（設定 repo）、SPEC §20／§22

## 第一性原理

R1 被驗的就是會上線的（同 digest 跨環境）；R2 環境差異在執行期注入、不烙進映像；R3 改環境值只需重建容器；R4 「現在該跑什麼」單一權威且可稽核；R5 能改配置者≠能改程式碼者。

## 政策審查：現行條文與 R1–R5 的偏離

| # | 現行條文 | 偏離 |
|---|---|---|
| a | recipe R1「同一宣告得到同一結果；映像 `@sha256` 固定」 | 把「宣告」與「映像」當同一件事。宣告檔同時放 `image`（artifact）與 env／埠／掛載／網路（配置），單一檔單一修訂，無法各自演進 |
| b | D1「版本號代表凍結的業務邏輯」＋ `anchor` 要求宣告 `version` 與 release tag 同步 | 版本是 artifact 身分沒錯，但實作讓宣告的 `version` 欄隨配置提交被改寫，配置變更被迫耦合發版 |
| c | SPEC 容器命名 `svc-<name>-<version>`、state.db id 含 version | 單元身分＝版本；同版本改配置無法表達，換版又要處理舊實例讓位（埠、連線） |
| d | release tag 指向「含 pin 的設定 commit」（`config_commit`） | tag 同時像在凍結配置；回滾時是否還原配置無明文政策（現行只換 image pin） |
| e | `tier`（dev/prod）決定映像形態，同時決定 Host IP（127.0.0.2）、`<site-var-root>/...` 路徑 | 把「映像角色」與「部署環境」混成一軸。127.0.0.2、`<site-var-root>` 是 site 宿主的規則，不是 prod 映像的屬性；site 演練 remote-vps 時無處放第二套值 |
| f | ADR-008「多環境放 `.deploy/envs/<env>/containers/`，讀取層未實作」 | 多環境只有一句話，沒有 overlay 語意，R1 的跨環境演練沒有政策依據 |
| g | 無條文禁止映像內含環境值；`image_checks` 只擋 `/workspace`、debugpy | R2 沒有 `[T]` 也沒有 `[D]` 條文，只靠自覺 |
| h | 回滾（D7）「redeploy 到舊版本的 tag 記錄」 | 未定義「舊版」＝實際運行的上一個健康修訂；以收據／state.db 推算會錯（已發生） |

## 決策

- **P1 兩條獨立的軸**。artifact＝映像 digest（身分：release tag `release/<svc>/<ver>`，不可覆寫）；配置＝設定 repo 修訂（身分：commit，稽核：`config-audit.jsonl`）。任何變更先分類：

| 變更 | 走法 | 是否發版／重建映像 |
|---|---|---|
| env、埠、掛載、網路、資源、探針 | `config set`（或改設定 repo）→ `redeploy` | 否 |
| 程式碼、依賴 | `prod up --ver <新版>` | 是 |
| 回滾映像 | 回到**實際運行**的上一個健康 release（`prod rollback`） | 否 |
| 回滾配置 | 還原設定 repo 修訂（git revert）→ `redeploy` | 否 |

- **P2 映像不含環境值**。建置輸入只有 source commit＋version；映像不得內嵌上游 URL、主機名／IP、埠、金鑰、環境專屬路徑；這些一律執行期以 `env`／`*_FILE`／掛載注入。`[D]` 先文件化，工具檢查列後續（見下）。
- **P3 `version` 只表 artifact**。`prod up` 之外的流程不得改寫宣告的 `version`／`image`；`config set` 只動 env。release tag 只凍結 image digest；`config_commit` 僅供追溯，**不作為回滾還原配置的依據**。
- **P4 tier ≠ 環境**。tier 只決定映像形態（dev＝工具鏈＋掛載；prod＝含碼唯讀）。Host IP、宿主路徑、上游位址屬「環境」。現行 `127.0.0.2`、`<site-var-root>/...` 明列為 **site 環境 profile**，不是 prod 的本質規則；另一環境（remote-vps）有自己的 profile，由該環境的部署規範管理（site 不管遠端）。
- **P5 單一權威、舊者失效**。設定 repo 存在後，程式碼 repo／工作樹的 `.deploy` 一律非權威；工具對其差異報告「已失效」（已實作，validate／prod doctor）。

## 後果

- ✅ 改配置不發版；映像跨環境一致；回滾目標明確。
- ⚠️ P4 需要 overlay 語意才能真正落地（見待裁決 1）。
- ⚠️ 現行 `prod doctor` 仍用 site 專屬規則檢查所有 prod 宣告；在 overlay 前不改。

## 裁決結果（取代原〈待裁決〉）

1. **環境 overlay：做**（P1，已實作）。`.deploy/envs/<env>/containers/<svc>.json` + `profile.json`，白名單合併，預設環境 site。
2. **單元命名：做，且 opt-in**（P5，已實作）。服務宣告 `"unit_naming": "stable"` → 單元 `svc-<name>`、DB id 不含版本，release 以 `org.container-ctl.release` label 保留；預設 `versioned` 維持舊行為，故不影響運行中服務。遷移＝逐服務在宣告加一行並 `rollout apply`（舊單元由 redeploy 先停後啟，固定宿主埠時明示停機）。`update_strategy` 目前只支援 `stop-first`；`start-first` 待有無固定埠的服務需要時再做。
3. **映像 ENV 掃描：做**（P2，已實作），WARN → 一個版本後升 ERROR。

## 落地

| 階段 | 內容 | commit |
|---|---|---|
| 既有 | `config set`、舊 `.deploy` 失效警告、回滾取實際運行、埠衝突預檢 | 396ac6e |
| P1 | overlay／profile／`config set --env`／`config show` | 884ccaf |
| P2 | `contract.json`／`config check`／映像 ENV 掃描 | 5184064 |
| P3 | OCI manifest digest／OCI layout store／release schema 2／`image verify`（ADR-010） | f65c3f3 |
| P4 | `rollouts` 紀錄／`rollout apply|undo|status`／`reconcile`（ADR-011） | 75edab8 |
| P5 | `unit_naming: stable`／固定埠先停舊再啟新 | 0a7998e |
| P6 | `release export --format bundle2`（OCI layout＋compose.yaml＋.env.example＋load.sh；無環境值） | 6415b3e |
| P7 | 供應鏈掛點：release 記錄 sbom/signature/provenance 欄位、`image verify --require-signature`；syft／cosign 未裝時為 null（工具安裝待使用者批准） | 本次 |
