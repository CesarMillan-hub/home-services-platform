from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
from datetime import datetime
from functools import wraps
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

import click
from flask import (
    Flask,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
INSTANCE_DIR = BASE_DIR / "instance"

def resolve_path(env_name: str, default: Path) -> Path:
    raw_value = os.environ.get(env_name)
    path = Path(raw_value) if raw_value else default
    if not path.is_absolute():
        path = BASE_DIR / path
    return path

DATABASE = resolve_path("DATABASE_PATH", INSTANCE_DIR / "services.sqlite")
UPLOAD_DIR = resolve_path("UPLOAD_DIR", BASE_DIR / "uploads")

DATABASE.parent.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY", "dev-secret-change-me"),
    DATABASE=str(DATABASE),
    UPLOAD_FOLDER=str(UPLOAD_DIR),
    MAX_CONTENT_LENGTH=8 * 1024 * 1024,
    LIQPAY_PUBLIC_KEY=os.environ.get("LIQPAY_PUBLIC_KEY", "").strip(),
    LIQPAY_PRIVATE_KEY=os.environ.get("LIQPAY_PRIVATE_KEY", "").strip(),
    LIQPAY_SANDBOX=os.environ.get("LIQPAY_SANDBOX", "0").strip().lower() in {"1", "true", "yes", "on"},
    PUBLIC_BASE_URL=os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/"),
    PREFERRED_URL_SCHEME="https" if os.environ.get("PUBLIC_BASE_URL", "").startswith("https://") else "http",
)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

LIQPAY_CHECKOUT_URL = "https://www.liqpay.ua/api/3/checkout"
LIQPAY_SUCCESS_STATUSES = {"success", "sandbox"}

ROLES = {
    "client": "Клієнт",
    "worker": "Виконавець",
    "admin": "Адміністратор",
}

SPECIALIZATIONS = {
    "cleaning": "Клінінг",
    "repair": "Ремонт",
    "delivery": "Доставка",
}

ORDER_STATUSES = [
    "Нове",
    "Очікує підтвердження",
    "Прийнято виконавцем",
    "В дорозі / В роботі",
    "Виконано",
    "Скасовано",
]

WORKER_STATUS_OPTIONS = [
    "Прийнято виконавцем",
    "В дорозі / В роботі",
    "Виконано",
    "Скасовано",
]

ALLOWED_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL CHECK (role IN ('client', 'worker', 'admin')),
    full_name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    phone TEXT,
    city TEXT,
    address TEXT,
    specialization TEXT,
    service_area TEXT,
    schedule TEXT,
    price_text TEXT,
    is_blocked INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    slug TEXT NOT NULL UNIQUE,
    description TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS services (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    slug TEXT NOT NULL,
    description TEXT,
    unit_label TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE RESTRICT,
    UNIQUE (category_id, slug)
);

