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

## 管理員指令（私訊 bot）
- 派單 訂單內容 → 轉發到群組，自動編號
- 回 001 內容 → 回覆群組，並自動標註（@）喊單的司機；已指派的單只標註被指派的司機
- 指派 001 司機名 → 指派並公告，並標註該司機
- 取消 001 / 列表

## 司機（在群組）
- 標註機器人 + 單號 + 回覆內容，例如：@機器人 001 十分鐘到
- 舊的「喊 001 十分鐘到」也可用
- 沒寫單號一律不指派，bot 會提醒要寫單號
- 有人喊單時，bot 會私訊通知你

## 部署
需 https（Render、Railway、Fly.io 或 VPS）。state.json 是本機檔，平台重啟可能清空，建議掛 volume 或改資料庫。
