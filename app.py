from contextlib import closing
from datetime import date, datetime, time, timedelta, timezone
from email.message import EmailMessage
from functools import wraps
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import smtplib
import ssl
import sqlite3
from urllib.parse import urlparse

from flask import Flask, abort, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from news_integration import get_latest_news, init_news_database, news_bp
from translations import translate

app = Flask(__name__)
APP_ENV = os.environ.get("APP_ENV", "development").strip().lower()
configured_secret_key = os.environ.get("FLASK_SECRET_KEY")
if APP_ENV == "production" and (
    not configured_secret_key or len(configured_secret_key) < 32
):
    raise RuntimeError(
        "Production requires a FLASK_SECRET_KEY of at least 32 characters."
    )

app.config.update(
    APP_ENV=APP_ENV,
    SITE_NAME="Ikh Zasag Polytechnic College",
    SECRET_KEY=configured_secret_key or secrets.token_hex(32),
    DATABASE=os.environ.get(
        "DATABASE_PATH",
        str(Path(app.instance_path) / "users.sqlite3"),
    ),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=(
        APP_ENV == "production" or os.environ.get("FLASK_COOKIE_SECURE") == "1"
    ),
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
    TEMPLATES_AUTO_RELOAD=True,
)

if not os.environ.get("FLASK_SECRET_KEY"):
    app.logger.warning(
        "FLASK_SECRET_KEY is unset; sessions will not survive restarts. "
        "Set it to a persistent random secret before deployment."
    )

app.register_blueprint(news_bp)
app.add_url_rule(
    "/admin/news",
    endpoint="admin_news",
    view_func=app.view_functions["news.manage"],
)


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.create_function(
            "unicode_casefold",
            1,
            lambda value: value.casefold() if isinstance(value, str) else "",
        )
    return g.db


def initialize_database():
    database_path = Path(app.config["DATABASE"])
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database_path)) as connection, connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                full_name TEXT NOT NULL DEFAULT '',
                email TEXT,
                phone TEXT,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'student',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(users)").fetchall()
        }
        if "full_name" not in columns:
            connection.execute(
                "ALTER TABLE users ADD COLUMN full_name TEXT NOT NULL DEFAULT ''"
            )
        if "email" not in columns:
            connection.execute("ALTER TABLE users ADD COLUMN email TEXT")
        if "phone" not in columns:
            connection.execute("ALTER TABLE users ADD COLUMN phone TEXT")
        if "role" not in columns:
            connection.execute(
                "ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'student'"
            )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS users_email_unique "
            "ON users(email COLLATE NOCASE) WHERE email IS NOT NULL"
        )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS users_phone_unique "
            "ON users(phone) WHERE phone IS NOT NULL"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS user_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                news_id INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                read_at TEXT,
                UNIQUE(user_id, news_id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS programs (
                slug TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                icon TEXT NOT NULL,
                description TEXT NOT NULL,
                skills TEXT NOT NULL,
                image TEXT NOT NULL DEFAULT ''
            )
            """
        )
        program_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(programs)").fetchall()
        }
        if "image" not in program_columns:
            connection.execute(
                "ALTER TABLE programs ADD COLUMN image TEXT NOT NULL DEFAULT ''"
            )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS contact_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL,
                phone TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS enrollment_applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                registration_number TEXT NOT NULL UNIQUE COLLATE NOCASE,
                surname TEXT NOT NULL,
                first_name TEXT NOT NULL,
                date_of_birth TEXT NOT NULL,
                email TEXT NOT NULL,
                phone TEXT NOT NULL,
                facebook_url TEXT NOT NULL DEFAULT '',
                address TEXT NOT NULL,
                program_slug TEXT NOT NULL,
                program_name TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS director_appointment_slots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                starts_at TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS director_appointments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slot_id INTEGER NOT NULL UNIQUE,
                name TEXT NOT NULL,
                phone TEXT NOT NULL,
                email TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schedule_classes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            "INSERT OR IGNORE INTO schedule_classes (name) VALUES (?)",
            ("Ерөнхий анги",),
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS class_schedule (
                class_id INTEGER NOT NULL,
                period INTEGER NOT NULL CHECK (period BETWEEN 1 AND 6),
                day INTEGER NOT NULL CHECK (day BETWEEN 0 AND 4),
                subject TEXT NOT NULL,
                teacher TEXT NOT NULL DEFAULT '',
                room TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (class_id, period, day)
            )
            """
        )
        schedule_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(class_schedule)")
        }
        if "class_id" not in schedule_columns:
            default_class_id = connection.execute(
                "SELECT id FROM schedule_classes WHERE name = ?",
                ("Ерөнхий анги",),
            ).fetchone()[0]
            connection.execute("ALTER TABLE class_schedule RENAME TO class_schedule_legacy")
            connection.execute(
                """
                CREATE TABLE class_schedule (
                    class_id INTEGER NOT NULL,
                    period INTEGER NOT NULL CHECK (period BETWEEN 1 AND 6),
                    day INTEGER NOT NULL CHECK (day BETWEEN 0 AND 4),
                    subject TEXT NOT NULL,
                    teacher TEXT NOT NULL DEFAULT '',
                    room TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (class_id, period, day)
                )
                """
            )
            connection.execute(
                """
                INSERT INTO class_schedule
                    (class_id, period, day, subject, teacher, room, updated_at)
                SELECT ?, period, day, subject, teacher, room, updated_at
                FROM class_schedule_legacy
                """,
                (default_class_id,),
            )
            connection.execute("DROP TABLE class_schedule_legacy")


initialize_database()
init_news_database(app)


@app.teardown_appcontext
def close_db(_error=None):
    connection = g.pop("db", None)
    if connection is not None:
        connection.close()


