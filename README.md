# LINE 派單機器人

## 安裝
pip install flask line-bot-sdk gunicorn
export LINE_CHANNEL_SECRET=你的secret
export LINE_CHANNEL_ACCESS_TOKEN=你的token
export ADMIN_PASSWORD=自訂密碼
gunicorn app:app --workers 1 --threads 4

## LINE 後台設定
1. LINE Developers 建立 Messaging API channel
2. Webhook URL 設為 https://你的網域/callback，開啟 Use webhook
3. 在 Official Account Manager 允許加入群組、關閉自動回應訊息

## 第一次使用
1. 私訊 bot：我是管理員 你的密碼
2. 把 bot 拉進司機群組，在群組輸入：綁定群組

## 管理員指令（私訊 bot，沒有單號）
- 派單 訂單內容 → 轉發到群組，之後每 10 秒重送，直到取消或指派
- 回 司機名 內容 → 回覆群組並自動標註（@）該司機
- 指派 司機名 訂單關鍵字 → 指派並公告（標註該司機），停止重送
- 取消 訂單關鍵字 → 取消訂單，停止重送
- 列表 → 看未指派的單
- 只有一張未指派的單時，關鍵字可省略；有多筆符合會列出請你改用更明確的關鍵字

## 司機（在群組）
- 標註機器人 + 內容 時間，例如：@機器人 桃園機場 18:30
- 舊的「喊 桃園機場 18:30」也可用
- 喊單成功不會在群組顯示，只私訊通知管理員；沒寫內容時 bot 會提醒

## 部署
需 https（Render、Railway、Fly.io 或 VPS）。state.json 是本機檔，平台重啟可能清空，建議掛 volume 或改資料庫。
