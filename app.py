from __future__ import annotations

import atexit
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.request import Request, urlopen
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from flask import Flask, flash, g, redirect, render_template, request, url_for

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
IS_VERCEL = bool(os.getenv("VERCEL"))
DATABASE_PATH = Path("/tmp/app.db") if IS_VERCEL else BASE_DIR / "app.db"
TIMEZONE = ZoneInfo(os.getenv("APP_TIMEZONE", "Asia/Tokyo"))

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "dev-secret-key")

scheduler = BackgroundScheduler(timezone=str(TIMEZONE))


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        connection = sqlite3.connect(DATABASE_PATH)
        connection.row_factory = sqlite3.Row
        g.db = connection
    return g.db


@app.teardown_appcontext
def close_db(_: Optional[BaseException]) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_name TEXT NOT NULL DEFAULT '',
                item_url TEXT NOT NULL,
                bid_amount TEXT NOT NULL DEFAULT '',
                auction_end_at TEXT,
                notify_at TEXT NOT NULL,
                email_to TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                sent_at TEXT,
                error_message TEXT
            )
            """
        )
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(alerts)").fetchall()
        }
        if "item_name" not in columns:
            connection.execute(
                "ALTER TABLE alerts ADD COLUMN item_name TEXT NOT NULL DEFAULT ''"
            )
        if "bid_amount" not in columns:
            connection.execute(
                "ALTER TABLE alerts ADD COLUMN bid_amount TEXT NOT NULL DEFAULT ''"
            )
        if "auction_end_at" not in columns:
            connection.execute(
                "ALTER TABLE alerts ADD COLUMN auction_end_at TEXT"
            )
        if "enabled" not in columns:
            connection.execute(
                "ALTER TABLE alerts ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1"
            )


def list_alerts(
    search: str = "",
    status_filter: str = "all",
    enabled_filter: str = "all",
) -> list[sqlite3.Row]:
    db = get_db()
    conditions = []
    params: list[Any] = []

    if search:
        like = f"%{search}%"
        conditions.append(
            """
            (
                item_name LIKE ?
                OR item_url LIKE ?
                OR note LIKE ?
            )
            """
        )
        params.extend([like, like, like])

    if status_filter in {"pending", "sent", "failed", "disabled"}:
        conditions.append("status = ?")
        params.append(status_filter)

    if enabled_filter == "enabled":
        conditions.append("enabled = 1")
    elif enabled_filter == "disabled":
        conditions.append("enabled = 0")

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    query = f"""
        SELECT
            id,
            item_name,
            item_url,
            bid_amount,
            auction_end_at,
            notify_at,
            email_to,
            note,
            enabled,
            status,
            created_at,
            sent_at,
            error_message
        FROM alerts
        {where_clause}
        ORDER BY notify_at ASC, id DESC
    """
    return db.execute(query, params).fetchall()


def fetch_alert(alert_id: int) -> Optional[sqlite3.Row]:
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(
            """
            SELECT
                id,
                item_name,
                item_url,
                bid_amount,
                auction_end_at,
                notify_at,
                email_to,
                note,
                enabled,
                status,
                created_at,
                sent_at,
                error_message
            FROM alerts
            WHERE id = ?
            """,
            (alert_id,),
        ).fetchone()


def normalize_url(raw_url: str) -> str:
    parsed = urlparse(raw_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("商品URLは http または https で始まる正しいリンクを指定してください。")
    return raw_url.strip()


def parse_notify_at(raw_notify_at: str) -> datetime:
    try:
        notify_at = datetime.strptime(raw_notify_at, "%Y-%m-%dT%H:%M")
    except ValueError as exc:
        raise ValueError("通知時刻の形式が正しくありません。") from exc

    notify_at = notify_at.replace(tzinfo=TIMEZONE)
    if notify_at <= datetime.now(TIMEZONE):
        raise ValueError("通知時刻は現在より後の時間を指定してください。")
    return notify_at


def parse_optional_datetime(raw_value: str) -> str | None:
    value = raw_value.strip()
    if not value:
        return None

    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M")
    except ValueError as exc:
        raise ValueError("オークション終了時刻の形式が正しくありません。") from exc

    return parsed.replace(tzinfo=TIMEZONE).isoformat()


def format_datetime(value: str | None) -> str:
    if not value:
        return "-"
    return datetime.fromisoformat(value).astimezone(TIMEZONE).strftime("%Y-%m-%d %H:%M")


app.jinja_env.filters["datetime_jst"] = format_datetime


def automation_label(alert: sqlite3.Row) -> str:
    if alert["status"] == "sent":
        return "完了"
    return "ON" if alert["enabled"] else "OFF"


app.jinja_env.filters["automation_label"] = automation_label


def alert_counts() -> dict[str, int]:
    db = get_db()
    row = db.execute(
        """
        SELECT
            COUNT(*) AS total_count,
            SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending_count,
            SUM(CASE WHEN status = 'sent' THEN 1 ELSE 0 END) AS sent_count,
            SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_count,
            SUM(CASE WHEN status = 'disabled' THEN 1 ELSE 0 END) AS disabled_count
        FROM alerts
        """
    ).fetchone()
    return {
        "total": row["total_count"] or 0,
        "pending": row["pending_count"] or 0,
        "sent": row["sent_count"] or 0,
        "failed": row["failed_count"] or 0,
        "disabled": row["disabled_count"] or 0,
    }


def schedule_alert(alert_id: int, notify_at: datetime) -> None:
    scheduler.add_job(
        func=send_notification_job,
        trigger="date",
        run_date=notify_at,
        args=[alert_id],
        id=f"alert-{alert_id}",
        replace_existing=True,
        misfire_grace_time=300,
    )


def unschedule_alert(alert_id: int) -> None:
    try:
        scheduler.remove_job(f"alert-{alert_id}")
    except JobLookupError:
        return


def discord_config() -> dict[str, Any]:
    return {
        "webhook_url": os.getenv("DISCORD_WEBHOOK_URL", ""),
        "username": os.getenv("DISCORD_USERNAME", "Yahuoku Alert Bot"),
        "avatar_url": os.getenv("DISCORD_AVATAR_URL", ""),
        "mention_text": os.getenv("DISCORD_MENTION_TEXT", ""),
    }


def missing_discord_fields() -> list[str]:
    config = discord_config()
    required_keys = ("webhook_url",)
    return [key for key in required_keys if not config[key]]


def discord_status() -> dict[str, Any]:
    config = discord_config()
    missing = missing_discord_fields()
    return {
        "configured": not missing,
        "missing_fields": missing,
        "webhook_url": config["webhook_url"],
        "username": config["username"],
        "avatar_url": config["avatar_url"],
        "mention_text": config["mention_text"],
    }


@app.context_processor
def inject_discord_status() -> dict[str, Any]:
    return {
        "discord": discord_status(),
        "app_env": {
            "is_vercel": IS_VERCEL,
            "storage_mode": "ephemeral" if IS_VERCEL else "local",
            "scheduler_enabled": not IS_VERCEL,
        },
    }


def send_discord_message(content: str) -> None:
    config = discord_config()
    missing = missing_discord_fields()
    if missing:
        raise RuntimeError(f"Discord設定が不足しています: {', '.join(missing)}")

    message_parts = [config["mention_text"].strip(), content.strip()]
    payload = {
        "content": "\n".join(part for part in message_parts if part),
        "username": config["username"],
    }
    if config["avatar_url"]:
        payload["avatar_url"] = config["avatar_url"]

    request = Request(
        config["webhook_url"],
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0",
        },
        method="POST",
    )
    with urlopen(request, timeout=30) as response:
        status_code = getattr(response, "status", response.getcode())
        if status_code >= 400:
            raise RuntimeError(f"Discord Webhook送信に失敗しました: HTTP {status_code}")


def send_discord_notification(alert: sqlite3.Row) -> None:
    notify_at = format_datetime(alert["notify_at"])
    auction_end_at = format_datetime(alert["auction_end_at"])
    content = "\n".join(
        [
            "ヤフオク通知",
            "",
            f"商品名: {alert['item_name'] or '未入力'}",
            f"商品URL: {alert['item_url']}",
            f"入札金額: {alert['bid_amount'] or '-'}",
            f"オークション終了時刻: {auction_end_at}",
            f"通知時刻: {notify_at} ({TIMEZONE.key})",
            f"メモ: {alert['note'] or '-'}",
        ]
    )
    send_discord_message(content)


def mark_alert_sent(alert_id: int) -> None:
    timestamp = datetime.now(TIMEZONE).isoformat()
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.execute(
            """
            UPDATE alerts
            SET status = 'sent', sent_at = ?, error_message = NULL
            WHERE id = ?
            """,
            (timestamp, alert_id),
        )


def mark_alert_failed(alert_id: int, error_message: str) -> None:
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.execute(
            """
            UPDATE alerts
            SET status = 'failed', error_message = ?
            WHERE id = ?
            """,
            (error_message[:500], alert_id),
        )


def send_notification_job(alert_id: int) -> None:
    alert = fetch_alert(alert_id)
    if alert is None or alert["status"] != "pending" or not alert["enabled"]:
        return

    try:
        send_discord_notification(alert)
    except Exception as exc:
        mark_alert_failed(alert_id, str(exc))
        return

    mark_alert_sent(alert_id)


def reschedule_pending_alerts() -> None:
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.row_factory = sqlite3.Row
        pending_alerts = connection.execute(
            """
            SELECT id, notify_at
            FROM alerts
            WHERE status = 'pending' AND enabled = 1
            """
        ).fetchall()

    now = datetime.now(TIMEZONE)
    for alert in pending_alerts:
        notify_at = datetime.fromisoformat(alert["notify_at"]).astimezone(TIMEZONE)
        if notify_at <= now:
            send_notification_job(alert["id"])
            continue
        schedule_alert(alert["id"], notify_at)


@app.route("/", methods=["GET"])
def index() -> Any:
    return redirect(url_for("new_alert"))


@app.route("/alerts/new", methods=["GET"])
def new_alert() -> str:
    now = datetime.now(TIMEZONE)
    return render_template(
        "new_alert.html",
        now=now,
        min_notify_at=now.strftime("%Y-%m-%dT%H:%M"),
        active_page="new",
    )


@app.route("/alerts/list", methods=["GET"])
def alert_list() -> str:
    search = request.args.get("q", "").strip()
    status_filter = request.args.get("status", "all")
    enabled_filter = request.args.get("enabled", "all")
    alerts = list_alerts(search, status_filter, enabled_filter)
    return render_template(
        "alert_list.html",
        alerts=alerts,
        now=datetime.now(TIMEZONE),
        filters={
            "q": search,
            "status": status_filter,
            "enabled": enabled_filter,
        },
        counts=alert_counts(),
        active_page="list",
    )


@app.route("/alerts", methods=["POST"])
def create_alert() -> Any:
    if IS_VERCEL:
        flash("Vercel上では登録データ保存と時刻通知は安定動作しません。画面確認用モードです。", "error")
        return redirect(url_for("new_alert"))

    form = request.form

    try:
        item_name = form.get("item_name", "").strip()
        item_url = normalize_url(form.get("item_url", ""))
        bid_amount = form.get("bid_amount", "").strip()
        auction_end_at = parse_optional_datetime(form.get("auction_end_at", ""))
        notify_at = parse_notify_at(form.get("notify_at", ""))
        note = form.get("note", "").strip()
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("new_alert"))

    enabled = form.get("enabled") == "on"
    status = "pending" if enabled else "disabled"
    created_at = datetime.now(TIMEZONE).isoformat()
    db = get_db()
    cursor = db.execute(
        """
        INSERT INTO alerts (
            item_name, item_url, bid_amount, auction_end_at, notify_at, email_to, note, enabled, status, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            item_name,
            item_url,
            bid_amount,
            auction_end_at,
            notify_at.isoformat(),
            "",
            note,
            int(enabled),
            status,
            created_at,
        ),
    )
    db.commit()

    if enabled:
        schedule_alert(cursor.lastrowid, notify_at)
        flash("通知を登録しました。指定時刻にDiscordへ送信されます。", "success")
    else:
        flash("通知を下書き保存しました。一覧画面でONにするとDiscord送信待ちになります。", "success")
    return redirect(url_for("alert_list"))