@app.before_request
def load_current_user():
    user_id = session.get("user_id")
    g.current_user = (
        get_db().execute(
            "SELECT id, username, full_name, email, phone, role "
            "FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if user_id is not None
        else None
    )


@app.context_processor
def inject_auth_context():
    current_user = g.get("current_user")
    language = session.get("language", "mn")
    return {
        "current_user": current_user,
        "programs": get_programs(),
        "csrf_token": session.setdefault("csrf_token", secrets.token_urlsafe(32)),
        "current_year": date.today().year,
        "language": language,
        "site_name": app.config["SITE_NAME"],
        "t": lambda text: translate(text, language),
        "unread_notification_count": (
            get_db()
            .execute(
                "SELECT COUNT(*) FROM user_notifications "
                "WHERE user_id = ? AND read_at IS NULL",
                (current_user["id"],),
            )
            .fetchone()[0]
            if current_user is not None
            else 0
        ),
    }


def validate_csrf():
    submitted_token = request.form.get("csrf_token", "")
    session_token = session.get("csrf_token", "")
    if not session_token or not hmac.compare_digest(
        submitted_token.encode("utf-8"),
        session_token.encode("utf-8"),
    ):
        abort(400, description="Хүсэлтийн баталгаажуулалт амжилтгүй боллоо.")


def login_required(view):
    @wraps(view)
    def wrapped_view(**kwargs):
        if g.current_user is None:
            return redirect(url_for("login", next=request.path))
        return view(**kwargs)

    return wrapped_view


def admin_required(view):
    @wraps(view)
    def wrapped_view(**kwargs):
        if g.current_user is None:
            return redirect(url_for("admin_login", next=request.path))
        if g.current_user["role"] != "admin":
            abort(403)
        return view(**kwargs)

    return wrapped_view


def safe_next_url(target):
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return url_for("students")


def safe_language_return_url(target):
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return url_for("index")


def safe_admin_next_url(target):
    if (
        target
        and target.startswith("/")
        and not target.startswith("//")
        and "\\" not in target
        and (target == "/admin" or target.startswith("/admin/") or target.startswith("/news/"))
    ):
        return target
    return url_for("admin")


def normalize_phone(value):
    compact = re.sub(r"[\s().-]", "", value)
    if compact.startswith("+976"):
        compact = compact[4:]
    elif compact.startswith("976") and len(compact) == 11:
        compact = compact[3:]
    return compact if re.fullmatch(r"[0-9]{8}", compact) else None


def mongolia_now():
    mongolia_timezone = timezone(timedelta(hours=8))
    return datetime.now(mongolia_timezone).replace(tzinfo=None)


def valid_facebook_url(value):
    if not value:
        return True
    try:
        parsed_url = urlparse(value)
        hostname = parsed_url.hostname
    except ValueError:
        return False
    return (
        len(value) <= 500
        and parsed_url.scheme in {"http", "https"}
        and hostname
        in {"facebook.com", "www.facebook.com", "m.facebook.com", "fb.com", "www.fb.com", "fb.me"}
    )


def send_enrollment_notification(application):
    recipient = os.environ.get("ENROLLMENT_NOTIFY_EMAIL", "").strip()
    smtp_host = os.environ.get("SMTP_HOST", "").strip()
    smtp_username = os.environ.get("SMTP_USERNAME", "").strip()
    smtp_password = os.environ.get("SMTP_PASSWORD", "")
    sender = os.environ.get("SMTP_FROM_EMAIL", "").strip() or smtp_username
    if not all((recipient, smtp_host, sender)):
        app.logger.warning(
            "Enrollment email is not configured. Set ENROLLMENT_NOTIFY_EMAIL, "
            "SMTP_HOST, and SMTP_FROM_EMAIL or SMTP_USERNAME."
        )
        return False
    if bool(smtp_username) != bool(smtp_password):
        app.logger.error("Configure both SMTP_USERNAME and SMTP_PASSWORD, or neither.")
        return False

    try:
        message = EmailMessage()
        message["Subject"] = f"Элсэлтийн хүсэлт: {application['registration_number']}"
        message["From"] = sender
        message["To"] = recipient
        message.set_content(
            "\n".join(
                (
                    "Шинэ элсэлтийн хүсэлт ирлээ.",
                    f"Бүртгэлийн дугаар: {application['registration_number']}",
                    f"Овог: {application['surname']}",
                    f"Нэр: {application['first_name']}",
                    f"Төрсөн огноо: {application['date_of_birth']}",
                    f"Имэйл: {application['email']}",
                    f"Утас: {application['phone']}",
                    f"Facebook: {application['facebook_url'] or 'Оруулаагүй'}",
                    f"Хаяг: {application['address']}",
                    f"Хөтөлбөр: {application['program_name']}",
                )
            )
        )
        smtp_port = int(os.environ.get("SMTP_PORT", "587"))
        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
            if smtp_username:
                smtp.login(smtp_username, smtp_password)
            smtp.send_message(message)
    except (OSError, ValueError, smtplib.SMTPException):
        app.logger.exception("Unable to send enrollment email notification.")
        return False
    return True


def render_home(**context):
    context.setdefault("programs", get_programs())
    context.setdefault("latest_news", get_latest_news())
    context.setdefault("current_year", date.today().year)
    context.setdefault("next_url", url_for("students"))
    context.setdefault("form_data", {})
    return render_template("index.html", **context)


# Нүүр хуудас
@app.route("/")
def index():
    return render_home(next_url=safe_next_url(request.args.get("next", "")))


@app.route("/contact", methods=["POST"])
def contact():
    validate_csrf()
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip().lower()
    phone = request.form.get("phone", "").strip()
    message = request.form.get("message", "").strip()
    form_data = {
        "contact_name": name,
        "contact_email": email,
        "contact_phone": phone,
        "contact_message": message,
    }

    if not name or len(name) > 100:
        flash("Нэр 1–100 тэмдэгт байх ёстой.", "error")
    elif (
        len(email) > 254
        or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)
    ):
        flash("Зөв имэйл хаяг оруулна уу.", "error")
    elif normalize_phone(phone) is None:
        flash("Утасны дугаар 8 оронтой Монгол дугаар байна уу.", "error")
    elif not message or len(message) > 5000:
        flash("Зурвас 1–5000 тэмдэгт байх ёстой.", "error")
    else:
        get_db().execute(
            """
            INSERT INTO contact_messages (name, email, phone, message)
            VALUES (?, ?, ?, ?)
            """,
            (name, email, normalize_phone(phone), message),
        )
        get_db().commit()
        flash("Таны зурвас амжилттай илгээгдлээ. Баярлалаа.", "success")
        return redirect(url_for("index", _anchor="contact"))

    return render_home(form_data=form_data, contact_open=True)


