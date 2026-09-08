# 客服域名替換模擬展示

這是公開、獨立的介面展示專案。全部使用假域名、假登入和假操作，不會連線 AWS、Google 試算表或 Telegram，也不會變更真實域名。

## Codespaces 展示

在 GitHub 點 **Code → Codespaces → Create codespace**。環境完成後，在下方 **Ports（連接埠）** 找到 **20203**，開啟轉送網址並在後方加上 `/login`。

預設連接埠為 **Private**。管理者同意分享後，可將 20203 的 **Port Visibility** 改為 **Public**，再把展示網址提供給客服。公開儲存庫與公開展示埠是不同設定；不需分享 Codespaces 編輯器網址。

任何取得公開展示網址的人都能以模擬身分操作，不限客服。請勿輸入真實域名、帳密或其他私人資料。所有訪客共用同一個假流程，請輪流試看。

如果需要手動啟動，可在 Codespaces 終端執行：

```sh
python -m turkey_domain_replacer.preview --public-origin "https://$CODESPACE_NAME-20203.app.github.dev"
```

若已啟動，請勿重複執行。停止 Codespace 後網址停止服務；重啟後是全新的假資料，舊流程連結不再有效。試看完請停止 Codespace，避免持續使用運算額度；儲存用量仍依 GitHub 帳號方案計算。

## 試看步驟

1. 開啟 `/login`，自動進入假帳號。
2. 點「開始取得下一組域名」，確認準備新域名。
3. 看到域名紀錄更新後，可查看 v2、v3、f3、c1 四筆「切換前測試網址」，點旁邊的「複製」按鈕。網址會保留試玩路徑與參數，並套用流程的新域名；展示版使用 `new.example` 假域名，無法實際開啟遊戲。接著點「我已完成公司後台切換」（此處也是模擬，不需操作真實後台）。
4. 清理確認輸入 `old.example`；若輸入 `new.example`，會顯示中文錯誤提示。
5. 完成後可查看歷史與台北時間的操作紀錄。

## 本機啟動

需要 Python 3.11 以上；Codespaces 設定使用 Python 3.12。

```sh
python -m venv .venv
```

Windows：

```powershell
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\python.exe -m turkey_domain_replacer.preview
```

macOS / Linux：

```sh
.venv/bin/python -m pip install .
.venv/bin/python -m turkey_domain_replacer.preview
```

開啟 [本機模擬版](http://127.0.0.1:20203/login)。預設只監聽本機；Codespaces 透過平台轉送 20203。

## 測試與安全邊界

```sh
python -m pip install '.[dev]'
python -m pytest -W error -q
```

測試禁止外部網路連線，包含流程順序、來源／CSRF 驗證與敏感識別資訊隱藏。`example` 域名、測試資源 ID 與 `111111111111` 都是合成資料。正式執行模式仍停用，這不是正式域名管理服務。

本專案以全新歷史建立，不含原雲端專案的文件、設定、憑證、資料庫、日誌或提交歷史。