CREATE TABLE IF NOT EXISTS prices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    service_id INTEGER NOT NULL,
    base_price REAL NOT NULL DEFAULT 0,
    unit_price REAL NOT NULL DEFAULT 0,
    unit_name TEXT NOT NULL DEFAULT 'од.',
    extra_option_price REAL NOT NULL DEFAULT 0,
    urgent_multiplier REAL NOT NULL DEFAULT 1.0,
    is_active INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (service_id) REFERENCES services(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL,
    worker_id INTEGER,
    category_id INTEGER NOT NULL,
    service_id INTEGER NOT NULL,
    city TEXT NOT NULL,
    street TEXT NOT NULL,
    building TEXT NOT NULL,
    apartment TEXT,
    preferred_at TEXT NOT NULL,
    description TEXT,
    cleaning_area REAL,
    rooms INTEGER,
    extra_options TEXT,
    repair_type TEXT,
    repair_hours REAL,
    urgency TEXT NOT NULL DEFAULT 'normal',
    photo_filename TEXT,
    delivery_from TEXT,
    delivery_to TEXT,
    delivery_distance_km REAL,
    weight REAL,
    dimensions TEXT,
    estimated_price REAL NOT NULL DEFAULT 0,
    final_price REAL,
    status TEXT NOT NULL DEFAULT 'Нове',
    is_paid INTEGER NOT NULL DEFAULT 0,
    paid_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (client_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (worker_id) REFERENCES users(id) ON DELETE SET NULL,
    FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE RESTRICT,
    FOREIGN KEY (service_id) REFERENCES services(id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS order_status_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    changed_by_user_id INTEGER,
    comment TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE,
    FOREIGN KEY (changed_by_user_id) REFERENCES users(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS order_rejections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    worker_id INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE,
    FOREIGN KEY (worker_id) REFERENCES users(id) ON DELETE CASCADE,
    UNIQUE (order_id, worker_id)
);

CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    provider TEXT NOT NULL DEFAULT 'liqpay',
    provider_order_id TEXT NOT NULL UNIQUE,
    provider_payment_id TEXT,
    amount REAL NOT NULL,
    currency TEXT NOT NULL DEFAULT 'UAH',
    status TEXT NOT NULL DEFAULT 'created',
    checkout_data TEXT,
    checkout_signature TEXT,
    raw_response TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL UNIQUE,
    client_id INTEGER NOT NULL,
    worker_id INTEGER,
    rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
    text TEXT NOT NULL,
    is_visible INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE,
    FOREIGN KEY (client_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (worker_id) REFERENCES users(id) ON DELETE SET NULL
);
"""


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        db = sqlite3.connect(app.config["DATABASE"])
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        g.db = db
    return g.db


@app.teardown_appcontext
def close_db(error: Exception | None = None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def db_one(query: str, params: tuple = ()) -> sqlite3.Row | None:
    return get_db().execute(query, params).fetchone()


def db_all(query: str, params: tuple = ()) -> list[sqlite3.Row]:
    return get_db().execute(query, params).fetchall()


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def normalize_slug(value: str, fallback_prefix: str = "item") -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9_-]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value or f"{fallback_prefix}-{int(datetime.now().timestamp())}"


def format_datetime(value: str | None) -> str:
    if not value:
        return "—"
    try:
        clean = value.replace("T", " ").split(".")[0]
        dt = datetime.fromisoformat(clean)
        return dt.strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return value


def money(value: float | int | str | None) -> str:
    if value is None or value == "":
        return "—"
    try:
        return f"{float(value):,.2f} грн".replace(",", " ")
    except (TypeError, ValueError):
        return str(value)


def role_label(role: str | None) -> str:
    return ROLES.get(role or "", role or "—")


def spec_label(spec: str | None) -> str:
    return SPECIALIZATIONS.get(spec or "", spec or "—")


def active_label(value: int | None) -> str:
    return "Активно" if value else "Вимкнено"


def status_class(status: str | None) -> str:
    mapping = {
        "Нове": "new",
        "Очікує підтвердження": "pending",
        "Прийнято виконавцем": "accepted",
        "В дорозі / В роботі": "progress",
        "Виконано": "done",
        "Скасовано": "cancelled",
    }
    return mapping.get(status or "", "default")


def normalize_datetime_local(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip().replace("T", " ")
    try:
        dt = datetime.fromisoformat(value)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def to_float(value: str | None, default: float = 0) -> float:
    if value is None or str(value).strip() == "":
        return default
    try:
        return float(str(value).replace(",", "."))
    except ValueError:
        return default


def to_int(value: str | None, default: int = 0) -> int:
    if value is None or str(value).strip() == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


def allowed_image(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_IMAGE_EXTENSIONS


def public_url_for(endpoint: str, **values) -> str:
    public_base_url = app.config.get("PUBLIC_BASE_URL", "")
    if public_base_url:
        return f"{public_base_url}{url_for(endpoint, **values)}"
    return url_for(endpoint, _external=True, **values)


def liqpay_is_configured() -> bool:
    return bool(app.config["LIQPAY_PUBLIC_KEY"] and app.config["LIQPAY_PRIVATE_KEY"])


def liqpay_data(params: dict) -> str:
    json_string = json.dumps(params, ensure_ascii=False, separators=(",", ":"))
    return base64.b64encode(json_string.encode("utf-8")).decode("ascii")


def liqpay_signature(data: str) -> str:
    private_key = app.config["LIQPAY_PRIVATE_KEY"]
    sign_string = f"{private_key}{data}{private_key}".encode("utf-8")
    return base64.b64encode(hashlib.sha3_256(sign_string).digest()).decode("ascii")


def decode_liqpay_data(data: str) -> dict:
    decoded = base64.b64decode(data).decode("utf-8")
    return json.loads(decoded)


def payable_amount(order: sqlite3.Row) -> float:
    return round(float(order["final_price"] if order["final_price"] is not None else order["estimated_price"] or 0), 2)


def payment_status_label(status: str | None) -> str:
    mapping = {
        "created": "Створено",
        "success": "Успішно",
        "sandbox": "Успішно",
        "wait_secure": "Очікує підтвердження",
        "wait_accept": "Очікує підтвердження",
        "processing": "В обробці",
        "prepared": "Підготовлено",
        "sandbox_confirmed": "Оплачено",
        "failure": "Помилка",
        "error": "Помилка",
        "reversed": "Повернено",
        "amount_mismatch": "Сума не збігається",
        "invalid_signature": "Некоректний підпис",
    }
    return mapping.get(status or "", status or "—")


def payment_status_class(status: str | None) -> str:
    if status in LIQPAY_SUCCESS_STATUSES or status == "sandbox_confirmed":
        return "success"
    if status in {"failure", "error", "reversed", "amount_mismatch", "invalid_signature"}:
        return "danger"
    if status in {"wait_secure", "wait_accept", "processing", "prepared", "created"}:
        return "warning"
    return "default"


def latest_payments(order_id: int) -> list[sqlite3.Row]:
    return db_all(
        """
        SELECT * FROM payments
        WHERE order_id = ?
        ORDER BY created_at DESC, id DESC
        LIMIT 6
        """,
        (order_id,),
    )


def current_user() -> sqlite3.Row | None:
    user_id = session.get("user_id")
    if not user_id:
        return None
    return db_one("SELECT * FROM users WHERE id = ?", (user_id,))


@app.before_request
def load_logged_in_user():
    g.user = current_user()
    if g.user and g.user["is_blocked"] and request.endpoint not in {"logout", "static"}:
        session.clear()
        g.user = None
        flash("Ваш обліковий запис заблоковано адміністратором.", "danger")
        return redirect(url_for("login"))
    return None


@app.context_processor
def inject_globals() -> dict:
    return {
        "current_user": getattr(g, "user", None),
        "roles": ROLES,
        "specializations": SPECIALIZATIONS,
        "order_statuses": ORDER_STATUSES,
        "worker_status_options": WORKER_STATUS_OPTIONS,
        "format_datetime": format_datetime,
        "money": money,
        "role_label": role_label,
        "spec_label": spec_label,
        "active_label": active_label,
        "status_class": status_class,
        "payment_status_label": payment_status_label,
        "payment_status_class": payment_status_class,
    }


def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if g.user is None:
            flash("Спочатку увійдіть у систему.", "warning")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped_view


def role_required(*roles_required: str):
    def decorator(view):
        @wraps(view)
        def wrapped_view(*args, **kwargs):
            if g.user is None:
                flash("Спочатку увійдіть у систему.", "warning")
                return redirect(url_for("login", next=request.path))
            if g.user["role"] not in roles_required:
                abort(403)
            return view(*args, **kwargs)

        return wrapped_view

    return decorator


def add_status_history(order_id: int, status: str, comment: str = "") -> None:
    changed_by = g.user["id"] if getattr(g, "user", None) else None
    get_db().execute(
        """
        INSERT INTO order_status_history (order_id, status, changed_by_user_id, comment)
        VALUES (?, ?, ?, ?)
        """,
        (order_id, status, changed_by, comment),
    )


def update_order_status(order_id: int, new_status: str, comment: str = "") -> None:
    if new_status not in ORDER_STATUSES:
        raise ValueError("Невідомий статус")
    order = db_one("SELECT status FROM orders WHERE id = ?", (order_id,))
    if not order:
        abort(404)
    if order["status"] != new_status:
        get_db().execute(
            "UPDATE orders SET status = ?, updated_at = ? WHERE id = ?",
            (new_status, now_iso(), order_id),
        )
        add_status_history(order_id, new_status, comment)


def get_service_with_price(service_id: int) -> sqlite3.Row | None:
    return db_one(
        """
        SELECT s.*, c.name AS category_name, c.slug AS category_slug,
               p.base_price, p.unit_price, p.unit_name,
               p.extra_option_price, p.urgent_multiplier
        FROM services s
        JOIN categories c ON c.id = s.category_id
        LEFT JOIN prices p ON p.service_id = s.id AND p.is_active = 1
        WHERE s.id = ?
        ORDER BY p.updated_at DESC
        LIMIT 1
        """,
        (service_id,),
    )


def calculate_estimate(service_id: int, form) -> tuple[float, list[str], list[str]]:
    service = get_service_with_price(service_id)
    errors: list[str] = []
    details: list[str] = []
    if not service:
        return 0, ["Послугу не знайдено."], []

    base = float(service["base_price"] or 0)
    unit = float(service["unit_price"] or 0)
    extra = float(service["extra_option_price"] or 0)
    urgent_multiplier = float(service["urgent_multiplier"] or 1)
    category_slug = service["category_slug"]
    total = base
    details.append(f"База: {money(base)}")

    if category_slug == "cleaning":
        area = to_float(form.get("cleaning_area"))
        rooms = to_int(form.get("rooms"))
        selected_options = form.getlist("extra_options") if hasattr(form, "getlist") else []
        if area <= 0:
            errors.append("Для клінінгу вкажіть площу більше 0 м².")
        if rooms <= 0:
            errors.append("Для клінінгу вкажіть кількість кімнат.")
        area_part = unit * area
        rooms_part = max(0, rooms - 1) * 50
        extras_part = len(selected_options) * extra
        total += area_part + rooms_part + extras_part
        details.extend(
            [
                f"Площа: {area:g} м² × {money(unit)} = {money(area_part)}",
                f"Кімнати: доплата {money(rooms_part)}",
                f"Додаткові опції: {len(selected_options)} × {money(extra)} = {money(extras_part)}",
            ]
        )
    elif category_slug == "repair":
        repair_type = (form.get("repair_type") or "").strip()
        hours = to_float(form.get("repair_hours"), 1)
        urgency = form.get("urgency") or "normal"
        if not repair_type:
            errors.append("Для ремонту вкажіть тип роботи.")
        if hours <= 0:
            errors.append("Для ремонту вкажіть орієнтовну кількість годин.")
        hourly_part = unit * hours
        total += hourly_part
        details.append(f"Робота: {hours:g} год × {money(unit)} = {money(hourly_part)}")
        if urgency == "urgent":
            before = total
            total *= urgent_multiplier
            details.append(f"Терміновість: множник ×{urgent_multiplier:g}, доплата {money(total - before)}")
    elif category_slug == "delivery":
        delivery_from = (form.get("delivery_from") or "").strip()
        delivery_to = (form.get("delivery_to") or "").strip()
        distance = to_float(form.get("delivery_distance_km"))
        weight = to_float(form.get("weight"))
        if not delivery_from or not delivery_to:
            errors.append("Для доставки вкажіть адресу звідки та куди.")
        if distance <= 0:
            errors.append("Для доставки вкажіть орієнтовну відстань у км.")
        distance_part = unit * distance
        weight_part = max(0, weight - 5) * 12
        total += distance_part + weight_part
        details.extend(
            [
                f"Відстань: {distance:g} км × {money(unit)} = {money(distance_part)}",
                f"Вага: доплата за понад 5 кг = {money(weight_part)}",
            ]
        )
    else:
        qty = to_float(form.get("quantity"), 1)
        total += unit * qty
        details.append(f"Одиниці: {qty:g} × {money(unit)}")

    return round(max(total, 0), 2), errors, details


def order_detail_query(order_id: int) -> sqlite3.Row | None:
    return db_one(
        """
        SELECT o.*, c.name AS category_name, c.slug AS category_slug,
               s.name AS service_name, s.unit_label,
               client.full_name AS client_name, client.email AS client_email,
               client.phone AS client_phone,
               worker.full_name AS worker_name, worker.email AS worker_email,
               worker.phone AS worker_phone, worker.specialization AS worker_specialization,
               worker.city AS worker_city, worker.service_area AS worker_area
        FROM orders o
        JOIN categories c ON c.id = o.category_id
        JOIN services s ON s.id = o.service_id
        JOIN users client ON client.id = o.client_id
        LEFT JOIN users worker ON worker.id = o.worker_id
        WHERE o.id = ?
        """,
        (order_id,),
    )


def validate_basic_order_fields(form) -> list[str]:
    errors: list[str] = []
    required = {
        "category_id": "категорію",
        "service_id": "підпослугу",
        "city": "місто",
        "street": "вулицю",
        "building": "будинок",
        "preferred_at": "дату та час",
    }
    for field, label in required.items():
        if not (form.get(field) or "").strip():
            errors.append(f"Вкажіть {label}.")
    if form.get("preferred_at") and normalize_datetime_local(form.get("preferred_at")) is None:
        errors.append("Дата і час мають бути у коректному форматі.")
    return errors


def seed_database() -> None:
    db = get_db()
    if db.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0:
        return

    users = [
        ("admin", "Адміністратор", "admin@example.com", "admin123", "+380000000001", "Ужгород", "", None, "", "Пн-Пт 09:00-18:00", ""),
        ("client", "Іван Коваленко", "client@example.com", "client123", "+380501112233", "Ужгород", "вул. Собранецька, 20", None, "", "", ""),
        ("worker", "Олена Клименко", "worker_clean@example.com", "worker123", "+380671112244", "Ужгород", "", "cleaning", "Ужгород і передмістя", "Пн-Сб 08:00-19:00", "від 450 грн"),
        ("worker", "Петро Майстер", "worker_repair@example.com", "worker123", "+380931234567", "Ужгород", "", "repair", "Ужгород", "Пн-Пт 10:00-20:00", "від 600 грн"),
        ("worker", "Марко Кур'єр", "worker_delivery@example.com", "worker123", "+380991234567", "Ужгород", "", "delivery", "Місто та область", "Щодня 09:00-21:00", "від 120 грн"),
    ]
    for role, full_name, email, password, phone, city, address, specialization, area, schedule, price_text in users:
        db.execute(
            """
            INSERT INTO users (role, full_name, email, password_hash, phone, city, address,
                               specialization, service_area, schedule, price_text)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                role,
                full_name,
                email,
                generate_password_hash(password),
                phone,
                city,
                address,
                specialization,
                area,
                schedule,
                price_text,
            ),
        )

    categories = [
        ("Клінінг", "cleaning", "Прибирання квартир, будинків та офісів із додатковими опціями."),
        ("Ремонт", "repair", "Побутовий ремонт: сантехніка, електрика, меблі та дрібні роботи."),
        ("Доставка", "delivery", "Кур'єрська доставка документів, покупок та габаритних речей."),
    ]
    for name, slug, description in categories:
        db.execute(
            "INSERT INTO categories (name, slug, description) VALUES (?, ?, ?)",
            (name, slug, description),
        )

    category_ids = {row["slug"]: row["id"] for row in db.execute("SELECT id, slug FROM categories")}
    services = [
        ("cleaning", "Генеральне прибирання", "general-cleaning", "Комплексне прибирання житла з кухнею та санвузлом.", "м²", 500, 18, "м²", 120, 1.0),
        ("cleaning", "Прибирання після ремонту", "after-renovation", "Вивіз будівельного пилу, миття поверхонь, порядок після робіт.", "м²", 800, 28, "м²", 160, 1.0),
        ("cleaning", "Миття вікон", "window-cleaning", "Миття вікон, рам та підвіконь.", "м²", 300, 35, "м²", 80, 1.0),
        ("repair", "Сантехнічні роботи", "plumbing", "Заміна змішувача, усунення протікань, монтаж сантехніки.", "год", 550, 280, "год", 0, 1.35),
        ("repair", "Електрика", "electric", "Розетки, вимикачі, світильники та дрібний електромонтаж.", "год", 600, 300, "год", 0, 1.4),
        ("repair", "Збірка меблів", "furniture", "Збірка шаф, столів, ліжок та кухонних модулів.", "год", 450, 220, "год", 0, 1.25),
        ("delivery", "Кур'єрська доставка", "courier", "Доставка малих посилок у межах міста.", "км", 90, 18, "км", 0, 1.0),
        ("delivery", "Доставка габаритів", "cargo", "Доставка меблів, техніки та великих речей.", "км", 250, 35, "км", 0, 1.0),
        ("delivery", "Доставка документів", "documents", "Швидка доставка документів та дрібних пакетів.", "км", 70, 15, "км", 0, 1.0),
    ]
    for category_slug, name, slug, description, unit_label, base, unit, unit_name, extra, multiplier in services:
        cursor = db.execute(
            """
            INSERT INTO services (category_id, name, slug, description, unit_label)
            VALUES (?, ?, ?, ?, ?)
            """,
            (category_ids[category_slug], name, slug, description, unit_label),
        )
        service_id = cursor.lastrowid
        db.execute(
            """
            INSERT INTO prices (service_id, base_price, unit_price, unit_name, extra_option_price, urgent_multiplier)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (service_id, base, unit, unit_name, extra, multiplier),
        )

    client_id = db.execute("SELECT id FROM users WHERE email = 'client@example.com'").fetchone()[0]
    cleaning_worker_id = db.execute("SELECT id FROM users WHERE email = 'worker_clean@example.com'").fetchone()[0]
    cleaning_category = category_ids["cleaning"]
    cleaning_service = db.execute("SELECT id FROM services WHERE slug = 'general-cleaning'").fetchone()[0]
    order_cursor = db.execute(
        """
        INSERT INTO orders (client_id, worker_id, category_id, service_id, city, street, building,
                            apartment, preferred_at, description, cleaning_area, rooms, extra_options,
                            estimated_price, final_price, status, is_paid)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            client_id,
            cleaning_worker_id,
            cleaning_category,
            cleaning_service,
            "Ужгород",
            "вул. Корзо",
            "12",
            "5",
            "2026-05-18 11:00:00",
            "Потрібне генеральне прибирання квартири перед заселенням.",
            55,
            2,
            "Миття вікон, Холодильник",
            1690,
            1690,
            "Прийнято виконавцем",
            0,
        ),
    )
    order_id = order_cursor.lastrowid
    db.execute(
        "INSERT INTO order_status_history (order_id, status, changed_by_user_id, comment) VALUES (?, ?, ?, ?)",
        (order_id, "Нове", client_id, "Замовлення створено клієнтом."),
    )
    db.execute(
        "INSERT INTO order_status_history (order_id, status, changed_by_user_id, comment) VALUES (?, ?, ?, ?)",
        (order_id, "Прийнято виконавцем", cleaning_worker_id, "Виконавець прийняв заявку."),
    )

    completed_cursor = db.execute(
        """
        INSERT INTO orders (client_id, worker_id, category_id, service_id, city, street, building,
                            apartment, preferred_at, description, cleaning_area, rooms, extra_options,
                            estimated_price, final_price, status, is_paid, paid_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            client_id,
            cleaning_worker_id,
            cleaning_category,
            cleaning_service,
            "Ужгород",
            "вул. Мукачівська",
            "8",
            "21",
            "2026-05-02 10:30:00",
            "Генеральне прибирання після переїзду.",
            42,
            2,
            "Миття вікон",
            1376,
            1376,
            "Виконано",
            1,
            "2026-05-02 14:20:00",
        ),
    )
    completed_id = completed_cursor.lastrowid
    for status, user_id, comment in [
        ("Нове", client_id, "Замовлення створено клієнтом."),
        ("Прийнято виконавцем", cleaning_worker_id, "Виконавець прийняв заявку."),
        ("В дорозі / В роботі", cleaning_worker_id, "Роботу розпочато."),
        ("Виконано", cleaning_worker_id, "Замовлення виконано."),
    ]:
        db.execute(
            "INSERT INTO order_status_history (order_id, status, changed_by_user_id, comment) VALUES (?, ?, ?, ?)",
            (completed_id, status, user_id, comment),
        )
    db.execute(
        """
        INSERT INTO payments (order_id, provider, provider_order_id, provider_payment_id, amount, currency, status, raw_response)
        VALUES (?, 'liqpay', ?, ?, ?, 'UAH', 'success', ?)
        """,
        (
            completed_id,
            f"seed-order-{completed_id}",
            "seed-payment",
            1376,
            json.dumps({"status": "success", "amount": 1376, "currency": "UAH"}, ensure_ascii=False),
        ),
    )
    db.execute(
        "INSERT INTO reviews (order_id, client_id, worker_id, rating, text) VALUES (?, ?, ?, ?, ?)",
        (completed_id, client_id, cleaning_worker_id, 5, "Швидко, охайно і без перенесення часу."),
    )
    db.commit()


def init_database(reset: bool = False) -> None:
    if reset and DATABASE.exists():
        DATABASE.unlink()
    with app.app_context():
        get_db().executescript(SCHEMA_SQL)
        seed_database()
        get_db().commit()


@app.cli.command("init-db")
@click.option("--reset", is_flag=True, help="Видалити стару SQLite БД і створити seed-дані заново.")
def init_db_command(reset: bool) -> None:
    init_database(reset=reset)
    click.echo("Базу даних SQLite підготовлено.")


@app.route("/uploads/<path:filename>")
@login_required
def uploaded_file(filename: str):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)


@app.route("/")
def home():
    categories = db_all("SELECT * FROM categories WHERE is_active = 1 ORDER BY name")
    reviews = db_all(
        """
        SELECT r.*, u.full_name AS client_name, w.full_name AS worker_name, s.name AS service_name
        FROM reviews r
        JOIN users u ON u.id = r.client_id
        LEFT JOIN users w ON w.id = r.worker_id
        JOIN orders o ON o.id = r.order_id
        JOIN services s ON s.id = o.service_id
        WHERE r.is_visible = 1
        ORDER BY r.created_at DESC
        LIMIT 3
        """
    )
    stats = {
        "orders": db_one("SELECT COUNT(*) AS cnt FROM orders")["cnt"],
        "workers": db_one("SELECT COUNT(*) AS cnt FROM users WHERE role = 'worker'")["cnt"],
        "services": db_one("SELECT COUNT(*) AS cnt FROM services WHERE is_active = 1")["cnt"],
    }
    top_workers = db_all(
        """
        SELECT
            w.id,
            w.full_name,
            w.specialization,
            w.service_area,
            (SELECT COUNT(*) FROM reviews r WHERE r.worker_id = w.id AND r.is_visible = 1) AS reviews_count,
            (SELECT ROUND(AVG(r.rating), 1) FROM reviews r WHERE r.worker_id = w.id AND r.is_visible = 1) AS avg_rating,
            (SELECT COUNT(*) FROM orders o WHERE o.worker_id = w.id AND o.status = 'Виконано') AS completed_orders
        FROM users w
        WHERE w.role = 'worker' AND w.is_blocked = 0
        ORDER BY (avg_rating IS NULL), avg_rating DESC, completed_orders DESC, w.full_name ASC
        LIMIT 3
        """
    )
    return render_template("home.html", categories=categories, reviews=reviews, stats=stats, top_workers=top_workers)


@app.route("/catalog")
def catalog():
    q = (request.args.get("q") or "").strip()
    category = request.args.get("category") or ""
    params: list = []
    where = ["s.is_active = 1", "c.is_active = 1"]
    if q:
        where.append("(LOWER(s.name) LIKE ? OR LOWER(s.description) LIKE ? OR LOWER(c.name) LIKE ?)")
        pattern = f"%{q.lower()}%"
        params.extend([pattern, pattern, pattern])
    if category:
        where.append("c.slug = ?")
        params.append(category)
    services = db_all(
        f"""
        SELECT s.*, c.name AS category_name, c.slug AS category_slug,
               p.base_price, p.unit_price, p.unit_name
        FROM services s
        JOIN categories c ON c.id = s.category_id
        LEFT JOIN prices p ON p.service_id = s.id AND p.is_active = 1
        WHERE {' AND '.join(where)}
        ORDER BY c.name, s.name
        """,
        tuple(params),
    )
    categories = db_all("SELECT * FROM categories WHERE is_active = 1 ORDER BY name")
    return render_template("catalog.html", services=services, categories=categories, q=q, selected_category=category)


@app.route("/prices")
def prices_public():
    price_rows = db_all(
        """
        SELECT p.*, s.name AS service_name, c.name AS category_name, c.slug AS category_slug
        FROM prices p
        JOIN services s ON s.id = p.service_id
        JOIN categories c ON c.id = s.category_id
        WHERE p.is_active = 1 AND s.is_active = 1 AND c.is_active = 1
        ORDER BY c.name, s.name
        """
    )
    return render_template("prices.html", price_rows=price_rows)


@app.route("/contacts")
def contacts():
    return render_template("contacts.html")


@app.route("/faq")
def faq():
    return render_template("faq.html")


@app.route("/register", methods=("GET", "POST"))
def register():
    if g.user:
        return redirect(url_for(f"{g.user['role']}_dashboard"))
    if request.method == "POST":
        role = request.form.get("role")
        full_name = (request.form.get("full_name") or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        phone = (request.form.get("phone") or "").strip()
        city = (request.form.get("city") or "").strip()
        address = (request.form.get("address") or "").strip()
        password = request.form.get("password") or ""
        password_confirm = request.form.get("password_confirm") or ""
        specialization = request.form.get("specialization") if role == "worker" else None
        service_area = (request.form.get("service_area") or "").strip() if role == "worker" else ""
        schedule = (request.form.get("schedule") or "").strip() if role == "worker" else ""
        price_text = (request.form.get("price_text") or "").strip() if role == "worker" else ""

        errors = []
        if role not in {"client", "worker"}:
            errors.append("Оберіть роль клієнта або виконавця.")
        if not full_name:
            errors.append("Вкажіть ПІБ.")
        if not email or "@" not in email:
            errors.append("Вкажіть коректний email.")
        if len(password) < 6:
            errors.append("Пароль має містити щонайменше 6 символів.")
        if password != password_confirm:
            errors.append("Паролі не збігаються.")
        if role == "worker" and specialization not in SPECIALIZATIONS:
            errors.append("Для виконавця оберіть спеціалізацію.")
        if db_one("SELECT id FROM users WHERE email = ?", (email,)):
            errors.append("Користувач із таким email вже існує.")

        if errors:
            for error in errors:
                flash(error, "danger")
            return render_template("auth/register.html", form=request.form)

        cursor = get_db().execute(
            """
            INSERT INTO users (role, full_name, email, password_hash, phone, city, address,
                               specialization, service_area, schedule, price_text)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                role,
                full_name,
                email,
                generate_password_hash(password),
                phone,
                city,
                address,
                specialization,
                service_area,
                schedule,
                price_text,
            ),
        )
        get_db().commit()
        session.clear()
        session["user_id"] = cursor.lastrowid
        flash("Реєстрацію завершено. Ласкаво просимо!", "success")
        return redirect(url_for(f"{role}_dashboard"))
    return render_template("auth/register.html", form={})


