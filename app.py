from __future__ import annotations

import atexit
import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from flask import Flask, flash, g, redirect, render_template, request, url_for

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:
    psycopg = None
    dict_row = None

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
IS_VERCEL = bool(os.getenv("VERCEL"))
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
USE_POSTGRES = bool(DATABASE_URL)
DATABASE_PATH = Path("/tmp/app.db") if IS_VERCEL and not USE_POSTGRES else BASE_DIR / "app.db"
TIMEZONE = ZoneInfo(os.getenv("APP_TIMEZONE", "Asia/Tokyo"))

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "dev-secret-key")

scheduler = BackgroundScheduler(timezone=str(TIMEZONE))


def adapt_query(query: str) -> str:
    return query.replace("?", "%s") if USE_POSTGRES else query


def open_db_connection() -> Any:
    if USE_POSTGRES:
        if psycopg is None:
            raise RuntimeError("Postgresを使うには psycopg のインストールが必要です。")
        return psycopg.connect(DATABASE_URL, row_factory=dict_row)

    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def db_execute(connection: Any, query: str, params: tuple[Any, ...] = ()) -> Any:
    return connection.execute(adapt_query(query), params)


def get_db() -> Any:
    if "db" not in g:
        g.db = open_db_connection()
    return g.db


@app.teardown_appcontext
def close_db(_: Optional[BaseException]) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    connection = open_db_connection()
    try:
        if USE_POSTGRES:
            db_execute(
                connection,
                """
                CREATE TABLE IF NOT EXISTS alerts (
                    id BIGSERIAL PRIMARY KEY,
                    item_name TEXT NOT NULL DEFAULT '',
                    item_url TEXT NOT NULL,
                    bid_amount TEXT NOT NULL DEFAULT '',
                    auction_end_at TEXT,
                    notify_at TEXT NOT NULL,
                    email_to TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    sent_at TEXT,
                    error_message TEXT,
                    processing_started_at TEXT
                )
                """,
            )
            db_execute(
                connection,
                "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS item_name TEXT NOT NULL DEFAULT ''",
            )
            db_execute(
                connection,
                "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS bid_amount TEXT NOT NULL DEFAULT ''",
            )
            db_execute(
                connection,
                "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS auction_end_at TEXT",
            )
            db_execute(
                connection,
                "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS enabled INTEGER NOT NULL DEFAULT 1",
            )
            db_execute(
                connection,
                "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS email_to TEXT NOT NULL DEFAULT ''",
            )
            db_execute(
                connection,
                "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS processing_started_at TEXT",
            )
        else:
            db_execute(
                connection,
                """
                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_name TEXT NOT NULL DEFAULT '',
                    item_url TEXT NOT NULL,
                    bid_amount TEXT NOT NULL DEFAULT '',
                    auction_end_at TEXT,
                    notify_at TEXT NOT NULL,
                    email_to TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    sent_at TEXT,
                    error_message TEXT,
                    processing_started_at TEXT
                )
                """,
            )
            columns = {
                row[1]
                for row in db_execute(connection, "PRAGMA table_info(alerts)").fetchall()
            }
            if "item_name" not in columns:
                db_execute(
                    connection,
                    "ALTER TABLE alerts ADD COLUMN item_name TEXT NOT NULL DEFAULT ''",
                )
            if "bid_amount" not in columns:
                db_execute(
                    connection,
                    "ALTER TABLE alerts ADD COLUMN bid_amount TEXT NOT NULL DEFAULT ''",
                )
            if "auction_end_at" not in columns:
                db_execute(connection, "ALTER TABLE alerts ADD COLUMN auction_end_at TEXT")
            if "enabled" not in columns:
                db_execute(
                    connection,
                    "ALTER TABLE alerts ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1",
                )
            if "email_to" not in columns:
                db_execute(
                    connection,
                    "ALTER TABLE alerts ADD COLUMN email_to TEXT NOT NULL DEFAULT ''",
                )
            if "processing_started_at" not in columns:
                db_execute(
                    connection,
                    "ALTER TABLE alerts ADD COLUMN processing_started_at TEXT",
                )
        connection.commit()
    finally:
        connection.close()


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

    if status_filter in {"pending", "processing", "sent", "failed", "disabled"}:
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
    return db_execute(db, query, tuple(params)).fetchall()


