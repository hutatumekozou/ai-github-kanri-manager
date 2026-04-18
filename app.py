from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from flask import Flask, flash, g, redirect, render_template, request, url_for

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv() -> bool:
        return False

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


def adapt_query(query: str) -> str:
    return query.replace("?", "%s") if USE_POSTGRES else query


def open_db_connection() -> Any:
    if USE_POSTGRES:
        if psycopg is None:
            raise RuntimeError("Postgresを使うには psycopg のインストールが必要です。")
        return psycopg.connect(DATABASE_URL, row_factory=dict_row)

    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
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
                CREATE TABLE IF NOT EXISTS works (
                    id BIGSERIAL PRIMARY KEY,
                    title TEXT NOT NULL,
                    github_url TEXT NOT NULL,
                    local_site_url TEXT,
                    vercel_site_url TEXT,
                    note TEXT NOT NULL DEFAULT '',
                    display_order BIGINT NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                )
                """,
            )
            db_execute(
                connection,
                "ALTER TABLE works ADD COLUMN IF NOT EXISTS display_order BIGINT NOT NULL DEFAULT 0",
            )
            db_execute(
                connection,
                "ALTER TABLE works ADD COLUMN IF NOT EXISTS local_site_url TEXT",
            )
            db_execute(
                connection,
                "ALTER TABLE works ADD COLUMN IF NOT EXISTS vercel_site_url TEXT",
            )
            db_execute(
                connection,
                """
                CREATE TABLE IF NOT EXISTS work_updates (
                    id BIGSERIAL PRIMARY KEY,
                    work_id BIGINT NOT NULL REFERENCES works(id) ON DELETE CASCADE,
                    saved_at TEXT NOT NULL,
                    update_github_url TEXT,
                    update_content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """,
            )
            db_execute(
                connection,
                "ALTER TABLE work_updates ADD COLUMN IF NOT EXISTS update_github_url TEXT",
            )
        else:
            db_execute(
                connection,
                """
                CREATE TABLE IF NOT EXISTS works (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    github_url TEXT NOT NULL,
                    local_site_url TEXT,
                    vercel_site_url TEXT,
                    note TEXT NOT NULL DEFAULT '',
                    display_order INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                )
                """,
            )
            work_columns = {
                row[1]
                for row in db_execute(connection, "PRAGMA table_info(works)").fetchall()
            }
            if "display_order" not in work_columns:
                db_execute(
                    connection,
                    "ALTER TABLE works ADD COLUMN display_order INTEGER NOT NULL DEFAULT 0",
                )
            if "local_site_url" not in work_columns:
                db_execute(
                    connection,
                    "ALTER TABLE works ADD COLUMN local_site_url TEXT",
                )
            if "vercel_site_url" not in work_columns:
                db_execute(
                    connection,
                    "ALTER TABLE works ADD COLUMN vercel_site_url TEXT",
                )
            db_execute(
                connection,
                """
                CREATE TABLE IF NOT EXISTS work_updates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    work_id INTEGER NOT NULL,
                    saved_at TEXT NOT NULL,
                    update_github_url TEXT,
                    update_content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (work_id) REFERENCES works(id) ON DELETE CASCADE
                )
                """,
            )
            columns = {
                row[1]
                for row in db_execute(connection, "PRAGMA table_info(work_updates)").fetchall()
            }
            if "update_github_url" not in columns:
                db_execute(
                    connection,
                    "ALTER TABLE work_updates ADD COLUMN update_github_url TEXT",
                )
        db_execute(
            connection,
            """
            UPDATE work_updates
            SET update_github_url = (
                SELECT github_url
                FROM works
                WHERE works.id = work_updates.work_id
            )
            WHERE update_github_url IS NULL
              AND id IN (
                SELECT MIN(id)
                FROM work_updates
                GROUP BY work_id
                )
            """,
        )
        ordered_work_ids = [
            row["id"]
            for row in db_execute(
                connection,
                """
                SELECT
                    w.id
                FROM works w
                ORDER BY COALESCE(
                    (
                        SELECT wu.saved_at
                        FROM work_updates wu
                        WHERE wu.work_id = w.id
                        ORDER BY wu.saved_at DESC, wu.id DESC
                        LIMIT 1
                    ),
                    w.created_at
                ) DESC,
                w.id DESC
                """,
            ).fetchall()
        ]
        for index, work_id in enumerate(ordered_work_ids, start=1):
            db_execute(
                connection,
                """
                UPDATE works
                SET display_order = ?
                WHERE id = ?
                  AND (display_order IS NULL OR display_order = 0)
                """,
                (index, work_id),
            )
        connection.commit()
    finally:
        connection.close()


def normalize_url(raw_url: str, label: str = "URL") -> str:
    parsed = urlparse(raw_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{label}は http または https で始まる正しいURLを指定してください。")
    return raw_url.strip()


def normalize_optional_url(raw_url: str, label: str = "URL") -> str | None:
    value = raw_url.strip()
    if not value:
        return None
    return normalize_url(value, label)


def parse_saved_at(raw_saved_at: str) -> datetime:
    try:
        saved_at = datetime.strptime(raw_saved_at, "%Y-%m-%dT%H:%M")
    except ValueError as exc:
        raise ValueError("最終保存日の形式が正しくありません。") from exc
    return saved_at.replace(tzinfo=TIMEZONE)


def format_datetime(value: str | None) -> str:
    if not value:
        return "-"
    return datetime.fromisoformat(value).astimezone(TIMEZONE).strftime("%Y-%m-%d %H:%M")


app.jinja_env.filters["datetime_jst"] = format_datetime


def list_works(search: str = "") -> list[Any]:
    db = get_db()
    conditions = []
    params: list[Any] = []

    if search:
        like = f"%{search}%"
        conditions.append(
            """
            (
                w.title LIKE ?
                OR w.github_url LIKE ?
                OR COALESCE(w.local_site_url, '') LIKE ?
                OR COALESCE(w.vercel_site_url, '') LIKE ?
                OR w.note LIKE ?
                OR EXISTS (
                    SELECT 1
                    FROM work_updates wu
                    WHERE wu.work_id = w.id
                      AND (
                        wu.update_content LIKE ?
                        OR COALESCE(wu.update_github_url, '') LIKE ?
                      )
                )
            )
            """
        )
        params.extend([like, like, like, like, like, like, like])

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    query = f"""
        SELECT
            w.id,
            w.title,
            w.github_url,
            w.note,
            w.display_order,
            w.created_at,
            (
                SELECT wu.saved_at
                FROM work_updates wu
                WHERE wu.work_id = w.id
                ORDER BY wu.saved_at DESC, wu.id DESC
                LIMIT 1
            ) AS last_saved_at,
            (
                SELECT wu.update_content
                FROM work_updates wu
                WHERE wu.work_id = w.id
                ORDER BY wu.saved_at DESC, wu.id DESC
                LIMIT 1
            ) AS latest_update_content,
            (
                SELECT COUNT(*)
                FROM work_updates wu
                WHERE wu.work_id = w.id
            ) AS update_count,
            w.local_site_url,
            w.vercel_site_url
        FROM works w
        {where_clause}
        ORDER BY w.display_order ASC, w.id DESC
    """
    return db_execute(db, query, tuple(params)).fetchall()


def work_summary() -> dict[str, int]:
    db = get_db()
    row = db_execute(
        db,
        """
        SELECT
            (SELECT COUNT(*) FROM works) AS work_count,
            (SELECT COUNT(*) FROM work_updates) AS update_count
        """,
    ).fetchone()
    return {
        "works": row["work_count"] or 0,
        "updates": row["update_count"] or 0,
    }


def fetch_work(work_id: int) -> Optional[Any]:
    db = get_db()
    return db_execute(
        db,
        """
        SELECT
            w.id,
            w.title,
            w.github_url,
            w.local_site_url,
            w.vercel_site_url,
            w.note,
            w.display_order,
            w.created_at,
            (
                SELECT wu.saved_at
                FROM work_updates wu
                WHERE wu.work_id = w.id
                ORDER BY wu.saved_at DESC, wu.id DESC
                LIMIT 1
            ) AS last_saved_at,
            (
                SELECT COUNT(*)
                FROM work_updates wu
                WHERE wu.work_id = w.id
            ) AS update_count
        FROM works w
        WHERE w.id = ?
        """,
        (work_id,),
    ).fetchone()


def fetch_work_updates(work_id: int) -> list[Any]:
    db = get_db()
    return db_execute(
        db,
        """
        SELECT id, saved_at, update_github_url, update_content, created_at
        FROM work_updates
        WHERE work_id = ?
        ORDER BY saved_at DESC, id DESC
        """,
        (work_id,),
    ).fetchall()


def app_runtime_status() -> dict[str, Any]:
    return {
        "is_vercel": IS_VERCEL,
        "use_postgres": USE_POSTGRES,
        "has_persistent_storage": USE_POSTGRES or not IS_VERCEL,
    }


def next_work_display_order() -> int:
    db = get_db()
    row = db_execute(db, "SELECT COALESCE(MAX(display_order), 0) AS max_display_order FROM works").fetchone()
    return int(row["max_display_order"] or 0) + 1


@app.context_processor
def inject_runtime_status() -> dict[str, Any]:
    return {
        "app_env": app_runtime_status(),
    }


@app.route("/", methods=["GET"])
def index() -> Any:
    return redirect(url_for("work_list"))


@app.route("/works", methods=["GET"])
def work_list() -> str:
    search = request.args.get("q", "").strip()
    works = list_works(search)
    return render_template(
        "work_list.html",
        works=works,
        filters={"q": search},
        counts=work_summary(),
        active_page="list",
    )


@app.route("/works/new", methods=["GET"])
def new_work() -> str:
    now = datetime.now(TIMEZONE)
    return render_template(
        "new_work.html",
        now=now,
        default_saved_at=now.strftime("%Y-%m-%dT%H:%M"),
        active_page="new",
    )


@app.route("/works", methods=["POST"])
def create_work() -> Any:
    if not app_runtime_status()["has_persistent_storage"]:
        flash("この環境では永続DBが未設定です。DATABASE_URL を設定してください。", "error")
        return redirect(url_for("new_work"))

    form = request.form

    try:
        title = form.get("title", "").strip()
        github_url = normalize_url(form.get("github_url", ""), "GitHubリンク")
        local_site_url = normalize_optional_url(form.get("local_site_url", ""), "ローカルサイト")
        vercel_site_url = normalize_optional_url(form.get("vercel_site_url", ""), "Vercelサイト")
        saved_at = parse_saved_at(form.get("saved_at", ""))
        update_content = form.get("update_content", "").strip()
        note = form.get("note", "").strip()
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("new_work"))

    if not title:
        flash("作品名を入力してください。", "error")
        return redirect(url_for("new_work"))

    if not update_content:
        flash("更新内容を入力してください。", "error")
        return redirect(url_for("new_work"))

    timestamp = datetime.now(TIMEZONE).isoformat()
    db = get_db()

    if USE_POSTGRES:
        cursor = db_execute(
            db,
            """
            INSERT INTO works (title, github_url, local_site_url, vercel_site_url, note, display_order, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            (
                title,
                github_url,
                local_site_url,
                vercel_site_url,
                note,
                next_work_display_order(),
                timestamp,
            ),
        )
        work_id = cursor.fetchone()["id"]
    else:
        cursor = db_execute(
            db,
            """
            INSERT INTO works (title, github_url, local_site_url, vercel_site_url, note, display_order, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                title,
                github_url,
                local_site_url,
                vercel_site_url,
                note,
                next_work_display_order(),
                timestamp,
            ),
        )
        work_id = cursor.lastrowid

    db_execute(
        db,
        """
        INSERT INTO work_updates (work_id, saved_at, update_github_url, update_content, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (work_id, saved_at.isoformat(), github_url, update_content, timestamp),
    )
    db.commit()

    flash("作品を登録しました。", "success")
    return redirect(url_for("work_detail", work_id=work_id))


@app.route("/works/<int:work_id>", methods=["GET"])
def work_detail(work_id: int) -> Any:
    work = fetch_work(work_id)
    if work is None:
        flash("作品が見つかりません。", "error")
        return redirect(url_for("work_list"))

    updates = fetch_work_updates(work_id)
    now = datetime.now(TIMEZONE)
    return render_template(
        "work_detail.html",
        work=work,
        updates=updates,
        now=now,
        default_saved_at=now.strftime("%Y-%m-%dT%H:%M"),
        active_page="list",
    )


@app.route("/works/<int:work_id>/updates", methods=["POST"])
def create_work_update(work_id: int) -> Any:
    if not app_runtime_status()["has_persistent_storage"]:
        flash("この環境では永続DBが未設定です。DATABASE_URL を設定してください。", "error")
        return redirect(url_for("work_detail", work_id=work_id))

    work = fetch_work(work_id)
    if work is None:
        flash("作品が見つかりません。", "error")
        return redirect(url_for("work_list"))

    try:
        saved_at = parse_saved_at(request.form.get("saved_at", ""))
        update_github_url = normalize_optional_url(
            request.form.get("update_github_url", ""),
            "別のGitHubリンク",
        )
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("work_detail", work_id=work_id))

    update_content = request.form.get("update_content", "").strip()
    if not update_content:
        flash("更新内容を入力してください。", "error")
        return redirect(url_for("work_detail", work_id=work_id))

    db = get_db()
    db_execute(
        db,
        """
        INSERT INTO work_updates (work_id, saved_at, update_github_url, update_content, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            work_id,
            saved_at.isoformat(),
            update_github_url,
            update_content,
            datetime.now(TIMEZONE).isoformat(),
        ),
    )
    db.commit()

    flash("更新履歴を追加しました。", "success")
    return redirect(url_for("work_detail", work_id=work_id))


@app.route("/works/<int:work_id>/features", methods=["POST"])
def update_work_features(work_id: int) -> Any:
    if not app_runtime_status()["has_persistent_storage"]:
        flash("この環境では永続DBが未設定です。DATABASE_URL を設定してください。", "error")
        return redirect(url_for("work_detail", work_id=work_id))

    work = fetch_work(work_id)
    if work is None:
        flash("作品が見つかりません。", "error")
        return redirect(url_for("work_list"))

    features = request.form.get("note", "").strip()
    db = get_db()
    db_execute(
        db,
        """
        UPDATE works
        SET note = ?
        WHERE id = ?
        """,
        (features, work_id),
    )
    db.commit()

    flash("このアプリの特徴を更新しました。", "success")
    return redirect(url_for("work_detail", work_id=work_id))


@app.route("/works/<int:work_id>/sites", methods=["POST"])
def update_work_sites(work_id: int) -> Any:
    if not app_runtime_status()["has_persistent_storage"]:
        flash("この環境では永続DBが未設定です。DATABASE_URL を設定してください。", "error")
        return redirect(url_for("work_detail", work_id=work_id))

    work = fetch_work(work_id)
    if work is None:
        flash("作品が見つかりません。", "error")
        return redirect(url_for("work_list"))

    try:
        local_site_url = normalize_optional_url(request.form.get("local_site_url", ""), "ローカルサイト")
        vercel_site_url = normalize_optional_url(request.form.get("vercel_site_url", ""), "Vercelサイト")
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("work_detail", work_id=work_id, edit="sites"))

    db = get_db()
    db_execute(
        db,
        """
        UPDATE works
        SET local_site_url = ?, vercel_site_url = ?
        WHERE id = ?
        """,
        (local_site_url, vercel_site_url, work_id),
    )
    db.commit()

    flash("サイトURLを更新しました。", "success")
    return redirect(url_for("work_detail", work_id=work_id))


@app.route("/works/<int:work_id>/sites/<site_type>", methods=["POST"])
def update_work_site(work_id: int, site_type: str) -> Any:
    if not app_runtime_status()["has_persistent_storage"]:
        flash("この環境では永続DBが未設定です。DATABASE_URL を設定してください。", "error")
        return redirect(url_for("work_detail", work_id=work_id))

    work = fetch_work(work_id)
    if work is None:
        flash("作品が見つかりません。", "error")
        return redirect(url_for("work_list"))

    field_map = {
        "local": ("local_site_url", "ローカルサイト"),
        "vercel": ("vercel_site_url", "Vercelサイト"),
    }
    target = field_map.get(site_type)
    if target is None:
        flash("更新対象のサイト種別が不正です。", "error")
        return redirect(url_for("work_detail", work_id=work_id))

    field_name, label = target

    try:
        site_url = normalize_optional_url(request.form.get(field_name, ""), label)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("work_detail", work_id=work_id, edit=site_type))

    db = get_db()
    db_execute(
        db,
        f"""
        UPDATE works
        SET {field_name} = ?
        WHERE id = ?
        """,
        (site_url, work_id),
    )
    db.commit()

    flash(f"{label}を更新しました。", "success")
    return redirect(url_for("work_detail", work_id=work_id))


@app.route("/works/reorder", methods=["POST"])
def reorder_works() -> Any:
    if not app_runtime_status()["has_persistent_storage"]:
        flash("この環境では永続DBが未設定です。DATABASE_URL を設定してください。", "error")
        return redirect(url_for("work_list"))

    raw_order = request.form.get("work_order", "").strip()
    if not raw_order:
        flash("並び順データが見つかりません。", "error")
        return redirect(url_for("work_list"))

    try:
        ordered_ids = [int(value) for value in raw_order.split(",") if value.strip()]
    except ValueError:
        flash("並び順データが不正です。", "error")
        return redirect(url_for("work_list"))

    db = get_db()
    existing_ids = {
        row["id"]
        for row in db_execute(db, "SELECT id FROM works").fetchall()
    }
    if set(ordered_ids) != existing_ids or len(ordered_ids) != len(existing_ids):
        flash("並び順の保存に失敗しました。ページを再読み込みして再度お試しください。", "error")
        return redirect(url_for("work_list"))

    for index, work_id in enumerate(ordered_ids, start=1):
        db_execute(
            db,
            """
            UPDATE works
            SET display_order = ?
            WHERE id = ?
            """,
            (index, work_id),
        )
    db.commit()

    flash("作品の順番を更新しました。", "success")
    return redirect(url_for("work_list"))


@app.route("/works/<int:work_id>/delete", methods=["POST"])
def delete_work(work_id: int) -> Any:
    if not app_runtime_status()["has_persistent_storage"]:
        flash("この環境では永続DBが未設定です。DATABASE_URL を設定してください。", "error")
        return redirect(url_for("work_list"))

    db = get_db()
    db_execute(db, "DELETE FROM works WHERE id = ?", (work_id,))
    db.commit()
    flash("作品を削除しました。", "success")
    return redirect(url_for("work_list"))


def bootstrap() -> None:
    init_db()


bootstrap()


if __name__ == "__main__":
    app.run(debug=True, port=int(os.getenv("PORT", "5001")))