@app.route("/login", methods=("GET", "POST"))
def login():
    if g.user:
        return redirect(url_for(f"{g.user['role']}_dashboard"))
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        user = db_one("SELECT * FROM users WHERE email = ?", (email,))
        if user is None or not check_password_hash(user["password_hash"], password):
            flash("Невірний email або пароль.", "danger")
            return render_template("auth/login.html", email=email)
        if user["is_blocked"]:
            flash("Ваш обліковий запис заблоковано.", "danger")
            return render_template("auth/login.html", email=email)
        session.clear()
        session["user_id"] = user["id"]
        flash("Вхід виконано успішно.", "success")
        next_url = request.args.get("next")
        if next_url and next_url.startswith("/"):
            return redirect(next_url)
        return redirect(url_for(f"{user['role']}_dashboard"))
    return render_template("auth/login.html", email="")


@app.route("/logout")
def logout():
    session.clear()
    flash("Ви вийшли із системи.", "info")
    return redirect(url_for("home"))


@app.post("/api/price-preview")
@login_required
def price_preview():
    service_id = to_int(request.form.get("service_id"))
    price, errors, details = calculate_estimate(service_id, request.form)
    if errors:
        return jsonify({"ok": False, "errors": errors}), 400
    return jsonify({"ok": True, "price": price, "price_text": money(price), "details": details})