@app.route("/enroll", methods=["GET", "POST"])
def enroll():
    programs = get_programs()
    if request.method == "GET":
        requested_program = request.args.get("program", "")
        selected_program = requested_program if requested_program in programs else ""
        return render_template(
            "enroll.html",
            programs=programs,
            form_data={"program_slug": selected_program},
        )

    validate_csrf()
    registration_number = request.form.get("registration_number", "").strip()
    surname = request.form.get("surname", "").strip()
    first_name = request.form.get("first_name", "").strip()
    date_of_birth = request.form.get("date_of_birth", "").strip()
    email = request.form.get("email", "").strip().lower()
    phone = normalize_phone(request.form.get("phone", "").strip())
    facebook_url = request.form.get("facebook_url", "").strip()
    address = request.form.get("address", "").strip()
    program_slug = request.form.get("program_slug", "").strip()
    program = programs.get(program_slug)

    try:
        birth_date = date.fromisoformat(date_of_birth)
    except ValueError:
        birth_date = None

    if not re.fullmatch(r"[A-Za-z0-9._/-]{1,50}", registration_number):
        flash("Бүртгэлийн дугаар 1–50 тэмдэгт, латин үсэг, тоо ашиглана уу.", "error")
    elif not surname or len(surname) > 100:
        flash("Овог 1–100 тэмдэгт байх ёстой.", "error")
    elif not first_name or len(first_name) > 100:
        flash("Нэр 1–100 тэмдэгт байх ёстой.", "error")
    elif birth_date is None or birth_date > date.today():
        flash("Төрсөн огноогоо зөв оруулна уу.", "error")
    elif len(email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        flash("Зөв Gmail/имэйл хаяг оруулна уу.", "error")
    elif phone is None:
        flash("Утасны дугаар 8 оронтой Монгол дугаар байна уу.", "error")
    elif not valid_facebook_url(facebook_url):
        flash("Facebook хаягийн холбоос зөв эсэхийг шалгана уу.", "error")
    elif not address or len(address) > 500:
        flash("Оршин суугаа хаяг 1–500 тэмдэгт байх ёстой.", "error")
    elif program is None:
        flash("Сонгох хөтөлбөрөө сонгоно уу.", "error")
    else:
        application = {
            "registration_number": registration_number,
            "surname": surname,
            "first_name": first_name,
            "date_of_birth": birth_date.isoformat(),
            "email": email,
            "phone": phone,
            "facebook_url": facebook_url,
            "address": address,
            "program_slug": program_slug,
            "program_name": program["name"],
        }
        try:
            get_db().execute(
                """
                INSERT INTO enrollment_applications (
                    registration_number, surname, first_name, date_of_birth,
                    email, phone, facebook_url, address, program_slug, program_name
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    application["registration_number"],
                    application["surname"],
                    application["first_name"],
                    application["date_of_birth"],
                    application["email"],
                    application["phone"],
                    application["facebook_url"],
                    application["address"],
                    application["program_slug"],
                    application["program_name"],
                ),
            )
            get_db().commit()
        except sqlite3.IntegrityError:
            get_db().rollback()
            flash("Энэ бүртгэлийн дугаараар хүсэлт өмнө нь ирсэн байна.", "error")
        else:
            flash(
                f"Элсэлтийн хүсэлт хадгалагдлаа. Дугаар: {registration_number}",
                "success",
            )
            if not send_enrollment_notification(application):
                flash(
                    "Хүсэлт хадгалагдсан боловч имэйл мэдэгдэл илгээгдсэнгүй. "
                    "Админ самбараас шалгана уу.",
                    "warning",
                )
            return redirect(url_for("enroll", submitted=1))

    return render_template(
        "enroll.html",
        programs=programs,
        form_data=request.form,
    )


# Бидний тухай
@app.route("/about")
def about():
    return redirect(url_for("index", _anchor="about"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if g.current_user is not None:
        return redirect(url_for("students"))

    if request.method == "GET":
        return redirect(url_for("index", _anchor="register"))

    if request.method == "POST":
        validate_csrf()
        username = request.form.get("username", "").strip()
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        phone_input = request.form.get("phone", "").strip()
        phone = normalize_phone(phone_input)
        password = request.form.get("password", "")
        password_confirmation = request.form.get("password_confirmation", "")

        if not full_name or len(full_name) > 100:
            flash("Овог нэр 1–100 тэмдэгт байх ёстой.", "error")
        elif not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username):
            flash(
                "Нэвтрэх нэр 3–32 тэмдэгт байх бөгөөд латин үсэг, тоо, цэг, "
                "зураас ашиглана уу.",
                "error",
            )
        elif (
            len(email) > 254
            or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)
        ):
            flash("Зөв Gmail/имэйл хаяг оруулна уу.", "error")
        elif phone is None:
            flash("Утасны дугаар 8 оронтой Монгол дугаар байна уу.", "error")
        elif len(password) < 10:
            flash("Нууц үг хамгийн багадаа 10 тэмдэгт байна.", "error")
        elif len(password) > 128:
            flash("Нууц үг 128 тэмдэгтээс хэтрэхгүй байна.", "error")
        elif password != password_confirmation:
            flash("Нууц үгийн баталгаажуулалт таарахгүй байна.", "error")
        else:
            identifiers = (username, email, phone)
            existing_user = get_db().execute(
                """
                SELECT 1 FROM users
                WHERE username = ? COLLATE NOCASE
                   OR username = ? COLLATE NOCASE
                   OR username = ? COLLATE NOCASE
                   OR email = ? COLLATE NOCASE
                   OR email = ? COLLATE NOCASE
                   OR email = ? COLLATE NOCASE
                   OR phone = ? OR phone = ? OR phone = ?
                LIMIT 1
                """,
                identifiers + identifiers + identifiers,
            ).fetchone()
            if existing_user is not None:
                flash("Нэвтрэх нэр, имэйл эсвэл утас бүртгэлтэй байна.", "error")
            else:
                try:
                    get_db().execute(
                        "INSERT INTO users "
                        "(username, full_name, email, phone, password_hash) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            username,
                            full_name,
                            email,
                            phone,
                            generate_password_hash(password),
                        ),
                    )
                    get_db().commit()
                except sqlite3.IntegrityError:
                    get_db().rollback()
                    flash("Нэвтрэх нэр, имэйл эсвэл утас бүртгэлтэй байна.", "error")
                else:
                    flash("Бүртгэл амжилттай. Одоо нэвтэрнэ үү.", "success")
                    return redirect(url_for("login"))

    return render_home(form_data=request.form, auth_view="register")


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.current_user is not None:
        return redirect(url_for("students"))

    if request.method == "GET":
        next_url = safe_next_url(request.args.get("next", ""))
        return redirect(url_for("index", _anchor="login", next=next_url))

    if request.method == "POST":
        validate_csrf()
        identifier = request.form.get("identifier", "").strip()
        password = request.form.get("password", "")
        user = get_db().execute(
            """
            SELECT id, username, password_hash FROM users
            WHERE username = ? COLLATE NOCASE
               OR email = ? COLLATE NOCASE
               OR phone = ?
            """,
            (identifier, identifier.lower(), normalize_phone(identifier)),
        ).fetchone()

        if user is None or not check_password_hash(user["password_hash"], password):
            flash("Нэвтрэх нэр эсвэл нууц үг буруу байна.", "error")
        else:
            language = session.get("language", "mn")
            session.clear()
            session["user_id"] = user["id"]
            session["language"] = language
            session["csrf_token"] = secrets.token_urlsafe(32)
            session.permanent = True
            return redirect(safe_next_url(request.form.get("next", "")))

    return render_home(
        next_url=safe_next_url(request.form.get("next", "")),
        auth_view="login",
    )


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    next_url = safe_admin_next_url(
        request.values.get("next", ""),
    )
    if request.method == "POST":
        validate_csrf()
    if g.current_user is not None and g.current_user["role"] == "admin":
        return redirect(next_url)

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = get_db().execute(
            """
            SELECT id, username, password_hash FROM users
            WHERE username = ? COLLATE NOCASE AND role = 'admin'
            """,
            (username,),
        ).fetchone()

        if user is None or not check_password_hash(user["password_hash"], password):
            flash("Админы нэр эсвэл нууц үг буруу байна.", "error")
        else:
            language = session.get("language", "mn")
            session.clear()
            session["user_id"] = user["id"]
            session["language"] = language
            session["csrf_token"] = secrets.token_urlsafe(32)
            session.permanent = True
            return redirect(next_url)

    return render_template("admin_login.html", next_url=next_url)


@app.route("/logout", methods=["POST"])
def logout():
    validate_csrf()
    language = session.get("language", "mn")
    session.clear()
    session["language"] = language
    flash("Та системээс гарлаа.", "success")
    return redirect(url_for("index"))


@app.route("/language", methods=["POST"])
def set_language():
    validate_csrf()
    language = request.form.get("language", "")
    if language not in {"mn", "en"}:
        abort(400, description="Сонгосон хэл буруу байна.")
    session["language"] = language
    return redirect(safe_language_return_url(request.form.get("next", "")))


@app.route("/students")
@login_required
def students():
    return render_template("students.html", form_data={})


@app.route("/search")
def search():
    query = request.args.get("q", "").strip()
    if len(query) > 100:
        abort(400, description="Хайлтын үг 100 тэмдэгтээс хэтрэхгүй байна.")

    program_results = []
    news_results = []
    if query:
        normalized_query = query.casefold()
        pattern = (
            "%"
            + normalized_query.replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
            + "%"
        )
        if normalized_query == "мэргэжил":
            program_results = get_db().execute(
                """
                SELECT slug, name, description, skills
                FROM programs
                ORDER BY name
                LIMIT 50
                """
            ).fetchall()
        else:
            program_results = get_db().execute(
                """
                SELECT slug, name, description, skills
                FROM programs
                WHERE unicode_casefold(name) LIKE ? ESCAPE '\\'
                   OR unicode_casefold(description) LIKE ? ESCAPE '\\'
                   OR unicode_casefold(skills) LIKE ? ESCAPE '\\'
                ORDER BY name
                LIMIT 50
                """,
                (pattern, pattern, pattern),
            ).fetchall()
        news_results = get_db().execute(
            """
            SELECT id, title, summary, category, created_at
            FROM news
            WHERE is_published = 1
              AND (unicode_casefold(title) LIKE ? ESCAPE '\\'
                   OR unicode_casefold(summary) LIKE ? ESCAPE '\\'
                   OR unicode_casefold(body) LIKE ? ESCAPE '\\')
            ORDER BY COALESCE(published_at, created_at) DESC, id DESC
            LIMIT 50
            """,
            (pattern, pattern, pattern),
        ).fetchall()

    return render_template(
        "search.html",
        query=query,
        program_results=program_results,
        news_results=news_results,
    )


@app.route("/notifications")
@login_required
def notifications():
    notification_rows = get_db().execute(
        """
        SELECT user_notifications.news_id, user_notifications.created_at,
               user_notifications.read_at, news.title, news.summary,
               news.category
        FROM user_notifications
        JOIN news ON news.id = user_notifications.news_id
        WHERE user_notifications.user_id = ? AND news.is_published = 1
        ORDER BY user_notifications.created_at DESC, user_notifications.id DESC
        """,
        (g.current_user["id"],),
    ).fetchall()
    return render_template("notifications.html", notifications=notification_rows)


@app.route("/notifications/<int:news_id>/read", methods=["POST"])
@login_required
def mark_notification_read(news_id):
    validate_csrf()
    get_db().execute(
        """
        UPDATE user_notifications
        SET read_at = CURRENT_TIMESTAMP
        WHERE user_id = ? AND news_id = ? AND read_at IS NULL
        """,
        (g.current_user["id"], news_id),
    )
    get_db().commit()
    return redirect(url_for("notifications"))


@app.route("/notifications/read-all", methods=["POST"])
@login_required
def mark_all_notifications_read():
    validate_csrf()
    get_db().execute(
        """
        UPDATE user_notifications
        SET read_at = CURRENT_TIMESTAMP
        WHERE user_id = ? AND read_at IS NULL
          AND news_id IN (SELECT id FROM news WHERE is_published = 1)
        """,
        (g.current_user["id"],),
    )
    get_db().commit()
    return redirect(url_for("notifications"))


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    if request.method == "POST":
        validate_csrf()
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        phone_input = request.form.get("phone", "").strip()
        phone = normalize_phone(phone_input)

        if not full_name or len(full_name) > 100:
            flash("Овог нэр 1–100 тэмдэгт байх ёстой.", "error")
        elif (
            len(email) > 254
            or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)
        ):
            flash("Зөв Gmail/имэйл хаяг оруулна уу.", "error")
        elif phone is None:
            flash("Утасны дугаар 8 оронтой Монгол дугаар байна уу.", "error")
        else:
            existing_user = get_db().execute(
                """
                SELECT 1 FROM users
                WHERE id != ?
                  AND (email = ? COLLATE NOCASE OR phone = ?)
                LIMIT 1
                """,
                (g.current_user["id"], email, phone),
            ).fetchone()
            if existing_user is not None:
                flash(
                    "Энэ имэйл эсвэл утасны дугаар өөр бүртгэлд ашиглагдаж байна.",
                    "error",
                )
            else:
                try:
                    get_db().execute(
                        "UPDATE users SET full_name = ?, email = ?, phone = ? "
                        "WHERE id = ?",
                        (full_name, email, phone, g.current_user["id"]),
                    )
                    get_db().commit()
                except sqlite3.IntegrityError:
                    get_db().rollback()
                    flash(
                        "Энэ имэйл эсвэл утасны дугаар өөр бүртгэлд ашиглагдаж байна.",
                        "error",
                    )
                else:
                    flash("Хувийн мэдээлэл шинэчлэгдлээ.", "success")
                    return redirect(url_for("profile"))

    return render_template(
        "students.html",
        form_data=request.form if request.method == "POST" else {},
    )


# Хөтөлбөрийн үндсэн хуудас
@app.route("/hutulbur")
def hutulbur():
    return redirect(url_for("index", _anchor="programs"))


@app.route("/schedule")
def schedule():
    database = get_db()
    classes = database.execute(
        "SELECT id, name FROM schedule_classes ORDER BY name"
    ).fetchall()
    requested_class_id = request.args.get("class_id", type=int)
    selected_class = next(
        (item for item in classes if item["id"] == requested_class_id),
        classes[0] if classes else None,
    )
    schedule_entries = {
        (row["period"], row["day"]): row
        for row in database.execute(
            "SELECT period, day, subject, teacher, room "
            "FROM class_schedule WHERE class_id = ? ORDER BY period, day",
            (selected_class["id"],) if selected_class else (-1,),
        ).fetchall()
    }
    return render_template(
        "schedule.html",
        schedule_entries=schedule_entries,
        classes=classes,
        selected_class=selected_class,
        day_labels=["Даваа", "Мягмар", "Лхагва", "Пүрэв", "Баасан"],
    )


@app.route("/appointments", methods=["GET", "POST"])
def appointments():
    database = get_db()
    if request.method == "POST":
        validate_csrf()
        slot_id_value = request.form.get("slot_id", "")
        name = request.form.get("name", "").strip()
        phone = normalize_phone(request.form.get("phone", "").strip())
        email = request.form.get("email", "").strip().lower()
        reason = request.form.get("reason", "").strip()

        try:
            slot_id = int(slot_id_value)
        except ValueError:
            slot_id = 0

        slot = database.execute(
            "SELECT id FROM director_appointment_slots "
            "WHERE id = ? AND starts_at >= ?",
            (slot_id, mongolia_now().isoformat(sep=" ", timespec="seconds")),
        ).fetchone()
        if slot is None:
            flash("Сонгосон сул цаг боломжгүй болсон байна. Дахин сонгоно уу.", "error")
        elif not name or len(name) > 100:
            flash("Нэр 1–100 тэмдэгт байх ёстой.", "error")
        elif phone is None:
            flash("Утасны дугаар 8 оронтой Монгол дугаар байна уу.", "error")
        elif len(email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            flash("Зөв имэйл хаяг оруулна уу.", "error")
        elif not reason or len(reason) > 1000:
            flash("Уулзалтын шалтгаан 1–1000 тэмдэгт байх ёстой.", "error")
        else:
            try:
                database.execute(
                    """
                    INSERT INTO director_appointments
                        (slot_id, name, phone, email, reason)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (slot_id, name, phone, email, reason),
                )
                database.commit()
            except sqlite3.IntegrityError:
                database.rollback()
                flash("Энэ цагийг өөр хүн захиалсан байна. Өөр цаг сонгоно уу.", "error")
            else:
                flash("Уулзалтын цаг амжилттай захиалагдлаа.", "success")
                return redirect(url_for("appointments"))

    slots = database.execute(
        """
        SELECT slots.id, slots.starts_at
        FROM director_appointment_slots AS slots
        LEFT JOIN director_appointments AS bookings ON bookings.slot_id = slots.id
        WHERE slots.starts_at >= ? AND bookings.id IS NULL
        ORDER BY slots.starts_at
        """,
        (mongolia_now().isoformat(sep=" ", timespec="seconds"),),
    ).fetchall()
    return render_template(
        "appointments.html",
        slots=slots,
        form_data=request.form if request.method == "POST" else {},
    )


# Хөтөлбөр тус бүрийн мэдээлэл
programs = {
    "ikh-zasag": {
        "name": "Их Засаг Политехник коллеж",
        "icon": "🏫",
        "image": "ikh-zasag-school.png",
        "description": "Их Засаг Политехник коллежийн сургалтын орчин болон мэргэжлийн хөтөлбөрүүдтэй танилцаарай.",
        "skills": [
            "Мэргэжлийн сургалт",
            "Сургалтын хөтөлбөрүүд",
            "Суралцах орчин",
        ],
    },
    "basketball": {
        "name": "Сагсан бөмбөг",
        "icon": "🏀",
        "image": "",
        "description": "Сагсан бөмбөгийн үндсэн техник, багийн тоглолтын ур чадвараа хөгжүүлээрэй.",
        "skills": [
            "Бөмбөг эзэмшилт ба дриблинг",
            "Дамжуулалт",
            "Шидэлт",
            "Хамгаалалт",
            "Багийн ажиллагаа",
        ],
    },
    "web-programming": {
        "name": "Вэб програмчлал",
        "icon": "💻",
        "description": "Вэб сайт болон веб систем боловсруулах чиглэлээр мэргэжлийн мэдлэг, ур чадвар эзэмшинэ.",
        "skills": [
            "HTML, CSS",
            "JavaScript",
            "Python",
            "Flask",
            "Өгөгдлийн сан",
            "Вэб сайт хөгжүүлэлт",
        ],
    },
    "graphic-design": {
        "name": "Хэвлэлийн график дизайн",
        "icon": "🎨",
        "description": "Хэвлэлийн болон график дизайны бүтээгдэхүүн боловсруулах, эх бэлтгэх, дизайн хийх ур чадвар эзэмшинэ.",
        "skills": [
            "Adobe Photoshop",
            "Adobe Illustrator",
            "CorelDRAW",
            "InDesign",
            "Лого дизайн",
            "Хэвлэлийн эх бэлтгэл",
        ],
    },
    "hairdressing": {
        "name": "Үсчин",
        "icon": "💇",
        "description": "Үс засалт, хэлбэржүүлэлт, будаг болон гоо сайхны үйлчилгээний мэргэжлийн ур чадвар эзэмшинэ.",
        "skills": [
            "Үс засалт",
            "Үс будах",
            "Үс хэлбэржүүлэх",
            "Үсний арчилгаа",
            "Орчин үеийн үсний технологи",
        ],
    },
    "tourism": {
        "name": "Аялал жуулчлал",
        "icon": "✈️",
        "description": "Аялал жуулчлалын үйлчилгээ, аяллын хөтөлбөр боловсруулах, жуулчдад үйлчилгээ үзүүлэх мэдлэг эзэмшинэ.",
        "skills": [
            "Аяллын хөтөлбөр боловсруулах",
            "Жуулчны үйлчилгээ",
            "Хөтөч тайлбарлагч",
            "Зочлох үйлчилгээ",
            "Аялал жуулчлалын менежмент",
        ],
    },
    "office": {
        "name": "Оффис бичиг хэрэг",
        "icon": "📑",
        "description": "Албан хэрэг хөтлөлт, бичиг баримт боловсруулах, оффисын программ ашиглах ур чадвар эзэмшинэ.",
        "skills": [
            "MS Word",
            "MS Excel",
            "MS PowerPoint",
            "Албан бичиг боловсруулах",
            "Архив, бичиг хэрэг",
            "Оффисын зохион байгуулалт",
        ],
    },
    "beauty": {
        "name": "Гоо засалч",
        "icon": "💄",
        "image": "basketball.png",
        "description": "Гоо сайхны үйлчилгээ, нүүр будалт, арьс арчилгаа болон гоо заслын мэргэжлийн ур чадвар эзэмшинэ.",
        "skills": [
            "Нүүр будалт",
            "Арьс арчилгаа",
            "Гоо сайхны үйлчилгээ",
            "Маникюр",
            "Педикюр",
            "Гоо заслын технологи",
        ],
    },
    "nursing": {
        "name": "Сувилагчийн туслах",
        "icon": "🩺",
        "description": "Эрүүл мэндийн байгууллагад сувилагчид туслах үйлчилгээ үзүүлэх мэдлэг, ур чадвар эзэмшинэ.",
        "skills": [
            "Сувилахуйн тусламж",
            "Анхны тусламж",
            "Өвчтөний асаргаа",
            "Эрүүл мэндийн боловсрол",
            "Сувилахуйн ёс зүй",
        ],
    },
    "accounting": {
        "name": "Нярав",
        "icon": "💰",
        "description": "Байгууллагын бараа материал, хөрөнгө, мөнгөн хөрөнгийн бүртгэл, тооцоог хөтлөх мэдлэг эзэмшинэ.",
        "skills": [
            "Анхан шатны бүртгэл",
            "Бараа материалын бүртгэл",
            "Мөнгөн хөрөнгийн бүртгэл",
            "Excel",
            "Санхүүгийн тайлан",
        ],
    },
    "stage-screen": {
        "name": "Тайз дэлгэц",
        "icon": "🎬",
        "description": "Тайз, дэлгэцийн бүтээл, зураг авалт, гэрэлтүүлэг, дуу болон техникийн чиглэлээр мэдлэг эзэмшинэ.",
        "skills": [
            "Камер ашиглалт",
            "Видео зураг авалт",
            "Видео эвлүүлэг",
            "Тайзны зохион байгуулалт",
            "Гэрэлтүүлэг",
            "Дууны техник",
        ],
    },
    "fashion-design": {
        "name": "Хувцас дизайн",
        "icon": "👗",
        "description": "Хувцас загвар зохион бүтээх, эсгүүр хийх, оёх болон дизайны мэргэжлийн ур чадвар эзэмшинэ.",
        "skills": [
            "Хувцас загвар зохион бүтээлт",
            "Эсгүүр",
            "Оёдол",
            "Материал судлал",
            "Fashion illustration",
            "Хувцасны дизайн",
        ],
    },
    "construction": {
        "name": "Барилга",
        "icon": "🏗️",
        "description": "Барилгын технологи, зураг төсөл, барилгын ажил гүйцэтгэх мэргэжлийн мэдлэг, ур чадвар эзэмшинэ.",
        "skills": [
            "Барилгын зураг",
            "Барилгын материал",
            "Барилгын технологи",
            "Хөдөлмөрийн аюулгүй байдал",
            "Барилгын ажлын зохион байгуулалт",
        ],
    }
}


DEFAULT_PROGRAM_IMAGES = {
    "ikh-zasag": "ikh-zasag-school.png",
    "web-programming": "web-programming.jpg",
    "graphic-design": "graphic-design.jpg",
    "hairdressing": "hairdressing.jpg",
    "tourism": "tourism.jpg",
    "office": "office.jpg",
    "beauty": "beauty.jpg",
    "nursing": "nursing.jpg",
    "accounting": "accounting.jpg",
    "stage-screen": "stage-screen.jpg",
    "fashion-design": "fashion-design.jpg",
    "construction": "construction.jpg",
}


def get_programs():
    rows = get_db().execute(
        "SELECT slug, name, icon, description, skills, image "
        "FROM programs ORDER BY rowid"
    ).fetchall()
    return {
        row["slug"]: {
            "name": row["name"],
            "icon": row["icon"],
            "description": row["description"],
            "skills": json.loads(row["skills"]),
            "image": row["image"] or DEFAULT_PROGRAM_IMAGES.get(row["slug"], ""),
        }
        for row in rows
    }


def initialize_programs():
    database = get_db()
    for slug, program in programs.items():
        database.execute(
            """
            INSERT OR IGNORE INTO programs
                (slug, name, icon, description, skills, image)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                slug,
                program["name"],
                program["icon"],
                program["description"],
                json.dumps(program["skills"], ensure_ascii=False),
                program.get("image", DEFAULT_PROGRAM_IMAGES.get(slug, "")),
            ),
        )
    database.execute(
        "UPDATE programs SET image = '' WHERE slug = 'basketball' AND image = ?",
        ("basketball.png",),
    )
    database.execute(
        """
        UPDATE programs SET image = 'beauty.jpg'
        WHERE slug = 'beauty' AND image = 'basketball.png'
        """,
    )
    database.execute(
        "UPDATE programs SET name = ? WHERE slug = ? AND name = ?",
        ("Сувилагчийн туслах", "nursing", "Сувилагч"),
    )
    database.execute(
        "UPDATE programs SET description = ? WHERE slug = ? AND description = ?",
        (
            programs["nursing"]["description"],
            "nursing",
            "Эрүүл мэндийн байгууллагад сувилахуйн тусламж үйлчилгээ үзүүлэх "
            "мэргэжлийн мэдлэг, ур чадвар эзэмшинэ.",
        ),
    )
    database.commit()


def initialize_admin():
    configured_username = os.environ.get("FLASK_ADMIN_USERNAME")
    configured_password = os.environ.get("FLASK_ADMIN_PASSWORD")
    using_default_credentials = (
        configured_username is None and configured_password is None
    )
    if app.config["APP_ENV"] == "production" and using_default_credentials:
        raise RuntimeError(
            "Production requires FLASK_ADMIN_USERNAME and FLASK_ADMIN_PASSWORD."
        )
    username = (
        "admin1"
        if using_default_credentials
        else (configured_username or "").strip()
    )
    password = "admin1" if using_default_credentials else (configured_password or "")
    if using_default_credentials:
        app.logger.warning(
            "Using the default admin credentials (admin1/admin1). "
            "Set FLASK_ADMIN_USERNAME and FLASK_ADMIN_PASSWORD before deployment."
        )
    if not username or not password:
        raise RuntimeError(
            "Set both FLASK_ADMIN_USERNAME and FLASK_ADMIN_PASSWORD to configure the admin."
        )
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username):
        raise RuntimeError("FLASK_ADMIN_USERNAME must be 3-32 valid username characters.")
    if len(password) > 128 or (
        not using_default_credentials and len(password) < 12
    ):
        raise RuntimeError("FLASK_ADMIN_PASSWORD must contain 12-128 characters.")

    database = get_db()
    user = database.execute(
        "SELECT id, password_hash FROM users WHERE username = ? COLLATE NOCASE",
        (username,),
    ).fetchone()
    if user is None:
        database.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, 'admin')",
            (username, generate_password_hash(password)),
        )
    else:
        if app.config["APP_ENV"] == "production":
            database.execute(
                "UPDATE users SET role = 'admin', password_hash = ? WHERE id = ?",
                (generate_password_hash(password), user["id"]),
            )
        else:
            database.execute(
                "UPDATE users SET role = 'admin' WHERE id = ?", (user["id"],)
            )
    if (
        app.config["APP_ENV"] == "production"
        and username.casefold() != "admin1"
    ):
        database.execute(
            "UPDATE users SET role = 'student' WHERE username = ? COLLATE NOCASE",
            ("admin1",),
        )
    database.commit()


with app.app_context():
    initialize_programs()
    initialize_admin()


@app.route("/hutulbur/<program_slug>")
def program_detail(program_slug):
    program = get_programs().get(program_slug)
    if program is None:
        return "Хөтөлбөр олдсонгүй", 404

    return render_template(
        "program_detail.html",
        program=program,
        program_slug=program_slug,
    )


@app.route("/admin")
@admin_required
def admin():
    database = get_db()
    users = database.execute(
        "SELECT id, username, email, phone, role, created_at FROM users ORDER BY id"
    ).fetchall()
    statistics = {
        "users": database.execute("SELECT COUNT(*) FROM users").fetchone()[0],
        "admins": database.execute(
            "SELECT COUNT(*) FROM users WHERE role = 'admin'"
        ).fetchone()[0],
        "programs": database.execute("SELECT COUNT(*) FROM programs").fetchone()[0],
        "messages": database.execute(
            "SELECT COUNT(*) FROM contact_messages"
        ).fetchone()[0],
        "enrollments": database.execute(
            "SELECT COUNT(*) FROM enrollment_applications"
        ).fetchone()[0],
        "appointments": database.execute(
            "SELECT COUNT(*) FROM director_appointments"
        ).fetchone()[0],
    }
    return render_template(
        "admin.html",
        users=users,
        statistics=statistics,
        programs=get_programs(),
    )


@app.route("/admin/schedule")
@admin_required
def admin_schedule():
    database = get_db()
    classes = database.execute(
        "SELECT id, name FROM schedule_classes ORDER BY name"
    ).fetchall()
    requested_class_id = request.args.get("class_id", type=int)
    selected_class = next(
        (item for item in classes if item["id"] == requested_class_id),
        classes[0] if classes else None,
    )
    schedule_entries = {
        (row["period"], row["day"]): row
        for row in database.execute(
            "SELECT period, day, subject, teacher, room "
            "FROM class_schedule WHERE class_id = ? ORDER BY period, day",
            (selected_class["id"],) if selected_class else (-1,),
        ).fetchall()
    }
    return render_template(
        "admin_schedule.html",
        classes=classes,
        selected_class=selected_class,
        schedule_entries=schedule_entries,
        day_labels=["Даваа", "Мягмар", "Лхагва", "Пүрэв", "Баасан"],
    )


@app.route("/admin/schedule/classes", methods=["POST"])
@admin_required
def create_schedule_class():
    validate_csrf()
    name = request.form.get("name", "").strip()
    if not name or len(name) > 100:
        flash("Ангийн нэрийг 1–100 тэмдэгтээр оруулна уу.", "error")
        return redirect(url_for("admin_schedule"))

    database = get_db()
    existing_names = database.execute(
        "SELECT name FROM schedule_classes"
    ).fetchall()
    if any(row["name"].casefold() == name.casefold() for row in existing_names):
        flash("Ийм нэртэй анги өмнө нь нэмэгдсэн байна.", "error")
        return redirect(url_for("admin_schedule"))
    try:
        cursor = database.execute(
            "INSERT INTO schedule_classes (name) VALUES (?)",
            (name,),
        )
        database.commit()
    except sqlite3.IntegrityError:
        database.rollback()
        flash("Ийм нэртэй анги өмнө нь нэмэгдсэн байна.", "error")
        return redirect(url_for("admin_schedule"))
    flash("Анги нэмэгдлээ.", "success")
    return redirect(url_for("admin_schedule", class_id=cursor.lastrowid))


@app.route("/admin/schedule/classes/<int:class_id>/delete", methods=["POST"])
@admin_required
def delete_schedule_class(class_id):
    validate_csrf()
    database = get_db()
    class_row = database.execute(
        "SELECT id FROM schedule_classes WHERE id = ?",
        (class_id,),
    ).fetchone()
    if class_row is None:
        abort(404)
    database.execute("DELETE FROM class_schedule WHERE class_id = ?", (class_id,))
    database.execute("DELETE FROM schedule_classes WHERE id = ?", (class_id,))
    database.commit()
    flash("Анги болон хуваарь устгагдлаа.", "success")
    return redirect(url_for("admin_schedule"))


@app.route(
    "/admin/schedule/<int:class_id>/<int:period>/<int:day>",
    methods=["POST"],
)
@admin_required
def update_class_schedule(class_id, period, day):
    validate_csrf()
    if period not in range(1, 6) or day not in range(5):
        abort(404)

    database = get_db()
    if database.execute(
        "SELECT 1 FROM schedule_classes WHERE id = ?",
        (class_id,),
    ).fetchone() is None:
        abort(404)
    if request.form.get("action") == "delete":
        database.execute(
            "DELETE FROM class_schedule "
            "WHERE class_id = ? AND period = ? AND day = ?",
            (class_id, period, day),
        )
        database.commit()
        flash("Хичээлийн цаг устгагдлаа.", "success")
        return redirect(url_for("admin_schedule", class_id=class_id))

    subject = request.form.get("subject", "").strip()
    teacher = request.form.get("teacher", "").strip()
    room = request.form.get("room", "").strip()
    if not subject or len(subject) > 120:
        flash("Хичээлийн нэрийг 1–120 тэмдэгтээр оруулна уу.", "error")
    elif len(teacher) > 100 or len(room) > 40:
        flash("Багшийн нэр эсвэл өрөөний дугаар хэт урт байна.", "error")
    else:
        database.execute(
            """
            INSERT INTO class_schedule
                (class_id, period, day, subject, teacher, room, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(class_id, period, day) DO UPDATE SET
                subject = excluded.subject,
                teacher = excluded.teacher,
                room = excluded.room,
                updated_at = CURRENT_TIMESTAMP
            """,
            (class_id, period, day, subject, teacher, room),
        )
        database.commit()
        flash("Хичээлийн хуваарь хадгалагдлаа.", "success")
    return redirect(url_for("admin_schedule", class_id=class_id))


@app.route("/admin/appointments")
@admin_required
def admin_appointments():
    slots = get_db().execute(
        """
        SELECT slots.id, slots.starts_at, bookings.id AS booking_id,
               bookings.name, bookings.phone, bookings.email, bookings.reason,
               bookings.created_at AS booked_at
        FROM director_appointment_slots AS slots
        LEFT JOIN director_appointments AS bookings ON bookings.slot_id = slots.id
        ORDER BY slots.starts_at DESC
        """
    ).fetchall()
    return render_template(
        "admin_appointments.html",
        slots=slots,
        today=mongolia_now().date().isoformat(),
    )


@app.route("/admin/appointments/slots", methods=["POST"])
@admin_required
def create_appointment_slot():
    validate_csrf()
    date_value = request.form.get("date", "").strip()
    time_value = request.form.get("time", "").strip()
    try:
        day = date.fromisoformat(date_value)
        start_time = time.fromisoformat(time_value)
    except ValueError:
        flash("Огноо болон 30 минутын эхлэх цагийг зөв оруулна уу.", "error")
        return redirect(url_for("admin_appointments"))
    starts_at = datetime.combine(day, start_time)

    if (
        start_time.minute not in {0, 30}
        or start_time.second
        or start_time.microsecond
    ):
        flash("Огноо болон 30 минутын эхлэх цагийг зөв оруулна уу.", "error")
    elif starts_at <= mongolia_now():
        flash("Ирээдүйн огноо, цаг сонгоно уу.", "error")
    else:
        try:
            get_db().execute(
                "INSERT INTO director_appointment_slots (starts_at) VALUES (?)",
                (starts_at.isoformat(sep=" ", timespec="minutes"),),
            )
            get_db().commit()
        except sqlite3.IntegrityError:
            get_db().rollback()
            flash("Энэ цагийн сул боломж өмнө нь нэмэгдсэн байна.", "error")
        else:
            flash("30 минутын уулзалтын сул цаг нэмэгдлээ.", "success")
    return redirect(url_for("admin_appointments"))


@app.route("/admin/appointments/slots/<int:slot_id>/update", methods=["POST"])
@admin_required
def update_appointment_slot(slot_id):
    validate_csrf()
    database = get_db()
    slot = database.execute(
        """
        SELECT slots.id,
               EXISTS (
                   SELECT 1
                   FROM director_appointments AS bookings
                   WHERE bookings.slot_id = slots.id
               ) AS is_booked
        FROM director_appointment_slots AS slots
        WHERE slots.id = ?
        """,
        (slot_id,),
    ).fetchone()
    if slot is None:
        abort(404)
    if slot["is_booked"]:
        flash("Захиалгатай цагийг өөрчлөхийн өмнө уулзалтыг цуцална уу.", "error")
        return redirect(url_for("admin_appointments"))

    try:
        day = date.fromisoformat(request.form.get("date", "").strip())
        start_time = time.fromisoformat(request.form.get("time", "").strip())
    except ValueError:
        flash("Огноо болон 30 минутын эхлэх цагийг зөв оруулна уу.", "error")
        return redirect(url_for("admin_appointments"))
    starts_at = datetime.combine(day, start_time)

    if (
        start_time.minute not in {0, 30}
        or start_time.second
        or start_time.microsecond
    ):
        flash("Огноо болон 30 минутын эхлэх цагийг зөв оруулна уу.", "error")
    elif starts_at <= mongolia_now():
        flash("Ирээдүйн огноо, цаг сонгоно уу.", "error")
    else:
        try:
            database.execute(
                "UPDATE director_appointment_slots SET starts_at = ? WHERE id = ?",
                (starts_at.isoformat(sep=" ", timespec="minutes"), slot_id),
            )
            database.commit()
        except sqlite3.IntegrityError:
            database.rollback()
            flash("Энэ цагийн сул боломж өмнө нь нэмэгдсэн байна.", "error")
        else:
            flash("Сул цаг шинэчлэгдлээ.", "success")
    return redirect(url_for("admin_appointments"))


@app.route("/admin/appointments/slots/<int:slot_id>/delete", methods=["POST"])
@admin_required
def delete_appointment_slot(slot_id):
    validate_csrf()
    database = get_db()
    booked = database.execute(
        "SELECT 1 FROM director_appointments WHERE slot_id = ?", (slot_id,)
    ).fetchone()
    if booked:
        flash("Захиалгатай цагийг устгахын өмнө уулзалтыг цуцална уу.", "error")
    else:
        cursor = database.execute(
            "DELETE FROM director_appointment_slots WHERE id = ?", (slot_id,)
        )
        if cursor.rowcount == 0:
            abort(404)
        database.commit()
        flash("Сул цаг устгагдлаа.", "success")
    return redirect(url_for("admin_appointments"))


@app.route("/admin/appointments/bookings/<int:booking_id>/cancel", methods=["POST"])
@admin_required
def cancel_appointment(booking_id):
    validate_csrf()
    cursor = get_db().execute(
        "DELETE FROM director_appointments WHERE id = ?", (booking_id,)
    )
    if cursor.rowcount == 0:
        abort(404)
    get_db().commit()
    flash("Уулзалтын захиалга цуцлагдлаа. Цаг дахин сул боллоо.", "success")
    return redirect(url_for("admin_appointments"))


@app.route("/admin/enrollments")
@admin_required
def admin_enrollments():
    applications = get_db().execute(
        """
        SELECT registration_number, surname, first_name, date_of_birth,
               email, phone, facebook_url, address, program_name, created_at
        FROM enrollment_applications
        ORDER BY created_at DESC, id DESC
        """
    ).fetchall()
    return render_template("admin_enrollments.html", applications=applications)


@app.route("/admin/messages")
@admin_required
def admin_messages():
    messages = get_db().execute(
        """
        SELECT id, name, email, phone, message, created_at
        FROM contact_messages
        ORDER BY created_at DESC, id DESC
        """
    ).fetchall()
    return render_template(
        "admin_messages.html",
        messages=messages,
        message_count=len(messages),
    )


@app.route("/admin/messages/<int:message_id>/delete", methods=["POST"])
@admin_required
def delete_contact_message(message_id):
    validate_csrf()
    cursor = get_db().execute(
        "DELETE FROM contact_messages WHERE id = ?", (message_id,)
    )
    if cursor.rowcount == 0:
        abort(404)
    get_db().commit()
    flash("Санал хүсэлт устгагдлаа.", "success")
    return redirect(url_for("admin_messages"))


@app.route("/admin/users/<int:user_id>/role", methods=["POST"])
@admin_required
def update_user_role(user_id):
    validate_csrf()
    role = request.form.get("role", "")
    if role not in {"student", "admin"}:
        abort(400, description="Хэрэглэгчийн эрхийн утга буруу байна.")

    database = get_db()
    target = database.execute(
        "SELECT id, role FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    if target is None:
        abort(404)
    if target["id"] == g.current_user["id"] and role != "admin":
        flash("Та өөрийн админ эрхийг бууруулах боломжгүй.", "error")
    elif target["role"] == "admin" and role != "admin":
        cursor = database.execute(
            """
            UPDATE users SET role = ?
            WHERE id = ? AND (SELECT COUNT(*) FROM users WHERE role = 'admin') > 1
            """,
            (role, user_id),
        )
        if cursor.rowcount == 0:
            flash("Сүүлчийн админы эрхийг бууруулж болохгүй.", "error")
        else:
            database.commit()
            flash("Хэрэглэгчийн эрх шинэчлэгдлээ.", "success")
    else:
        database.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
        database.commit()
        flash("Хэрэглэгчийн эрх шинэчлэгдлээ.", "success")
    return redirect(url_for("admin"))


@app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def delete_user(user_id):
    validate_csrf()
    database = get_db()
    target = database.execute(
        "SELECT id, role FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    if target is None:
        abort(404)
    if target["id"] == g.current_user["id"]:
        flash("Та өөрийн бүртгэлийг админ самбараас устгах боломжгүй.", "error")
    elif target["role"] == "admin":
        cursor = database.execute(
            """
            DELETE FROM users
            WHERE id = ? AND (SELECT COUNT(*) FROM users WHERE role = 'admin') > 1
            """,
            (user_id,),
        )
        if cursor.rowcount == 0:
            flash("Сүүлчийн админы бүртгэлийг устгаж болохгүй.", "error")
        else:
            database.commit()
            flash("Хэрэглэгчийн бүртгэл устгагдлаа.", "success")
    else:
        database.execute("DELETE FROM users WHERE id = ?", (user_id,))
        database.commit()
        flash("Хэрэглэгчийн бүртгэл устгагдлаа.", "success")
    return redirect(url_for("admin"))


@app.route("/admin/programs/<slug>", methods=["POST"])
@admin_required
def update_program(slug):
    validate_csrf()
    if slug not in get_programs():
        abort(404)

    name = request.form.get("name", "").strip()
    icon = request.form.get("icon", "").strip()
    description = request.form.get("description", "").strip()
    skills = [
        skill.strip()
        for skill in request.form.get("skills", "").splitlines()
        if skill.strip()
    ]
    if (
        not name
        or len(name) > 100
        or not icon
        or len(icon) > 16
        or not description
        or len(description) > 1000
        or not skills
        or len(skills) > 20
        or any(len(skill) > 100 for skill in skills)
    ):
        flash("Хөтөлбөрийн мэдээллийг шалгана уу.", "error")
        return redirect(url_for("admin"))

    get_db().execute(
        "UPDATE programs SET name = ?, icon = ?, description = ?, skills = ? "
        "WHERE slug = ?",
        (name, icon, description, json.dumps(skills, ensure_ascii=False), slug),
    )
    get_db().commit()
    flash("Хөтөлбөрийн мэдээлэл хадгалагдлаа.", "success")
    return redirect(url_for("admin"))


# ЗАСВАР: _name_ болон _main_-ийг хоёр талдаа давхар доод зураастай болгож засав.
if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        debug=os.environ.get("FLASK_DEBUG") == "1",
    )