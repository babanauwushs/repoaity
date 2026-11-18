from contextlib import closing
import hmac
from pathlib import Path
import re
import secrets
import sqlite3

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)

news_bp = Blueprint("news", __name__, url_prefix="/news")
MAX_NEWS_IMAGE_SIZE = 5 * 1024 * 1024
NEWS_IMAGE_TYPES = {
    ".jpg": lambda data: data.startswith(b"\xff\xd8\xff"),
    ".jpeg": lambda data: data.startswith(b"\xff\xd8\xff"),
    ".png": lambda data: data.startswith(b"\x89PNG\r\n\x1a\n"),
    ".webp": lambda data: (
        data.startswith(b"RIFF")
        and len(data) >= 12
        and data[8:12] == b"WEBP"
    ),
}


def init_news_database(app):
    app.config.setdefault("MAX_CONTENT_LENGTH", 6 * 1024 * 1024)
    app.config.setdefault(
        "NEWS_UPLOAD_FOLDER",
        str(Path(app.instance_path) / "news_uploads"),
    )
    database_path = Path(app.config["DATABASE"])
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database_path)) as connection, connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS news (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                body TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'news',
                is_published INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                published_at TEXT,
                image_filename TEXT
            )
            """
        )
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(news)").fetchall()
        }
        if "category" not in columns:
            connection.execute(
                "ALTER TABLE news ADD COLUMN category TEXT NOT NULL DEFAULT 'news'"
            )
        if "is_published" not in columns:
            connection.execute(
                "ALTER TABLE news ADD COLUMN is_published INTEGER NOT NULL DEFAULT 1"
            )
        if "published_at" not in columns:
            connection.execute("ALTER TABLE news ADD COLUMN published_at TEXT")
            connection.execute(
                "UPDATE news SET published_at = created_at WHERE is_published = 1"
            )
        if "image_filename" not in columns:
            connection.execute("ALTER TABLE news ADD COLUMN image_filename TEXT")


def get_news_db():
    if "news_db" not in g:
        connection = sqlite3.connect(current_app.config["DATABASE"])
        connection.row_factory = sqlite3.Row
        g.news_db = connection
    return g.news_db


def create_news_notifications(database, news_id):
    database.execute(
        """
        INSERT OR IGNORE INTO user_notifications (user_id, news_id)
        SELECT id, ? FROM users
        """,
        (news_id,),
    )


@news_bp.teardown_app_request
def close_news_db(_error=None):
    connection = g.pop("news_db", None)
    if connection is not None:
        connection.close()


def get_latest_news(limit=3):
    return get_news_db().execute(
        "SELECT id, title, summary, category, created_at, image_filename FROM news "
        "WHERE is_published = 1 "
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        (limit,),
    ).fetchall()


def require_admin():
    if g.get("current_user") is None:
        return redirect(url_for("admin_login", next=request.path))
    if g.current_user["role"] != "admin":
        abort(403)
    return None


def validate_news_csrf():
    submitted_token = request.form.get("csrf_token", "")
    session_token = session.get("csrf_token", "")
    if not session_token or not hmac.compare_digest(
        submitted_token.encode("utf-8"),
        session_token.encode("utf-8"),
    ):
        abort(400, description="Хүсэлтийн баталгаажуулалт амжилтгүй боллоо.")


def validate_news_form():
    title = request.form.get("title", "").strip()
    summary = request.form.get("summary", "").strip()
    body = request.form.get("body", "").strip()
    category = request.form.get("category", "news")
    is_published = request.form.get("is_published") == "1"
    if not title or len(title) > 150:
        flash("Гарчиг 1–150 тэмдэгт байх ёстой.", "error")
    elif not summary or len(summary) > 300:
        flash("Товч агуулга 1–300 тэмдэгт байх ёстой.", "error")
    elif not body or len(body) > 20000:
        flash("Мэдээний агуулга 1–20000 тэмдэгт байх ёстой.", "error")
    elif category not in {"news", "announcement"}:
        flash("Мэдээний төрөл буруу байна.", "error")
    else:
        return {
            "title": title,
            "summary": summary,
            "body": body,
            "category": category,
            "is_published": is_published,
        }
    return None


def save_news_image():
    upload = request.files.get("image")
    if upload is None or not upload.filename:
        return None, True

    extension = Path(upload.filename).suffix.lower()
    signature_check = NEWS_IMAGE_TYPES.get(extension)
    if signature_check is None:
        flash("Зөвхөн JPG, PNG эсвэл WEBP зураг оруулна уу.", "error")
        return None, False

    image_data = upload.stream.read(MAX_NEWS_IMAGE_SIZE + 1)
    if len(image_data) > MAX_NEWS_IMAGE_SIZE:
        flash("Зургийн хэмжээ 5 МБ-аас бага байх ёстой.", "error")
        return None, False
    if not image_data or not signature_check(image_data):
        flash("Сонгосон файл хүчинтэй зураг биш байна.", "error")
        return None, False

    filename = f"{secrets.token_hex(16)}{extension}"
    upload_directory = Path(current_app.config["NEWS_UPLOAD_FOLDER"])
    upload_directory.mkdir(parents=True, exist_ok=True)
    (upload_directory / filename).write_bytes(image_data)
    return filename, True


def remove_news_image(filename):
    if filename and re.fullmatch(r"[a-f0-9]{32}\.(jpg|jpeg|png|webp)", filename):
        image_path = Path(current_app.config["NEWS_UPLOAD_FOLDER"]) / filename
        if image_path.exists():
            image_path.unlink()


@news_bp.route("/media/<filename>")
def media(filename):
    if not re.fullmatch(r"[a-f0-9]{32}\.(jpg|jpeg|png|webp)", filename):
        abort(404)
    article = get_news_db().execute(
        "SELECT is_published FROM news WHERE image_filename = ?",
        (filename,),
    ).fetchone()
    is_admin = g.get("current_user") is not None and g.current_user["role"] == "admin"
    if article is None or (not article["is_published"] and not is_admin):
        abort(404)
    return send_from_directory(
        current_app.config["NEWS_UPLOAD_FOLDER"],
        filename,
        max_age=3600,
    )


@news_bp.route("/", endpoint="index")
@news_bp.route("", endpoint="list_news")
def index():
    articles = get_news_db().execute(
        "SELECT id, title, summary, category, created_at, image_filename FROM news "
        "WHERE is_published = 1 "
        "ORDER BY created_at DESC, id DESC"
    ).fetchall()
    return render_template("news_index.html", articles=articles)


@news_bp.route("/<int:news_id>")
def detail(news_id):
    article = get_news_db().execute(
        """
        SELECT id, title, summary, body, category, is_published, created_at, image_filename
        FROM news
        WHERE id = ? AND (is_published = 1 OR ? = 1)
        """,
        (
            news_id,
            int(g.get("current_user") is not None and g.current_user["role"] == "admin"),
        ),
    ).fetchone()
    if article is None:
        abort(404)
    return render_template("news_detail.html", article=article)


@news_bp.route("/manage")
def manage():
    denied = require_admin()
    if denied is not None:
        return denied
    articles = get_news_db().execute(
        "SELECT id, title, category, is_published, created_at, updated_at FROM news "
        "ORDER BY created_at DESC, id DESC"
    ).fetchall()
    return render_template("news_manage.html", articles=articles)


@news_bp.route("/create", methods=["GET", "POST"])
def create():
    denied = require_admin()
    if denied is not None:
        return denied
    if request.method == "POST":
        validate_news_csrf()
        article = validate_news_form()
        if article is not None:
            image_filename, image_valid = save_news_image()
            if not image_valid:
                article = None
        if article is not None:
            database = get_news_db()
            cursor = database.execute(
                """
                INSERT INTO news (
                    title, summary, body, category, is_published, published_at, image_filename
                )
                VALUES (?, ?, ?, ?, ?, CASE WHEN ? = 1 THEN CURRENT_TIMESTAMP END, ?)
                """,
                (
                    article["title"],
                    article["summary"],
                    article["body"],
                    article["category"],
                    int(article["is_published"]),
                    int(article["is_published"]),
                    image_filename,
                ),
            )
            if article["is_published"]:
                create_news_notifications(database, cursor.lastrowid)
            database.commit()
            flash(
                "Мэдээ нийтлэгдлээ." if article["is_published"] else "Ноорог хадгалагдлаа.",
                "success",
            )
            return redirect(url_for("news.manage"))
    else:
        article = {
            "title": "",
            "summary": "",
            "body": "",
            "category": "news",
            "is_published": False,
            "image_filename": None,
        }
    if article is None:
        article = {
            "title": request.form.get("title", ""),
            "summary": request.form.get("summary", ""),
            "body": request.form.get("body", ""),
            "category": request.form.get("category", "news"),
            "is_published": request.form.get("is_published") == "1",
            "image_filename": None,
        }
    return render_template("news_form.html", article=article, page_title="Мэдээ нэмэх")


@news_bp.route("/<int:news_id>/edit", methods=["GET", "POST"])
def edit(news_id):
    denied = require_admin()
    if denied is not None:
        return denied
    database = get_news_db()
    existing_article = database.execute(
        """
        SELECT id, title, summary, body, category, is_published, image_filename
        FROM news WHERE id = ?
        """,
        (news_id,),
    ).fetchone()
    if existing_article is None:
        abort(404)

    if request.method == "POST":
        validate_news_csrf()
        article = validate_news_form()
        if article is not None:
            image_filename, image_valid = save_news_image()
            if not image_valid:
                article = None
        if article is not None:
            old_image_filename = existing_article["image_filename"]
            was_published = bool(existing_article["is_published"])
            if image_filename is None:
                image_filename = (
                    None
                    if request.form.get("remove_image") == "1"
                    else old_image_filename
                )
            database.execute(
                """
                UPDATE news
                SET title = ?, summary = ?, body = ?, category = ?, is_published = ?,
                    image_filename = ?,
                    published_at = CASE
                        WHEN ? = 1 AND published_at IS NULL THEN CURRENT_TIMESTAMP
                        WHEN ? = 0 THEN NULL
                        ELSE published_at
                    END,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    article["title"],
                    article["summary"],
                    article["body"],
                    article["category"],
                    int(article["is_published"]),
                    image_filename,
                    int(article["is_published"]),
                    int(article["is_published"]),
                    news_id,
                ),
            )
            if article["is_published"] and not was_published:
                create_news_notifications(database, news_id)
            elif was_published and not article["is_published"]:
                database.execute(
                    "DELETE FROM user_notifications WHERE news_id = ?",
                    (news_id,),
                )
            database.commit()
            if old_image_filename != image_filename:
                remove_news_image(old_image_filename)
            flash(
                "Мэдээ шинэчлэгдлээ."
                if article["is_published"]
                else "Ноорог хадгалагдлаа.",
                "success",
            )
            return redirect(url_for("news.manage"))
    else:
        article = dict(existing_article)
    if article is None:
        article = dict(existing_article)
        article.update(
            title=request.form.get("title", ""),
            summary=request.form.get("summary", ""),
            body=request.form.get("body", ""),
            category=request.form.get("category", "news"),
            is_published=request.form.get("is_published") == "1",
        )
    return render_template(
        "news_form.html",
        article=article,
        page_title="Мэдээ засах",
    )