@app.route("/client")
@role_required("client")
def client_dashboard():
    user_id = g.user["id"]
    stats = {
        "all": db_one("SELECT COUNT(*) AS cnt FROM orders WHERE client_id = ?", (user_id,))["cnt"],
        "active": db_one(
            "SELECT COUNT(*) AS cnt FROM orders WHERE client_id = ? AND status NOT IN ('Виконано', 'Скасовано')",
            (user_id,),
        )["cnt"],
        "done": db_one("SELECT COUNT(*) AS cnt FROM orders WHERE client_id = ? AND status = 'Виконано'", (user_id,))["cnt"],
    }
    recent_orders = db_all(
        """
        SELECT o.*, s.name AS service_name, c.name AS category_name, w.full_name AS worker_name
        FROM orders o
        JOIN services s ON s.id = o.service_id
        JOIN categories c ON c.id = o.category_id
        LEFT JOIN users w ON w.id = o.worker_id
        WHERE o.client_id = ?
        ORDER BY o.created_at DESC
        LIMIT 5
        """,
        (user_id,),
    )
    return render_template("client/dashboard.html", stats=stats, recent_orders=recent_orders)


@app.route("/client/profile", methods=("GET", "POST"))
@role_required("client")
def client_profile():
    if request.method == "POST":
        full_name = (request.form.get("full_name") or "").strip()
        phone = (request.form.get("phone") or "").strip()
        city = (request.form.get("city") or "").strip()
        address = (request.form.get("address") or "").strip()
        if not full_name:
            flash("ПІБ є обов'язковим.", "danger")
        else:
            get_db().execute(
                "UPDATE users SET full_name = ?, phone = ?, city = ?, address = ? WHERE id = ?",
                (full_name, phone, city, address, g.user["id"]),
            )
            get_db().commit()
            flash("Профіль оновлено.", "success")
            return redirect(url_for("client_profile"))
    return render_template("client/profile.html")