@app.route("/alerts/<int:alert_id>/toggle", methods=["POST"])
def toggle_alert(alert_id: int) -> Any:
    if IS_VERCEL:
        flash("Vercel上では通知切り替えは無効です。", "error")
        return redirect(url_for("alert_list"))

    alert = fetch_alert(alert_id)
    if alert is None:
        flash("通知が見つかりません。", "error")
        return redirect(url_for("alert_list"))

    if alert["status"] == "sent":
        flash("送信済みの通知は切り替えできません。", "error")
        return redirect(url_for("alert_list"))

    notify_at = datetime.fromisoformat(alert["notify_at"]).astimezone(TIMEZONE)
    db = get_db()

    if alert["enabled"]:
        db.execute(
            """
            UPDATE alerts
            SET enabled = 0, status = 'disabled'
            WHERE id = ?
            """,
            (alert_id,),
        )
        db.commit()
        unschedule_alert(alert_id)
        flash("自動送信をOFFにしました。", "success")
        return redirect(url_for("alert_list"))

    if notify_at <= datetime.now(TIMEZONE):
        flash("通知時刻が過ぎているため再開できません。削除して再登録してください。", "error")
        return redirect(url_for("alert_list"))

    db.execute(
        """
        UPDATE alerts
        SET enabled = 1, status = 'pending', error_message = NULL
        WHERE id = ?
        """,
        (alert_id,),
    )
    db.commit()
    schedule_alert(alert_id, notify_at)
    flash("自動送信をONにしました。", "success")
    return redirect(url_for("alert_list"))