@news_bp.route("/<int:news_id>/status", methods=["POST"])
def update_status(news_id):
    denied = require_admin()
    if denied is not None:
        return denied
    validate_news_csrf()
    is_published = request.form.get("is_published")
    if is_published not in {"0", "1"}:
        abort(400, description="Нийтлэлийн төлөв буруу байна.")

    database = get_news_db()
    existing_article = database.execute(
        "SELECT is_published FROM news WHERE id = ?",
        (news_id,),
    ).fetchone()
    if existing_article is None:
        abort(404)
    was_published = bool(existing_article["is_published"])
    cursor = database.execute(
        """
        UPDATE news
        SET is_published = ?,
            published_at = CASE
                WHEN ? = 1 AND published_at IS NULL THEN CURRENT_TIMESTAMP
                WHEN ? = 0 THEN NULL
                ELSE published_at
            END,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (int(is_published), int(is_published), int(is_published), news_id),
    )
    if cursor.rowcount == 0:
        abort(404)
    if is_published == "1" and not was_published:
        create_news_notifications(database, news_id)
    elif was_published and is_published == "0":
        database.execute(
            "DELETE FROM user_notifications WHERE news_id = ?",
            (news_id,),
        )
    database.commit()
    flash(
        "Мэдээ нийтлэгдлээ." if is_published == "1" else "Ноорог болголоо.",
        "success",
    )
    return redirect(url_for("news.manage"))


@news_bp.route("/<int:news_id>/delete", methods=["POST"])
def delete(news_id):
    denied = require_admin()
    if denied is not None:
        return denied
    validate_news_csrf()
    database = get_news_db()
    article = database.execute(
        "SELECT image_filename FROM news WHERE id = ?",
        (news_id,),
    ).fetchone()
    if article is None:
        abort(404)
    database.execute(
        "DELETE FROM user_notifications WHERE news_id = ?",
        (news_id,),
    )
    cursor = database.execute("DELETE FROM news WHERE id = ?", (news_id,))
    if cursor.rowcount == 0:
        abort(404)
    database.commit()
    remove_news_image(article["image_filename"])
    flash("Мэдээ устгагдлаа.", "success")
    return redirect(url_for("news.manage"))
