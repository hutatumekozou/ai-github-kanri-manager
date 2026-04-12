# ヤフオク通知サイト

ヤフオクの商品URLと通知時刻を登録すると、指定時刻にDiscordへ通知を送る個人用サイトです。

## できること

- 商品URLを登録
- 日本時間で通知時刻を登録
- Discord Webhook チャンネルへ通知を送信
- 登録済み通知の一覧表示
- 不要な通知の削除

## セットアップ

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

`.env` に Discord Webhook 情報を入れてください。

## 起動

```bash
source .venv/bin/activate
python3 app.py
```

ブラウザで `http://127.0.0.1:5001` を開きます。

- 新規登録画面: `http://127.0.0.1:5001/alerts/new`
- 一覧画面: `http://127.0.0.1:5001/alerts/list`

## Discord設定

- `DISCORD_WEBHOOK_URL`: DiscordチャンネルのWebhook URL
- `DISCORD_USERNAME`: 投稿時に表示するBot名
- `DISCORD_AVATAR_URL`: 投稿時に表示するアイコンURL。空で可
- `DISCORD_MENTION_TEXT`: `@everyone` やロールメンションを先頭に付けたいときに使用。空で可

## メモ

- 通知データはローカルの `app.db` に保存されます。
- アプリを再起動すると、未送信の通知は自動で再登録されます。
- サーバーを止めている間に時刻を過ぎた通知は、起動時に即時送信を試みます。
- `.env` を更新したら Flask の再起動が必要です。
- DiscordのWebhook URLは外部に漏らさないでください。

## Vercelについて

- Vercel ではローカルSQLiteと常駐スケジューラ前提の構成は安定運用できません。
- このリポジトリを Vercel に載せる場合、画面表示はできますが、登録保存と時刻通知は本番用途には向きません。
- 通知までWeb公開で運用するなら、外部DBとCron対応に組み替えるか、常駐型ホスティングを使ってください。