def fetch_alert(alert_id: int) -> Optional[sqlite3.Row]:
    connection = open_db_connection()
    try:
        return db_execute(
            connection,
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
                error_message,
                processing_started_at
            FROM alerts
            WHERE id = ?
            """,
            (alert_id,),
        ).fetchone()
    finally:
        connection.close()


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
    row = db_execute(
        db,
        """
        SELECT
            COUNT(*) AS total_count,
            SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending_count,
            SUM(CASE WHEN status = 'processing' THEN 1 ELSE 0 END) AS processing_count,
            SUM(CASE WHEN status = 'sent' THEN 1 ELSE 0 END) AS sent_count,
            SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_count,
            SUM(CASE WHEN status = 'disabled' THEN 1 ELSE 0 END) AS disabled_count
        FROM alerts
        """,
    ).fetchone()
    return {
        "total": row["total_count"] or 0,
        "pending": row["pending_count"] or 0,
        "processing": row["processing_count"] or 0,
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


def app_runtime_status() -> dict[str, Any]:
    return {
        "is_vercel": IS_VERCEL,
        "use_postgres": USE_POSTGRES,
        "has_persistent_storage": USE_POSTGRES or not IS_VERCEL,
        "scheduler_mode": "github_actions_or_vercel_cron" if IS_VERCEL else "local_apscheduler",
        "cron_secret_configured": bool(os.getenv("CRON_SECRET", "").strip()),
    }


@app.context_processor
def inject_discord_status() -> dict[str, Any]:
    return {
        "discord": discord_status(),
        "app_env": app_runtime_status(),
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
    connection = open_db_connection()
    try:
        db_execute(
            connection,
            """
            UPDATE alerts
            SET status = 'sent', sent_at = ?, error_message = NULL, processing_started_at = NULL
            WHERE id = ?
            """,
            (timestamp, alert_id),
        )
        connection.commit()
    finally:
        connection.close()


def mark_alert_failed(alert_id: int, error_message: str) -> None:
    connection = open_db_connection()
    try:
        db_execute(
            connection,
            """
            UPDATE alerts
            SET status = 'failed', error_message = ?, processing_started_at = NULL
            WHERE id = ?
            """,
            (error_message[:500], alert_id),
        )
        connection.commit()
    finally:
        connection.close()


def fetch_and_claim_due_alerts(limit: int = 50) -> list[Any]:
    now = datetime.now(TIMEZONE)
    now_iso = now.isoformat()
    stale_cutoff_iso = (now - timedelta(minutes=15)).isoformat()
    connection = open_db_connection()

    try:
        rows = db_execute(
            connection,
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
                error_message,
                processing_started_at
            FROM alerts
            WHERE enabled = 1
              AND notify_at <= ?
              AND (
                status = 'pending'
                OR (
                  status = 'processing'
                  AND (
                    processing_started_at IS NULL
                    OR processing_started_at <= ?
                  )
                )
              )
            ORDER BY notify_at ASC, id ASC
            LIMIT ?
            """,
            (now_iso, stale_cutoff_iso, limit),
        ).fetchall()

        claimed_rows = []
        for row in rows:
            cursor = db_execute(
                connection,
                """
                UPDATE alerts
                SET status = 'processing', processing_started_at = ?, error_message = NULL
                WHERE id = ?
                  AND enabled = 1
                  AND (
                    status = 'pending'
                    OR (
                      status = 'processing'
                      AND (
                        processing_started_at IS NULL
                        OR processing_started_at <= ?
                      )
                    )
                  )
                """,
                (now_iso, row["id"], stale_cutoff_iso),
            )
            if cursor.rowcount == 1:
                claimed_rows.append(row)

        connection.commit()
        return claimed_rows
    finally:
        connection.close()


