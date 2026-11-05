import os
import tempfile

# 測試與站點無關：不讀站點設定檔，資料根指向暫存目錄（container_ctl.paths 於 import 時解析，所以要在最前面設）
os.environ["CONTAINER_CTL_CONFIG"] = "/nonexistent"
os.environ.setdefault("CONTAINER_CTL_DATA_ROOT", tempfile.mkdtemp(prefix="container-ctl-test-", dir="/var/tmp"))

# 測試預設不寫真實 OCI store（需要真的 podman push）；artifact_store 自身以注入 runner 測試
os.environ.setdefault("CONTAINER_CTL_OCI_STORE", "off")
os.environ.setdefault("CONTAINER_CTL_ROLLOUT_RECORD", "off")  # 單元測試不寫真實 state.db
