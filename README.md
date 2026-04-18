# AI作品管理ボード

AIのバイブコーディング作品を一覧で管理し、作品ごとの GitHub リンクと更新履歴を記録する Flask アプリです。

## できること

- 作品名、GitHubリンク、メモを登録
- 各作品に対して最終保存日つきの更新履歴を追加
- 一覧表で `ID / 作品名 / 更新情報 / 最終保存日 / メモ` を確認
- `中を見る` から作品ごとの詳細ページへ移動
- 詳細ページで GitHubリンク、最終保存日、更新内容を確認

## セットアップ

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 起動

```bash
source .venv/bin/activate
python3 app.py
```

1コマンドで起動する場合:

```bash
./start.command
```

ブラウザで `http://127.0.0.1:5001` を開きます。

- 一覧画面: `http://127.0.0.1:5001/works`
- 新規登録画面: `http://127.0.0.1:5001/works/new`

## 保存先

- ローカルでは `app.db` を使用します。
- Vercel では `DATABASE_URL` を設定すると永続保存できます。
- Postgres を使う場合は `psycopg` が必要です。
