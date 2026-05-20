# ServiceHub

Веб-платформа для онлайн-замовлення побутових послуг: клінінг, ремонт і доставка.
Проєкт зроблений на Flask з SQLite, рольовим доступом, кабінетами клієнта/виконавця/адміністратора та підготовкою до деплою.

## Що є в проєкті

- Публічна частина: головна, каталог, тарифи, FAQ, контакти, вхід, реєстрація.
- Кабінет клієнта: профіль, створення замовлення, мої замовлення, деталі, онлайн-оплата, відгуки.
- Кабінет виконавця: профіль, доступні заявки, призначені заявки, зміна статусів, історія виконань.
- Адмін-панель: статистика, користувачі, категорії, послуги, прайс, замовлення, призначення виконавця, відгуки.
- БД SQLite: `users`, `categories`, `services`, `prices`, `orders`, `order_status_history`, `order_rejections`, `payments`, `reviews`.
- Оплата: у локальному sandbox-режимі працює внутрішнє підтвердження платежу без помилки 403; для бойового режиму залишена інтеграція LiqPay Checkout з `data/signature`, callback і перевіркою підпису.
- Підготовка до хостингу: `Procfile`, `gunicorn.conf.py`, `render.yaml`, `.env.example`, `runtime.txt`.

## Локальний запуск у Visual Studio Code

1. Відкрийте папку проєкту у VS Code.
2. Створіть віртуальне середовище.
3. Встановіть залежності.
4. Підготуйте базу.
5. Запустіть сайт.

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
flask --app app init-db --reset
flask --app app run --debug
```

### macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
flask --app app init-db --reset
flask --app app run --debug
```

Після запуску відкрийте:

```text
http://127.0.0.1:5000
```

## Демо-акаунти

| Роль | Email | Пароль |
|---|---|---|
| Адміністратор | `admin@example.com` | `admin123` |
| Клієнт | `client@example.com` | `client123` |
| Виконавець, клінінг | `worker_clean@example.com` | `worker123` |
| Виконавець, ремонт | `worker_repair@example.com` | `worker123` |
| Виконавець, доставка | `worker_delivery@example.com` | `worker123` |

## Налаштування LiqPay

1. Створіть компанію в LiqPay.
2. Візьміть Public key і Private key у налаштуваннях компанії.
3. Для локального запуску файл `.env` уже доданий у проєкт. За потреби його можна створити на основі `.env.example`.
4. Заповніть ключі:

```env
LIQPAY_PUBLIC_KEY=your_liqpay_public_key
LIQPAY_PRIVATE_KEY=your_liqpay_private_key
LIQPAY_SANDBOX=1
PUBLIC_BASE_URL=http://127.0.0.1:5000
```

У локальному режимі `LIQPAY_SANDBOX=1` платіж підтверджується на сторінці сайту, щоб не залежати від зовнішнього checkout. Для реальної оплати на хостингу встановіть:

```env
LIQPAY_SANDBOX=0
PUBLIC_BASE_URL=https://your-domain.com
```

Callback-адреса формується автоматично:

```text
/payments/liqpay/callback
```

Після успішного callback сайт перевіряє підпис платежу, суму і статус, після чого ставить замовленню оплату `Оплачено`.

## Деплой на Render

У проєкті вже є `render.yaml`. Найзручніший варіант:

1. Завантажити код на GitHub.
2. У Render створити новий Web Service або Blueprint.
3. Build command:

```bash
pip install -r requirements.txt
```

4. Start command:

```bash
flask --app app init-db && gunicorn app:app --config gunicorn.conf.py
```

5. Додати змінні середовища:

```env
SECRET_KEY=long-random-secret
DATABASE_PATH=/var/data/services.sqlite
UPLOAD_DIR=/var/data/uploads
PUBLIC_BASE_URL=https://your-service.onrender.com
LIQPAY_PUBLIC_KEY=your_liqpay_public_key
LIQPAY_PRIVATE_KEY=your_liqpay_private_key
LIQPAY_SANDBOX=0
```

6. Додати persistent disk з mount path:

```text
/var/data
```

Це потрібно, щоб SQLite-файл і завантажені фото не зникали після перезапуску сервера.

## Структура

```text
home_services_platform/
├── app.py
├── requirements.txt
├── Procfile
├── render.yaml
├── gunicorn.conf.py
├── runtime.txt
├── .env.example
├── static/
│   ├── css/style.css
│   └── js/app.js
├── templates/
│   ├── admin/
│   ├── auth/
│   ├── client/
│   └── worker/
├── uploads/
└── instance/
```