def process_due_alerts(limit: int = 50) -> dict[str, Any]:
    claimed_rows = fetch_and_claim_due_alerts(limit=limit)
    sent_count = 0
    failed_count = 0

    for alert in claimed_rows:
        try:
            send_discord_notification(alert)
        except Exception as exc:
            mark_alert_failed(alert["id"], str(exc))
            failed_count += 1
            continue

        mark_alert_sent(alert["id"])
        sent_count += 1

    return {
        "checked": len(claimed_rows),
        "sent": sent_count,
        "failed": failed_count,
    }


def is_authorized_cron_request() -> bool:
    secret = os.getenv("CRON_SECRET", "").strip()
    if not secret:
        return True
    return request.headers.get("Authorization") == f"Bearer {secret}"


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
    connection = open_db_connection()
    try:
        pending_alerts = db_execute(
            connection,
            """
            SELECT id, notify_at
            FROM alerts
            WHERE status = 'pending' AND enabled = 1
            """,
        ).fetchall()
    finally:
        connection.close()

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
    if not app_runtime_status()["has_persistent_storage"]:
        flash("この環境では永続DBが未設定です。DATABASE_URL を設定してください。", "error")
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
    params = (
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
        None,
    )
    if USE_POSTGRES:
        cursor = db_execute(
            db,
            """
            INSERT INTO alerts (
                item_name, item_url, bid_amount, auction_end_at, notify_at, email_to, note, enabled, status, created_at, processing_started_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            params,
        )
        alert_id = cursor.fetchone()["id"]
    else:
        cursor = db_execute(
            db,
            """
            INSERT INTO alerts (
                item_name, item_url, bid_amount, auction_end_at, notify_at, email_to, note, enabled, status, created_at, processing_started_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            params,
        )
        alert_id = cursor.lastrowid
    db.commit()

    if enabled and not IS_VERCEL:
        schedule_alert(alert_id, notify_at)
        flash("通知を登録しました。指定時刻にDiscordへ送信されます。", "success")
    elif enabled:
        flash("通知を登録しました。次回のCron実行で送信対象として処理されます。", "success")
    else:
        flash("通知を下書き保存しました。一覧画面でONにするとDiscord送信待ちになります。", "success")
    return redirect(url_for("alert_list"))


@app.route("/alerts/<int:alert_id>/toggle", methods=["POST"])
def toggle_alert(alert_id: int) -> Any:
    if not app_runtime_status()["has_persistent_storage"]:
        flash("この環境では永続DBが未設定です。DATABASE_URL を設定してください。", "error")
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
        db_execute(
            db,
            """
            UPDATE alerts
            SET enabled = 0, status = 'disabled', processing_started_at = NULL
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

    db_execute(
        db,
        """
        UPDATE alerts
        SET enabled = 1, status = 'pending', error_message = NULL, processing_started_at = NULL
        WHERE id = ?
        """,
        (alert_id,),
    )
    db.commit()
    if not IS_VERCEL:
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


@app.route("/api/cron", methods=["GET", "POST"])
def cron_dispatch() -> tuple[dict[str, Any], int]:
    if not is_authorized_cron_request():
        return {"ok": False, "error": "unauthorized"}, 401

    if not app_runtime_status()["has_persistent_storage"]:
        return {"ok": False, "error": "database_not_configured"}, 503

    result = process_due_alerts(limit=50)
    return {"ok": True, **result}, 200


@app.route("/alerts/<int:alert_id>/delete", methods=["POST"])
def delete_alert(alert_id: int) -> Any:
    if not app_runtime_status()["has_persistent_storage"]:
        flash("この環境では永続DBが未設定です。DATABASE_URL を設定してください。", "error")
        return redirect(url_for("alert_list"))

    db = get_db()
    db_execute(db, "DELETE FROM alerts WHERE id = ?", (alert_id,))
    db.commit()
    if not IS_VERCEL:
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