@app.route("/client/orders/new", methods=("GET", "POST"))
@role_required("client")
def client_create_order():
    categories = db_all("SELECT * FROM categories WHERE is_active = 1 ORDER BY name")
    services = db_all(
        """
        SELECT s.*, c.slug AS category_slug, c.name AS category_name
        FROM services s
        JOIN categories c ON c.id = s.category_id
        WHERE s.is_active = 1 AND c.is_active = 1
        ORDER BY c.name, s.name
        """
    )
    if request.method == "POST":
        errors = validate_basic_order_fields(request.form)
        category_id = to_int(request.form.get("category_id"))
        service_id = to_int(request.form.get("service_id"))
        service = get_service_with_price(service_id)
        if not service:
            errors.append("Оберіть доступну підпослугу.")
        elif service["category_id"] != category_id:
            errors.append("Підпослуга не відповідає обраній категорії.")
        estimate, price_errors, _details = calculate_estimate(service_id, request.form)
        errors.extend(price_errors)

        photo_filename = None
        uploaded = request.files.get("photo")
        if uploaded and uploaded.filename:
            if not allowed_image(uploaded.filename):
                errors.append("Фото має бути у форматі JPG, PNG або WEBP.")
            else:
                safe_name = secure_filename(uploaded.filename)
                photo_filename = f"{uuid4().hex}_{safe_name}"

        if errors:
            for error in errors:
                flash(error, "danger")
            return render_template("client/create_order.html", categories=categories, services=services, form=request.form)

        if uploaded and uploaded.filename and photo_filename:
            uploaded.save(Path(app.config["UPLOAD_FOLDER"]) / photo_filename)

        extra_options = ", ".join(request.form.getlist("extra_options"))
        cursor = get_db().execute(
            """
            INSERT INTO orders (
                client_id, category_id, service_id, city, street, building, apartment, preferred_at,
                description, cleaning_area, rooms, extra_options, repair_type, repair_hours, urgency,
                photo_filename, delivery_from, delivery_to, delivery_distance_km, weight, dimensions,
                estimated_price, status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                g.user["id"],
                category_id,
                service_id,
                request.form.get("city", "").strip(),
                request.form.get("street", "").strip(),
                request.form.get("building", "").strip(),
                request.form.get("apartment", "").strip(),
                normalize_datetime_local(request.form.get("preferred_at")),
                request.form.get("description", "").strip(),
                to_float(request.form.get("cleaning_area")) or None,
                to_int(request.form.get("rooms")) or None,
                extra_options,
                request.form.get("repair_type", "").strip(),
                to_float(request.form.get("repair_hours")) or None,
                request.form.get("urgency") or "normal",
                photo_filename,
                request.form.get("delivery_from", "").strip(),
                request.form.get("delivery_to", "").strip(),
                to_float(request.form.get("delivery_distance_km")) or None,
                to_float(request.form.get("weight")) or None,
                request.form.get("dimensions", "").strip(),
                estimate,
                "Нове",
            ),
        )
        order_id = cursor.lastrowid
        add_status_history(order_id, "Нове", "Клієнт створив замовлення.")
        get_db().commit()
        flash(f"Замовлення #{order_id} створено. Орієнтовна вартість: {money(estimate)}.", "success")
        return redirect(url_for("client_order_detail", order_id=order_id))
    return render_template("client/create_order.html", categories=categories, services=services, form={})


@app.route("/client/orders")
@role_required("client")
def client_orders():
    status = request.args.get("status") or ""
    sort = request.args.get("sort") or "new"
    where = ["o.client_id = ?"]
    params: list = [g.user["id"]]
    if status:
        where.append("o.status = ?")
        params.append(status)
    order_by = "o.created_at DESC" if sort != "old" else "o.created_at ASC"
    orders = db_all(
        f"""
        SELECT o.*, s.name AS service_name, c.name AS category_name, w.full_name AS worker_name
        FROM orders o
        JOIN services s ON s.id = o.service_id
        JOIN categories c ON c.id = o.category_id
        LEFT JOIN users w ON w.id = o.worker_id
        WHERE {' AND '.join(where)}
        ORDER BY {order_by}
        """,
        tuple(params),
    )
    return render_template("client/orders.html", orders=orders, selected_status=status, sort=sort)


@app.route("/client/orders/<int:order_id>")
@role_required("client")
def client_order_detail(order_id: int):
    order = order_detail_query(order_id)
    if not order or order["client_id"] != g.user["id"]:
        abort(404)
    history = db_all(
        """
        SELECT h.*, u.full_name AS user_name
        FROM order_status_history h
        LEFT JOIN users u ON u.id = h.changed_by_user_id
        WHERE h.order_id = ?
        ORDER BY h.created_at DESC
        """,
        (order_id,),
    )
    review = db_one("SELECT * FROM reviews WHERE order_id = ?", (order_id,))
    payments = latest_payments(order_id)
    return render_template("client/order_detail.html", order=order, history=history, review=review, payments=payments)


@app.post("/client/orders/<int:order_id>/pay")
@role_required("client")
def client_pay_order(order_id: int):
    order = order_detail_query(order_id)
    if not order or order["client_id"] != g.user["id"]:
        abort(404)
    if order["status"] == "Скасовано":
        flash("Скасоване замовлення оплатити не можна.", "danger")
        return redirect(url_for("client_order_detail", order_id=order_id))
    if order["is_paid"]:
        flash("Замовлення вже оплачено.", "info")
        return redirect(url_for("client_order_detail", order_id=order_id))
    if not liqpay_is_configured():
        flash("Оплата тимчасово недоступна. Потрібно налаштувати платіжні ключі.", "danger")
        return redirect(url_for("client_order_detail", order_id=order_id))

    amount = payable_amount(order)
    if amount <= 0:
        flash("Для цього замовлення неможливо сформувати платіж.", "danger")
        return redirect(url_for("client_order_detail", order_id=order_id))

    provider_order_id = f"servicehub-{order_id}-{uuid4().hex[:12]}"
    params = {
        "public_key": app.config["LIQPAY_PUBLIC_KEY"],
        "version": 7,
        "action": "pay",
        "amount": f"{amount:.2f}",
        "currency": "UAH",
        "description": f"Оплата замовлення #{order_id} у ServiceHub",
        "order_id": provider_order_id,
        "result_url": public_url_for("payment_return", order_id=order_id),
        "server_url": public_url_for("liqpay_callback"),
        "language": "uk",
    }
    if app.config["LIQPAY_SANDBOX"]:
        params["sandbox"] = 1

    data = liqpay_data(params)
    signature = liqpay_signature(data)
    cur = get_db().execute(
        """
        INSERT INTO payments (order_id, provider, provider_order_id, amount, currency, status, checkout_data, checkout_signature)
        VALUES (?, 'liqpay', ?, ?, 'UAH', 'prepared', ?, ?)
        """,
        (order_id, provider_order_id, amount, data, signature),
    )
    payment_id = cur.lastrowid
    get_db().commit()

    if app.config["LIQPAY_SANDBOX"]:
        return render_template(
            "client/payment_sandbox.html",
            order=order,
            amount=amount,
            payment_id=payment_id,
        )

    return render_template(
        "client/payment_redirect.html",
        order=order,
        amount=amount,
        checkout_url=LIQPAY_CHECKOUT_URL,
        data=data,
        signature=signature,
    )


@app.post("/client/orders/<int:order_id>/payments/<int:payment_id>/confirm")
@role_required("client")
def client_confirm_sandbox_payment(order_id: int, payment_id: int):
    if not app.config["LIQPAY_SANDBOX"]:
        abort(404)

    order = db_one("SELECT * FROM orders WHERE id = ? AND client_id = ?", (order_id, g.user["id"]))
    if not order:
        abort(404)
    if order["is_paid"]:
        flash("Замовлення вже оплачено.", "info")
        return redirect(url_for("client_order_detail", order_id=order_id))

    payment = db_one("SELECT * FROM payments WHERE id = ? AND order_id = ?", (payment_id, order_id))
    if not payment:
        abort(404)

    payload = {
        "status": "sandbox",
        "payment_id": f"sandbox-{payment_id}",
        "order_id": payment["provider_order_id"],
        "amount": f"{float(payment['amount'] or 0):.2f}",
        "currency": payment["currency"],
        "created_at": now_iso(),
    }

    get_db().execute(
        "UPDATE orders SET is_paid = 1, paid_at = ?, updated_at = ? WHERE id = ?",
        (now_iso(), now_iso(), order_id),
    )
    get_db().execute(
        """
        UPDATE payments
        SET status = 'sandbox_confirmed', provider_payment_id = ?, raw_response = ?, updated_at = ?
        WHERE id = ?
        """,
        (payload["payment_id"], json.dumps(payload, ensure_ascii=False), now_iso(), payment_id),
    )
    add_status_history(order_id, order["status"], "Оплату підтверджено.")
    get_db().commit()
    flash("Оплату підтверджено.", "success")
    return redirect(url_for("client_order_detail", order_id=order_id))


@app.route("/client/orders/<int:order_id>/payment-return")
@role_required("client")
def payment_return(order_id: int):
    order = db_one("SELECT * FROM orders WHERE id = ? AND client_id = ?", (order_id, g.user["id"]))
    if not order:
        abort(404)
    if order["is_paid"]:
        flash("Оплату підтверджено.", "success")
    else:
        flash("Платіж обробляється. Статус оновиться після підтвердження банком.", "info")
    return redirect(url_for("client_order_detail", order_id=order_id))


@app.post("/payments/liqpay/callback")
def liqpay_callback():
    data = request.form.get("data", "")
    signature = request.form.get("signature", "")
    if not data or not signature or not liqpay_is_configured():
        abort(400)

    expected_signature = liqpay_signature(data)
    if not hmac.compare_digest(signature, expected_signature):
        payload = {}
        try:
            payload = decode_liqpay_data(data)
        except Exception:
            pass
        provider_order_id = payload.get("order_id") if isinstance(payload, dict) else None
        if provider_order_id:
            get_db().execute(
                "UPDATE payments SET status = 'invalid_signature', raw_response = ?, updated_at = ? WHERE provider_order_id = ?",
                (json.dumps(payload, ensure_ascii=False), now_iso(), provider_order_id),
            )
            get_db().commit()
        abort(400)

    try:
        payload = decode_liqpay_data(data)
    except Exception:
        abort(400)

    provider_order_id = payload.get("order_id")
    if not provider_order_id:
        abort(400)
    payment = db_one("SELECT * FROM payments WHERE provider_order_id = ?", (provider_order_id,))
    if not payment:
        return "unknown payment", 200

    liqpay_status = str(payload.get("status") or "processing")
    provider_payment_id = payload.get("payment_id") or payload.get("liqpay_order_id")
    amount = to_float(str(payload.get("amount") or "0"))
    raw_response = json.dumps(payload, ensure_ascii=False)
    stored_status = liqpay_status

    order = db_one("SELECT * FROM orders WHERE id = ?", (payment["order_id"],))
    if order and liqpay_status in LIQPAY_SUCCESS_STATUSES:
        expected_amount = float(payment["amount"] or 0)
        if abs(amount - expected_amount) <= 0.01:
            if not order["is_paid"]:
                get_db().execute(
                    "UPDATE orders SET is_paid = 1, paid_at = ?, updated_at = ? WHERE id = ?",
                    (now_iso(), now_iso(), order["id"]),
                )
                add_status_history(order["id"], order["status"], "Оплату підтверджено платіжною системою.")
        else:
            stored_status = "amount_mismatch"

    get_db().execute(
        """
        UPDATE payments
        SET status = ?, provider_payment_id = ?, raw_response = ?, updated_at = ?
        WHERE id = ?
        """,
        (stored_status, str(provider_payment_id or ""), raw_response, now_iso(), payment["id"]),
    )
    get_db().commit()
    return "ok", 200


@app.post("/client/orders/<int:order_id>/cancel")
@role_required("client")
def client_cancel_order(order_id: int):
    order = db_one("SELECT * FROM orders WHERE id = ? AND client_id = ?", (order_id, g.user["id"]))
    if not order:
        abort(404)
    if order["status"] in {"Виконано", "Скасовано"}:
        flash("Це замовлення вже неможливо скасувати.", "danger")
    else:
        update_order_status(order_id, "Скасовано", "Клієнт скасував замовлення.")
        get_db().commit()
        flash("Замовлення скасовано.", "success")
    return redirect(url_for("client_order_detail", order_id=order_id))


@app.post("/client/orders/<int:order_id>/review")
@role_required("client")
def client_add_review(order_id: int):
    order = db_one("SELECT * FROM orders WHERE id = ? AND client_id = ?", (order_id, g.user["id"]))
    if not order:
        abort(404)
    if order["status"] != "Виконано":
        flash("Відгук можна залишити тільки після виконання замовлення.", "danger")
        return redirect(url_for("client_order_detail", order_id=order_id))
    if db_one("SELECT id FROM reviews WHERE order_id = ?", (order_id,)):
        flash("Для цього замовлення відгук уже залишено.", "warning")
        return redirect(url_for("client_order_detail", order_id=order_id))
    rating = to_int(request.form.get("rating"))
    text = (request.form.get("text") or "").strip()
    if rating < 1 or rating > 5 or not text:
        flash("Оберіть оцінку від 1 до 5 і напишіть текст відгуку.", "danger")
    else:
        get_db().execute(
            "INSERT INTO reviews (order_id, client_id, worker_id, rating, text) VALUES (?, ?, ?, ?, ?)",
            (order_id, g.user["id"], order["worker_id"], rating, text),
        )
        get_db().commit()
        flash("Відгук додано. Дякуємо!", "success")
    return redirect(url_for("client_order_detail", order_id=order_id))


@app.route("/client/reviews")
@role_required("client")
def client_reviews():
    reviews = db_all(
        """
        SELECT r.*, o.id AS order_number, s.name AS service_name, w.full_name AS worker_name
        FROM reviews r
        JOIN orders o ON o.id = r.order_id
        JOIN services s ON s.id = o.service_id
        LEFT JOIN users w ON w.id = r.worker_id
        WHERE r.client_id = ?
        ORDER BY r.created_at DESC
        """,
        (g.user["id"],),
    )
    return render_template("client/reviews.html", reviews=reviews)


@app.route("/worker")
@role_required("worker")
def worker_dashboard():
    user_id = g.user["id"]
    spec = g.user["specialization"]
    stats = {
        "available": db_one(
            """
            SELECT COUNT(*) AS cnt
            FROM orders o
            JOIN categories c ON c.id = o.category_id
            LEFT JOIN order_rejections r ON r.order_id = o.id AND r.worker_id = ?
            WHERE o.worker_id IS NULL AND o.status IN ('Нове', 'Очікує підтвердження')
              AND c.slug = ? AND r.id IS NULL
            """,
            (user_id, spec),
        )["cnt"],
        "active": db_one(
            "SELECT COUNT(*) AS cnt FROM orders WHERE worker_id = ? AND status NOT IN ('Виконано', 'Скасовано')",
            (user_id,),
        )["cnt"],
        "done": db_one("SELECT COUNT(*) AS cnt FROM orders WHERE worker_id = ? AND status = 'Виконано'", (user_id,))["cnt"],
    }
    assigned = db_all(
        """
        SELECT o.*, s.name AS service_name, c.name AS category_name, client.full_name AS client_name
        FROM orders o
        JOIN services s ON s.id = o.service_id
        JOIN categories c ON c.id = o.category_id
        JOIN users client ON client.id = o.client_id
        WHERE o.worker_id = ? AND o.status NOT IN ('Виконано', 'Скасовано')
        ORDER BY o.preferred_at ASC
        LIMIT 5
        """,
        (user_id,),
    )
    return render_template("worker/dashboard.html", stats=stats, assigned=assigned)


@app.route("/worker/profile", methods=("GET", "POST"))
@role_required("worker")
def worker_profile():
    if request.method == "POST":
        full_name = (request.form.get("full_name") or "").strip()
        phone = (request.form.get("phone") or "").strip()
        city = (request.form.get("city") or "").strip()
        specialization = request.form.get("specialization")
        service_area = (request.form.get("service_area") or "").strip()
        schedule = (request.form.get("schedule") or "").strip()
        price_text = (request.form.get("price_text") or "").strip()
        if not full_name or specialization not in SPECIALIZATIONS:
            flash("Вкажіть ПІБ і коректну спеціалізацію.", "danger")
        else:
            get_db().execute(
                """
                UPDATE users
                SET full_name = ?, phone = ?, city = ?, specialization = ?, service_area = ?, schedule = ?, price_text = ?
                WHERE id = ?
                """,
                (full_name, phone, city, specialization, service_area, schedule, price_text, g.user["id"]),
            )
            get_db().commit()
            flash("Профіль виконавця оновлено.", "success")
            return redirect(url_for("worker_profile"))
    return render_template("worker/profile.html")


def worker_available_where() -> tuple[str, list]:
    where = [
        "o.worker_id IS NULL",
        "o.status IN ('Нове', 'Очікує підтвердження')",
        "c.slug = ?",
        "r.id IS NULL",
    ]
    params: list = [g.user["specialization"]]
    return " AND ".join(where), params


@app.route("/worker/orders/available")
@role_required("worker")
def worker_available_orders():
    selected_status = request.args.get("status") or ""
    where, params = worker_available_where()
    if selected_status:
        where += " AND o.status = ?"
        params.append(selected_status)
    orders = db_all(
        f"""
        SELECT o.*, s.name AS service_name, c.name AS category_name, client.full_name AS client_name
        FROM orders o
        JOIN services s ON s.id = o.service_id
        JOIN categories c ON c.id = o.category_id
        JOIN users client ON client.id = o.client_id
        LEFT JOIN order_rejections r ON r.order_id = o.id AND r.worker_id = ?
        WHERE {where}
        ORDER BY o.created_at DESC
        """,
        tuple([g.user["id"]] + params),
    )
    return render_template("worker/available_orders.html", orders=orders, selected_status=selected_status)


@app.route("/worker/orders/assigned")
@role_required("worker")
def worker_assigned_orders():
    mode = request.args.get("mode") or "active"
    where = ["o.worker_id = ?"]
    params: list = [g.user["id"]]
    if mode == "done":
        where.append("o.status = 'Виконано'")
    elif mode == "work":
        where.append("o.status = 'В дорозі / В роботі'")
    elif mode == "all":
        pass
    else:
        where.append("o.status NOT IN ('Виконано', 'Скасовано')")
    orders = db_all(
        f"""
        SELECT o.*, s.name AS service_name, c.name AS category_name, client.full_name AS client_name
        FROM orders o
        JOIN services s ON s.id = o.service_id
        JOIN categories c ON c.id = o.category_id
        JOIN users client ON client.id = o.client_id
        WHERE {' AND '.join(where)}
        ORDER BY o.preferred_at DESC
        """,
        tuple(params),
    )
    return render_template("worker/assigned_orders.html", orders=orders, mode=mode)


@app.route("/worker/orders/history")
@role_required("worker")
def worker_history():
    orders = db_all(
        """
        SELECT o.*, s.name AS service_name, c.name AS category_name, client.full_name AS client_name,
               r.rating, r.text AS review_text
        FROM orders o
        JOIN services s ON s.id = o.service_id
        JOIN categories c ON c.id = o.category_id
        JOIN users client ON client.id = o.client_id
        LEFT JOIN reviews r ON r.order_id = o.id
        WHERE o.worker_id = ? AND o.status IN ('Виконано', 'Скасовано')
        ORDER BY o.updated_at DESC
        """,
        (g.user["id"],),
    )
    return render_template("worker/history.html", orders=orders)


def worker_can_view_order(order: sqlite3.Row) -> bool:
    if not order:
        return False
    if order["worker_id"] == g.user["id"]:
        return True
    if order["worker_id"] is None and order["status"] in {"Нове", "Очікує підтвердження"}:
        if order["category_slug"] != g.user["specialization"]:
            return False
        rejected = db_one(
            "SELECT id FROM order_rejections WHERE order_id = ? AND worker_id = ?",
            (order["id"], g.user["id"]),
        )
        return rejected is None
    return False


@app.route("/worker/orders/<int:order_id>")
@role_required("worker")
def worker_order_detail(order_id: int):
    order = order_detail_query(order_id)
    if not worker_can_view_order(order):
        abort(404)
    history = db_all(
        """
        SELECT h.*, u.full_name AS user_name
        FROM order_status_history h
        LEFT JOIN users u ON u.id = h.changed_by_user_id
        WHERE h.order_id = ?
        ORDER BY h.created_at DESC
        """,
        (order_id,),
    )
    return render_template("worker/order_detail.html", order=order, history=history)


@app.post("/worker/orders/<int:order_id>/accept")
@role_required("worker")
def worker_accept_order(order_id: int):
    order = order_detail_query(order_id)
    if not worker_can_view_order(order) or order["worker_id"] is not None:
        abort(404)
    get_db().execute(
        "UPDATE orders SET worker_id = ?, status = ?, updated_at = ? WHERE id = ?",
        (g.user["id"], "Прийнято виконавцем", now_iso(), order_id),
    )
    add_status_history(order_id, "Прийнято виконавцем", "Виконавець прийняв замовлення.")
    get_db().commit()
    flash("Замовлення прийнято.", "success")
    return redirect(url_for("worker_order_detail", order_id=order_id))


@app.post("/worker/orders/<int:order_id>/reject")
@role_required("worker")
def worker_reject_order(order_id: int):
    order = order_detail_query(order_id)
    if not worker_can_view_order(order) or order["worker_id"] is not None:
        abort(404)
    get_db().execute(
        "INSERT OR IGNORE INTO order_rejections (order_id, worker_id) VALUES (?, ?)",
        (order_id, g.user["id"]),
    )
    get_db().commit()
    flash("Заявку відхилено. Вона більше не відображатиметься у доступних.", "info")
    return redirect(url_for("worker_available_orders"))


@app.post("/worker/orders/<int:order_id>/status")
@role_required("worker")
def worker_update_status(order_id: int):
    order = db_one("SELECT * FROM orders WHERE id = ? AND worker_id = ?", (order_id, g.user["id"]))
    if not order:
        abort(404)
    new_status = request.form.get("status") or ""
    if new_status not in WORKER_STATUS_OPTIONS:
        flash("Некоректний статус.", "danger")
    else:
        update_order_status(order_id, new_status, "Виконавець змінив статус.")
        if new_status == "Виконано" and order["final_price"] is None:
            get_db().execute("UPDATE orders SET final_price = COALESCE(final_price, estimated_price) WHERE id = ?", (order_id,))
        get_db().commit()
        flash("Статус оновлено.", "success")
    return redirect(url_for("worker_order_detail", order_id=order_id))


@app.route("/admin")
@role_required("admin")
def admin_dashboard():
    stats = {
        "orders": db_one("SELECT COUNT(*) AS cnt FROM orders")["cnt"],
        "new_orders": db_one("SELECT COUNT(*) AS cnt FROM orders WHERE status = 'Нове'")["cnt"],
        "done": db_one("SELECT COUNT(*) AS cnt FROM orders WHERE status = 'Виконано'")["cnt"],
        "users": db_one("SELECT COUNT(*) AS cnt FROM users")["cnt"],
        "revenue": db_one("SELECT COALESCE(SUM(COALESCE(final_price, estimated_price)), 0) AS total FROM orders WHERE is_paid = 1")["total"],
    }
    popular_services = db_all(
        """
        SELECT s.name, c.name AS category_name, COUNT(o.id) AS orders_count
        FROM services s
        JOIN categories c ON c.id = s.category_id
        LEFT JOIN orders o ON o.service_id = s.id
        GROUP BY s.id
        ORDER BY orders_count DESC, s.name
        LIMIT 5
        """
    )
    latest_orders = db_all(
        """
        SELECT o.*, s.name AS service_name, c.name AS category_name, client.full_name AS client_name,
               worker.full_name AS worker_name
        FROM orders o
        JOIN services s ON s.id = o.service_id
        JOIN categories c ON c.id = o.category_id
        JOIN users client ON client.id = o.client_id
        LEFT JOIN users worker ON worker.id = o.worker_id
        ORDER BY o.created_at DESC
        LIMIT 6
        """
    )
    return render_template("admin/dashboard.html", stats=stats, popular_services=popular_services, latest_orders=latest_orders)


@app.route("/admin/users")
@role_required("admin")
def admin_users():
    q = (request.args.get("q") or "").strip()
    role = request.args.get("role") or ""
    where = ["1=1"]
    params: list = []
    if q:
        where.append("(LOWER(full_name) LIKE ? OR LOWER(email) LIKE ? OR phone LIKE ?)")
        pattern = f"%{q.lower()}%"
        params.extend([pattern, pattern, f"%{q}%"])
    if role:
        where.append("role = ?")
        params.append(role)
    users = db_all(
        f"SELECT * FROM users WHERE {' AND '.join(where)} ORDER BY created_at DESC",
        tuple(params),
    )
    return render_template("admin/users.html", users=users, q=q, selected_role=role)


@app.post("/admin/users/<int:user_id>/toggle-block")
@role_required("admin")
def admin_toggle_block(user_id: int):
    if user_id == g.user["id"]:
        flash("Не можна заблокувати власний акаунт.", "danger")
        return redirect(url_for("admin_users"))
    user = db_one("SELECT * FROM users WHERE id = ?", (user_id,))
    if not user:
        abort(404)
    get_db().execute("UPDATE users SET is_blocked = ? WHERE id = ?", (0 if user["is_blocked"] else 1, user_id))
    get_db().commit()
    flash("Статус користувача оновлено.", "success")
    return redirect(url_for("admin_users"))


@app.post("/admin/users/<int:user_id>/role")
@role_required("admin")
def admin_change_role(user_id: int):
    new_role = request.form.get("role")
    if new_role not in ROLES:
        flash("Некоректна роль.", "danger")
    elif user_id == g.user["id"] and new_role != "admin":
        flash("Не можна забрати роль адміністратора у власного акаунта.", "danger")
    else:
        get_db().execute("UPDATE users SET role = ? WHERE id = ?", (new_role, user_id))
        get_db().commit()
        flash("Роль користувача змінено.", "success")
    return redirect(url_for("admin_users"))


@app.route("/admin/categories")
@role_required("admin")
def admin_categories():
    categories = db_all("SELECT * FROM categories ORDER BY is_active DESC, name")
    return render_template("admin/categories.html", categories=categories)


@app.route("/admin/categories/new", methods=("GET", "POST"))
@role_required("admin")
def admin_category_new():
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        slug = normalize_slug(request.form.get("slug") or name, "category")
        description = (request.form.get("description") or "").strip()
        is_active = 1 if request.form.get("is_active") else 0
        if not name:
            flash("Назва категорії є обов'язковою.", "danger")
        else:
            try:
                get_db().execute(
                    "INSERT INTO categories (name, slug, description, is_active) VALUES (?, ?, ?, ?)",
                    (name, slug, description, is_active),
                )
                get_db().commit()
                flash("Категорію додано.", "success")
                return redirect(url_for("admin_categories"))
            except sqlite3.IntegrityError:
                flash("Категорія з такою назвою або slug вже існує.", "danger")
    return render_template("admin/category_form.html", category=None)


@app.route("/admin/categories/<int:category_id>/edit", methods=("GET", "POST"))
@role_required("admin")
def admin_category_edit(category_id: int):
    category = db_one("SELECT * FROM categories WHERE id = ?", (category_id,))
    if not category:
        abort(404)
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        slug = normalize_slug(request.form.get("slug") or name, "category")
        description = (request.form.get("description") or "").strip()
        is_active = 1 if request.form.get("is_active") else 0
        if not name:
            flash("Назва категорії є обов'язковою.", "danger")
        else:
            try:
                get_db().execute(
                    "UPDATE categories SET name = ?, slug = ?, description = ?, is_active = ? WHERE id = ?",
                    (name, slug, description, is_active, category_id),
                )
                get_db().commit()
                flash("Категорію оновлено.", "success")
                return redirect(url_for("admin_categories"))
            except sqlite3.IntegrityError:
                flash("Категорія з такою назвою або slug вже існує.", "danger")
    return render_template("admin/category_form.html", category=category)


@app.post("/admin/categories/<int:category_id>/delete")
@role_required("admin")
def admin_category_delete(category_id: int):
    try:
        get_db().execute("DELETE FROM categories WHERE id = ?", (category_id,))
    except sqlite3.IntegrityError:
        get_db().execute("UPDATE categories SET is_active = 0 WHERE id = ?", (category_id,))
    get_db().commit()
    flash("Категорію видалено або деактивовано, якщо вона вже використовується.", "success")
    return redirect(url_for("admin_categories"))


@app.route("/admin/services")
@role_required("admin")
def admin_services():
    services = db_all(
        """
        SELECT s.*, c.name AS category_name
        FROM services s
        JOIN categories c ON c.id = s.category_id
        ORDER BY s.is_active DESC, c.name, s.name
        """
    )
    return render_template("admin/services.html", services=services)


@app.route("/admin/services/new", methods=("GET", "POST"))
@role_required("admin")
def admin_service_new():
    categories = db_all("SELECT * FROM categories ORDER BY name")
    if request.method == "POST":
        category_id = to_int(request.form.get("category_id"))
        name = (request.form.get("name") or "").strip()
        slug = normalize_slug(request.form.get("slug") or name, "service")
        description = (request.form.get("description") or "").strip()
        unit_label = (request.form.get("unit_label") or "од.").strip()
        is_active = 1 if request.form.get("is_active") else 0
        if not name or not category_id:
            flash("Оберіть категорію і назву послуги.", "danger")
        else:
            try:
                get_db().execute(
                    """
                    INSERT INTO services (category_id, name, slug, description, unit_label, is_active)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (category_id, name, slug, description, unit_label, is_active),
                )
                get_db().commit()
                flash("Послугу додано. Тепер можна створити прайс.", "success")
                return redirect(url_for("admin_services"))
            except sqlite3.IntegrityError:
                flash("У цій категорії вже існує послуга з таким slug.", "danger")
    return render_template("admin/service_form.html", service=None, categories=categories)