@app.route("/discord/test", methods=["POST"])
def send_discord_test() -> Any:
    redirect_to = request.form.get("redirect_to") or url_for("new_alert")

    try:
        send_discord_message(
            "\n".join(
                [
                    "Discord通知のテスト送信です。",
                    "",
                    f"送信時刻: {datetime.now(TIMEZONE).strftime('%Y-%m-%d %H:%M:%S')} ({TIMEZONE.key})",
                    "このメッセージが見えていれば Webhook 設定は有効です。",
                ]
            )
        )
    except Exception as exc:
        flash(f"テスト送信に失敗しました: {exc}", "error")
        return redirect(redirect_to)

    flash("Discordへテスト通知を送信しました。", "success")
    return redirect(redirect_to)


@app.route("/alerts/<int:alert_id>/delete", methods=["POST"])
def delete_alert(alert_id: int) -> Any:
    if IS_VERCEL:
        flash("Vercel上では削除操作は無効です。", "error")
        return redirect(url_for("alert_list"))

    db = get_db()
    db.execute("DELETE FROM alerts WHERE id = ?", (alert_id,))
    db.commit()
    unschedule_alert(alert_id)
    flash("通知を削除しました。", "success")
    return redirect(url_for("alert_list"))


def bootstrap() -> None:
    init_db()
    if IS_VERCEL:
        return
    if not scheduler.running:
        scheduler.start()
    reschedule_pending_alerts()


bootstrap()
atexit.register(lambda: scheduler.shutdown(wait=False) if scheduler.running else None)


if __name__ == "__main__":
    app.run(debug=True, port=int(os.getenv("PORT", "5001")))