@app.route("/admin/services/<int:service_id>/edit", methods=("GET", "POST"))
@role_required("admin")
def admin_service_edit(service_id: int):
    service = db_one("SELECT * FROM services WHERE id = ?", (service_id,))
    if not service:
        abort(404)
    categories = db_all("SELECT * FROM categories ORDER BY name")
    if request.method == "POST":
        category_id = to_int(request.form.get("category_id"))
        name = (request.form.get("name") or "").strip()
        slug = normalize_slug(request.form.get("slug") or name, "service")
        description = (request.form.get("description") or "").strip()
        unit_label = (request.form.get("unit_label") or "од.").strip()
        is_active = 1 if request.form.get("is_active") else 0
        if not name or not category_id:
            flash("Оберіть категорію і назву послуги.", "danger")
        else:
            try:
                get_db().execute(
                    """
                    UPDATE services
                    SET category_id = ?, name = ?, slug = ?, description = ?, unit_label = ?, is_active = ?
                    WHERE id = ?
                    """,
                    (category_id, name, slug, description, unit_label, is_active, service_id),
                )
                get_db().commit()
                flash("Послугу оновлено.", "success")
                return redirect(url_for("admin_services"))
            except sqlite3.IntegrityError:
                flash("У цій категорії вже існує послуга з таким slug.", "danger")
    return render_template("admin/service_form.html", service=service, categories=categories)


@app.post("/admin/services/<int:service_id>/delete")
@role_required("admin")
def admin_service_delete(service_id: int):
    try:
        get_db().execute("DELETE FROM services WHERE id = ?", (service_id,))
    except sqlite3.IntegrityError:
        get_db().execute("UPDATE services SET is_active = 0 WHERE id = ?", (service_id,))
    get_db().commit()
    flash("Послугу видалено або деактивовано, якщо вона вже використовується.", "success")
    return redirect(url_for("admin_services"))


@app.route("/admin/prices")
@role_required("admin")
def admin_prices():
    prices = db_all(
        """
        SELECT p.*, s.name AS service_name, c.name AS category_name
        FROM prices p
        JOIN services s ON s.id = p.service_id
        JOIN categories c ON c.id = s.category_id
        ORDER BY p.is_active DESC, c.name, s.name, p.updated_at DESC
        """
    )
    return render_template("admin/prices.html", prices=prices)


@app.route("/admin/prices/new", methods=("GET", "POST"))
@role_required("admin")
def admin_price_new():
    services = db_all(
        """
        SELECT s.*, c.name AS category_name
        FROM services s
        JOIN categories c ON c.id = s.category_id
        ORDER BY c.name, s.name
        """
    )
    if request.method == "POST":
        service_id = to_int(request.form.get("service_id"))
        base_price = to_float(request.form.get("base_price"))
        unit_price = to_float(request.form.get("unit_price"))
        unit_name = (request.form.get("unit_name") or "од.").strip()
        extra_option_price = to_float(request.form.get("extra_option_price"))
        urgent_multiplier = to_float(request.form.get("urgent_multiplier"), 1)
        is_active = 1 if request.form.get("is_active") else 0
        if not service_id:
            flash("Оберіть послугу.", "danger")
        else:
            get_db().execute(
                """
                INSERT INTO prices (service_id, base_price, unit_price, unit_name, extra_option_price, urgent_multiplier, is_active, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (service_id, base_price, unit_price, unit_name, extra_option_price, urgent_multiplier or 1, is_active, now_iso()),
            )
            get_db().commit()
            flash("Прайс додано.", "success")
            return redirect(url_for("admin_prices"))
    return render_template("admin/price_form.html", price=None, services=services)


@app.route("/admin/prices/<int:price_id>/edit", methods=("GET", "POST"))
@role_required("admin")
def admin_price_edit(price_id: int):
    price = db_one("SELECT * FROM prices WHERE id = ?", (price_id,))
    if not price:
        abort(404)
    services = db_all(
        """
        SELECT s.*, c.name AS category_name
        FROM services s
        JOIN categories c ON c.id = s.category_id
        ORDER BY c.name, s.name
        """
    )
    if request.method == "POST":
        service_id = to_int(request.form.get("service_id"))
        base_price = to_float(request.form.get("base_price"))
        unit_price = to_float(request.form.get("unit_price"))
        unit_name = (request.form.get("unit_name") or "од.").strip()
        extra_option_price = to_float(request.form.get("extra_option_price"))
        urgent_multiplier = to_float(request.form.get("urgent_multiplier"), 1)
        is_active = 1 if request.form.get("is_active") else 0
        get_db().execute(
            """
            UPDATE prices
            SET service_id = ?, base_price = ?, unit_price = ?, unit_name = ?, extra_option_price = ?,
                urgent_multiplier = ?, is_active = ?, updated_at = ?
            WHERE id = ?
            """,
            (service_id, base_price, unit_price, unit_name, extra_option_price, urgent_multiplier or 1, is_active, now_iso(), price_id),
        )
        get_db().commit()
        flash("Прайс оновлено.", "success")
        return redirect(url_for("admin_prices"))
    return render_template("admin/price_form.html", price=price, services=services)


@app.post("/admin/prices/<int:price_id>/delete")
@role_required("admin")
def admin_price_delete(price_id: int):
    get_db().execute("DELETE FROM prices WHERE id = ?", (price_id,))
    get_db().commit()
    flash("Прайс видалено.", "success")
    return redirect(url_for("admin_prices"))


@app.route("/admin/orders")
@role_required("admin")
def admin_orders():
    status = request.args.get("status") or ""
    category_id = request.args.get("category_id") or ""
    worker_id = request.args.get("worker_id") or ""
    date_from = request.args.get("date_from") or ""
    date_to = request.args.get("date_to") or ""
    where = ["1=1"]
    params: list = []
    if status:
        where.append("o.status = ?")
        params.append(status)
    if category_id:
        where.append("o.category_id = ?")
        params.append(category_id)
    if worker_id:
        where.append("o.worker_id = ?")
        params.append(worker_id)
    if date_from:
        where.append("DATE(o.created_at) >= DATE(?)")
        params.append(date_from)
    if date_to:
        where.append("DATE(o.created_at) <= DATE(?)")
        params.append(date_to)
    orders = db_all(
        f"""
        SELECT o.*, s.name AS service_name, c.name AS category_name, client.full_name AS client_name,
               worker.full_name AS worker_name
        FROM orders o
        JOIN services s ON s.id = o.service_id
        JOIN categories c ON c.id = o.category_id
        JOIN users client ON client.id = o.client_id
        LEFT JOIN users worker ON worker.id = o.worker_id
        WHERE {' AND '.join(where)}
        ORDER BY o.created_at DESC
        """,
        tuple(params),
    )
    categories = db_all("SELECT * FROM categories ORDER BY name")
    workers = db_all("SELECT id, full_name, specialization FROM users WHERE role = 'worker' ORDER BY full_name")
    return render_template(
        "admin/orders.html",
        orders=orders,
        categories=categories,
        workers=workers,
        selected_status=status,
        selected_category=category_id,
        selected_worker=worker_id,
        date_from=date_from,
        date_to=date_to,
    )


@app.route("/admin/orders/<int:order_id>")
@role_required("admin")
def admin_order_detail(order_id: int):
    order = order_detail_query(order_id)
    if not order:
        abort(404)
    workers = db_all("SELECT * FROM users WHERE role = 'worker' AND is_blocked = 0 ORDER BY specialization, full_name")
    history = db_all(
        """
        SELECT h.*, u.full_name AS user_name
        FROM order_status_history h
        LEFT JOIN users u ON u.id = h.changed_by_user_id
        WHERE h.order_id = ?
        ORDER BY h.created_at DESC
        """,
        (order_id,),
    )
    review = db_one("SELECT * FROM reviews WHERE order_id = ?", (order_id,))
    payments = latest_payments(order_id)
    return render_template("admin/order_detail.html", order=order, workers=workers, history=history, review=review, payments=payments)


@app.post("/admin/orders/<int:order_id>/update")
@role_required("admin")
def admin_update_order(order_id: int):
    order = db_one("SELECT * FROM orders WHERE id = ?", (order_id,))
    if not order:
        abort(404)
    new_status = request.form.get("status") or order["status"]
    if new_status not in ORDER_STATUSES:
        flash("Некоректний статус.", "danger")
        return redirect(url_for("admin_order_detail", order_id=order_id))
    worker_id_value = request.form.get("worker_id") or None
    worker_id = to_int(worker_id_value) if worker_id_value else None
    final_price = to_float(request.form.get("final_price")) if request.form.get("final_price") else None
    is_paid = 1 if request.form.get("is_paid") else 0
    paid_at = order["paid_at"]
    if is_paid and not order["is_paid"]:
        paid_at = now_iso()
    if not is_paid:
        paid_at = None
    get_db().execute(
        """
        UPDATE orders
        SET worker_id = ?, status = ?, final_price = ?, is_paid = ?, paid_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (worker_id, new_status, final_price, is_paid, paid_at, now_iso(), order_id),
    )
    if order["status"] != new_status:
        add_status_history(order_id, new_status, "Адміністратор змінив статус/призначення.")
    elif order["worker_id"] != worker_id:
        add_status_history(order_id, new_status, "Адміністратор призначив або змінив виконавця.")
    get_db().commit()
    flash("Замовлення оновлено.", "success")
    return redirect(url_for("admin_order_detail", order_id=order_id))


@app.route("/admin/reviews")
@role_required("admin")
def admin_reviews():
    reviews = db_all(
        """
        SELECT r.*, o.id AS order_number, s.name AS service_name, client.full_name AS client_name,
               worker.full_name AS worker_name
        FROM reviews r
        JOIN orders o ON o.id = r.order_id
        JOIN services s ON s.id = o.service_id
        JOIN users client ON client.id = r.client_id
        LEFT JOIN users worker ON worker.id = r.worker_id
        ORDER BY r.created_at DESC
        """
    )
    return render_template("admin/reviews.html", reviews=reviews)


@app.post("/admin/reviews/<int:review_id>/toggle")
@role_required("admin")
def admin_review_toggle(review_id: int):
    review = db_one("SELECT * FROM reviews WHERE id = ?", (review_id,))
    if not review:
        abort(404)
    get_db().execute("UPDATE reviews SET is_visible = ? WHERE id = ?", (0 if review["is_visible"] else 1, review_id))
    get_db().commit()
    flash("Видимість відгуку змінено.", "success")
    return redirect(url_for("admin_reviews"))


@app.post("/admin/reviews/<int:review_id>/delete")
@role_required("admin")
def admin_review_delete(review_id: int):
    get_db().execute("DELETE FROM reviews WHERE id = ?", (review_id,))
    get_db().commit()
    flash("Відгук видалено.", "success")
    return redirect(url_for("admin_reviews"))


@app.errorhandler(403)
def forbidden(error):
    return render_template("error.html", code=403, message="Немає доступу до цього розділу."), 403


@app.errorhandler(404)
def not_found(error):
    return render_template("error.html", code=404, message="Сторінку або запис не знайдено."), 404


@app.errorhandler(413)
def file_too_large(error):
    flash("Файл завеликий. Максимальний розмір — 8 МБ.", "danger")
    return redirect(request.referrer or url_for("home"))


# Автоматично створює SQLite БД при першому запуску.
with app.app_context():
    get_db().executescript(SCHEMA_SQL)
    seed_database()
    get_db().commit()


if __name__ == "__main__":
    app.run(debug=True)
