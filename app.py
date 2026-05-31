from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, Response
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import csv
import io
import os
import smtplib
import pyodbc
import stripe
from datetime import datetime, date, time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

app = Flask(__name__)
app.secret_key = "isdn_secret_key_2026"

app.config["STRIPE_SECRET_KEY"] = os.getenv("STRIPE_SECRET_KEY", "").strip()
app.config["STRIPE_WEBHOOK_SECRET"] = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()
app.config["STRIPE_CURRENCY"] = os.getenv("STRIPE_CURRENCY", "LKR").strip().lower()

app.config["SMTP_HOST"] = os.getenv("SMTP_HOST", "smtp.gmail.com").strip()
app.config["SMTP_PORT"] = int(os.getenv("SMTP_PORT", "587"))
app.config["SMTP_USERNAME"] = os.getenv("SMTP_USERNAME", "").strip()
app.config["SMTP_PASSWORD"] = os.getenv("SMTP_PASSWORD", "").strip()
app.config["MAIL_FROM"] = os.getenv("MAIL_FROM", app.config["SMTP_USERNAME"] or "noreply@islandlink.local").strip()

app.config["UPLOAD_FOLDER"] = os.path.join("static", "uploads", "products")
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024

os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

if app.config["STRIPE_SECRET_KEY"]:
    stripe.api_key = app.config["STRIPE_SECRET_KEY"]


def allowed_image(filename):
    if not filename or "." not in filename:
        return False
    ext = filename.rsplit(".", 1)[1].lower()
    return ext in {"png", "jpg", "jpeg", "webp"}


def get_db_connection():
    return pyodbc.connect(
        "DRIVER={SQL Server};"
        "SERVER=DESKTOP-D555FEQ\\SQLEXPRESS;"
        "DATABASE=ISDN_DB;"
        "Trusted_Connection=yes;"
    )


def _to_datetime(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="ignore")

    text = str(value).strip()
    if not text:
        return None

    normalized = text.replace("T", " ").replace("Z", "")
    patterns = [
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y/%m/%d",
        "%d-%m-%Y %H:%M:%S",
        "%d-%m-%Y %H:%M",
        "%d-%m-%Y",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%d/%m/%Y",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y %H:%M",
        "%m/%d/%Y",
    ]

    for pattern in patterns:
        try:
            return datetime.strptime(normalized, pattern)
        except ValueError:
            continue

    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _format_datetime(value, empty_text="N/A"):
    parsed = _to_datetime(value)
    if not parsed:
        return empty_text
    return parsed.strftime("%Y-%m-%d %I:%M %p")


def _format_date(value, empty_text="Will be updated soon"):
    parsed = _to_datetime(value)
    if not parsed:
        return empty_text
    return parsed.strftime("%Y-%m-%d")


def _safe_close(cursor=None, conn=None):
    try:
        if cursor is not None:
            cursor.close()
    except Exception:
        pass
    try:
        if conn is not None:
            conn.close()
    except Exception:
        pass


def _customer_guard():
    if "user_id" not in session:
        return redirect(url_for("login"))
    if session.get("role") != "customer":
        flash("Access denied.", "error")
        return redirect(url_for("login"))
    return None


def _admin_guard():
    if "user_id" not in session:
        return redirect(url_for("login"))
    if session.get("role") not in ["staff", "admin"]:
        flash("Access denied.", "error")
        return redirect(url_for("login"))
    return None


def _safe_int(value, default=1, minimum=1):
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    if number < minimum:
        number = minimum
    return number


def _get_base_url():
    return request.host_url.rstrip("/")


def _generate_invoice_number(order_id):
    return f"INV-{datetime.now().strftime('%Y%m%d')}-{int(order_id):06d}"


def _fetch_product(cursor, product_id):
    cursor.execute(
        """
        SELECT product_id, product_name, category, price, stock_quantity, is_active, image_path
        FROM dbo.Products
        WHERE product_id = ?
        """,
        (product_id,)
    )
    return cursor.fetchone()


def _get_order_items(cursor, order_id):
    cursor.execute(
        """
        SELECT
            oi.product_id,
            p.product_name,
            oi.quantity,
            oi.unit_price
        FROM dbo.OrderItems oi
        INNER JOIN dbo.Products p ON p.product_id = oi.product_id
        WHERE oi.order_id = ?
        ORDER BY oi.order_item_id ASC
        """,
        (order_id,)
    )
    rows = cursor.fetchall()

    items = []
    for row in rows:
        quantity = int(row[2])
        unit_price = float(row[3])
        items.append({
            "product_id": int(row[0]),
            "product_name": str(row[1]),
            "quantity": quantity,
            "unit_price": unit_price,
            "line_total": quantity * unit_price,
        })
    return items


def _create_invoice_and_payment(cursor, order_id, customer_email, total_amount):
    cursor.execute(
        """
        SELECT invoice_id, invoice_number
        FROM dbo.Invoices
        WHERE order_id = ?
        """,
        (order_id,)
    )
    existing = cursor.fetchone()
    if existing:
        return int(existing[0]), str(existing[1])

    invoice_number = _generate_invoice_number(order_id)

    cursor.execute(
        """
        INSERT INTO dbo.Invoices (
            order_id, invoice_number, invoice_date, subtotal, tax_amount,
            discount_amount, total_amount, invoice_status, customer_email
        )
        OUTPUT INSERTED.invoice_id
        VALUES (?, ?, GETDATE(), ?, 0, 0, ?, 'Unpaid', ?)
        """,
        (order_id, invoice_number, float(total_amount), float(total_amount), customer_email)
    )
    invoice_id = cursor.fetchone()[0]

    cursor.execute(
        """
        INSERT INTO dbo.Payments (
            order_id, invoice_id, amount, currency, gateway_name, gateway_status, created_at
        )
        VALUES (?, ?, ?, ?, 'Stripe', 'Pending', GETDATE())
        """,
        (order_id, invoice_id, float(total_amount), app.config["STRIPE_CURRENCY"].upper())
    )

    return int(invoice_id), invoice_number


def _create_order(cursor, customer_id, customer_email, items):
    total_amount = sum(item["unit_price"] * item["quantity"] for item in items)

    cursor.execute(
        """
        INSERT INTO dbo.Orders (customer_id, total_amount, order_status)
        OUTPUT INSERTED.order_id
        VALUES (?, ?, ?)
        """,
        (customer_id, total_amount, "Pending")
    )
    order_id = cursor.fetchone()[0]

    for item in items:
        cursor.execute(
            """
            INSERT INTO dbo.OrderItems (order_id, product_id, quantity, unit_price)
            VALUES (?, ?, ?, ?)
            """,
            (order_id, item["product_id"], item["quantity"], item["unit_price"])
        )

        cursor.execute(
            """
            UPDATE dbo.Products
            SET stock_quantity = stock_quantity - ?
            WHERE product_id = ?
            """,
            (item["quantity"], item["product_id"])
        )

    cursor.execute(
        """
        INSERT INTO dbo.Deliveries (order_id, delivery_status, estimated_date, tracking_note)
        VALUES (?, ?, NULL, ?)
        """,
        (order_id, "Pending", "Order received and awaiting processing")
    )

    invoice_id, invoice_number = _create_invoice_and_payment(
        cursor, order_id, customer_email, total_amount
    )

    return order_id, total_amount, invoice_id, invoice_number


def _update_invoice_checkout_session(invoice_id, session_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE dbo.Invoices
            SET stripe_checkout_session_id = ?
            WHERE invoice_id = ?
            """,
            (session_id, invoice_id)
        )
        conn.commit()
    finally:
        _safe_close(cursor, conn)


def _mark_invoice_paid(order_id, invoice_id, payment_intent_id=None, session_id=None):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE dbo.Invoices
            SET invoice_status = 'Paid',
                stripe_payment_intent_id = COALESCE(?, stripe_payment_intent_id),
                stripe_checkout_session_id = COALESCE(?, stripe_checkout_session_id)
            WHERE invoice_id = ? AND order_id = ?
            """,
            (payment_intent_id, session_id, invoice_id, order_id)
        )
        cursor.execute(
            """
            UPDATE dbo.Payments
            SET gateway_status = 'Paid',
                gateway_reference = COALESCE(?, gateway_reference),
                payment_method = COALESCE(payment_method, 'card'),
                paid_at = CASE WHEN paid_at IS NULL THEN GETDATE() ELSE paid_at END
            WHERE order_id = ? AND invoice_id = ?
            """,
            (payment_intent_id, order_id, invoice_id)
        )
        cursor.execute(
            """
            UPDATE dbo.Orders
            SET order_status = CASE WHEN order_status = 'Pending' THEN 'Confirmed' ELSE order_status END
            WHERE order_id = ?
            """,
            (order_id,)
        )
        conn.commit()
    finally:
        _safe_close(cursor, conn)


def _mark_payment_cancelled(order_id, invoice_id, reason="Customer cancelled checkout"):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE dbo.Payments
            SET gateway_status = 'Cancelled',
                failure_reason = ?
            WHERE order_id = ? AND invoice_id = ?
            """,
            (reason, order_id, invoice_id)
        )
        conn.commit()
    finally:
        _safe_close(cursor, conn)


def _build_invoice_email_html(invoice_data):
    rows_html = ""
    for item in invoice_data["items"]:
        rows_html += f"""
        <tr>
            <td style=\"padding:10px;border:1px solid #e5e7eb;\">{item['product_name']}</td>
            <td style=\"padding:10px;border:1px solid #e5e7eb;text-align:center;\">{item['quantity']}</td>
            <td style=\"padding:10px;border:1px solid #e5e7eb;text-align:right;\">{item['unit_price']:.2f}</td>
            <td style=\"padding:10px;border:1px solid #e5e7eb;text-align:right;\">{item['line_total']:.2f}</td>
        </tr>
        """

    return f"""
    <html>
    <body style=\"font-family:Arial,sans-serif;background:#f8fafc;padding:24px;color:#0f172a;\">
        <div style=\"max-width:760px;margin:auto;background:#ffffff;border:1px solid #e2e8f0;border-radius:16px;overflow:hidden;\">
            <div style=\"background:#0f172a;color:#ffffff;padding:24px;\">
                <h2 style=\"margin:0;\">IslandLink Distribution Platform</h2>
                <p style=\"margin:8px 0 0;\">Digital Invoice</p>
            </div>
            <div style=\"padding:24px;\">
                <p>Hello {invoice_data['customer_name']},</p>
                <p>Your invoice has been generated automatically.</p>
                <div style=\"display:grid;grid-template-columns:1fr 1fr;gap:12px;margin:20px 0;\">
                    <div><strong>Invoice No:</strong> {invoice_data['invoice_number']}</div>
                    <div><strong>Order ID:</strong> #{invoice_data['order_id']}</div>
                    <div><strong>Invoice Date:</strong> {invoice_data['invoice_date']}</div>
                    <div><strong>Order Date:</strong> {invoice_data['order_date']}</div>
                    <div><strong>Status:</strong> {invoice_data['invoice_status']}</div>
                    <div><strong>Customer Email:</strong> {invoice_data['customer_email']}</div>
                </div>
                <table style=\"width:100%;border-collapse:collapse;margin-top:16px;\">
                    <thead>
                        <tr style=\"background:#f1f5f9;\">
                            <th style=\"padding:10px;border:1px solid #e5e7eb;text-align:left;\">Product</th>
                            <th style=\"padding:10px;border:1px solid #e5e7eb;text-align:center;\">Qty</th>
                            <th style=\"padding:10px;border:1px solid #e5e7eb;text-align:right;\">Unit Price</th>
                            <th style=\"padding:10px;border:1px solid #e5e7eb;text-align:right;\">Total</th>
                        </tr>
                    </thead>
                    <tbody>{rows_html}</tbody>
                </table>
                <div style=\"margin-top:20px;text-align:right;\">
                    <p style=\"font-size:18px;\"><strong>Grand Total: {invoice_data['total_amount']:.2f} {app.config['STRIPE_CURRENCY'].upper()}</strong></p>
                </div>
                <p style=\"margin-top:24px;\">Thank you for your order.</p>
            </div>
        </div>
    </body>
    </html>
    """


def _load_invoice_for_email(order_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT
                i.invoice_id,
                i.invoice_number,
                i.invoice_date,
                i.total_amount,
                i.invoice_status,
                i.customer_email,
                o.order_id,
                o.order_date,
                u.full_name
            FROM dbo.Invoices i
            INNER JOIN dbo.Orders o ON o.order_id = i.order_id
            INNER JOIN dbo.Users u ON u.user_id = o.customer_id
            WHERE i.order_id = ?
            """,
            (order_id,)
        )
        row = cursor.fetchone()
        if not row:
            return None
        items = _get_order_items(cursor, order_id)
        return {
            "invoice_id": int(row[0]),
            "invoice_number": str(row[1]),
            "invoice_date": _format_datetime(row[2], "N/A"),
            "total_amount": float(row[3]),
            "invoice_status": str(row[4]),
            "customer_email": str(row[5]) if row[5] else "",
            "order_id": int(row[6]),
            "order_date": _format_datetime(row[7], "N/A"),
            "customer_name": str(row[8]),
            "items": items,
        }
    finally:
        _safe_close(cursor, conn)


def _send_invoice_email(order_id):
    invoice_data = _load_invoice_for_email(order_id)
    if not invoice_data:
        return False, "Invoice not found."
    if not invoice_data["customer_email"]:
        return False, "Customer email is missing."
    if not app.config["SMTP_USERNAME"] or not app.config["SMTP_PASSWORD"]:
        return False, "SMTP credentials are missing."

    html = _build_invoice_email_html(invoice_data)
    message = MIMEMultipart("alternative")
    message["Subject"] = f"Invoice {invoice_data['invoice_number']} - Order #{invoice_data['order_id']}"
    message["From"] = app.config["MAIL_FROM"]
    message["To"] = invoice_data["customer_email"]
    message.attach(MIMEText(html, "html"))

    server = None
    try:
        server = smtplib.SMTP(app.config["SMTP_HOST"], app.config["SMTP_PORT"])
        server.starttls()
        server.login(app.config["SMTP_USERNAME"], app.config["SMTP_PASSWORD"])
        server.sendmail(app.config["MAIL_FROM"], [invoice_data["customer_email"]], message.as_string())
    finally:
        if server:
            try:
                server.quit()
            except Exception:
                pass

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE dbo.Invoices
            SET emailed_at = GETDATE()
            WHERE order_id = ?
            """,
            (order_id,)
        )
        conn.commit()
    finally:
        _safe_close(cursor, conn)

    return True, None


def _create_checkout_session(order_id, invoice_id, customer_email, items):
    if not app.config["STRIPE_SECRET_KEY"]:
        return None

    line_items = []
    for item in items:
        unit_amount = int(round(float(item["unit_price"]) * 100))
        line_items.append({
            "price_data": {
                "currency": app.config["STRIPE_CURRENCY"],
                "product_data": {"name": item["product_name"]},
                "unit_amount": unit_amount,
            },
            "quantity": int(item["quantity"]),
        })

    checkout_session = stripe.checkout.Session.create(
        mode="payment",
        payment_method_types=["card"],
        customer_email=customer_email if customer_email else None,
        line_items=line_items,
        metadata={"order_id": str(order_id), "invoice_id": str(invoice_id)},
        success_url=f"{_get_base_url()}/customer/payment/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{_get_base_url()}/customer/payment/cancel?order_id={order_id}&invoice_id={invoice_id}",
    )

    _update_invoice_checkout_session(invoice_id, checkout_session.id)
    return checkout_session


def _load_invoice_download_data(order_id, customer_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT
                o.order_id,
                o.order_date,
                o.total_amount,
                o.order_status,
                i.invoice_id,
                i.invoice_number,
                i.invoice_date,
                i.invoice_status,
                i.customer_email,
                p.gateway_status,
                u.full_name
            FROM dbo.Orders o
            INNER JOIN dbo.Users u ON u.user_id = o.customer_id
            LEFT JOIN dbo.Invoices i ON i.order_id = o.order_id
            LEFT JOIN dbo.Payments p ON p.invoice_id = i.invoice_id
            WHERE o.order_id = ? AND o.customer_id = ?
            """,
            (order_id, customer_id)
        )
        row = cursor.fetchone()
        if not row:
            return None
        items = _get_order_items(cursor, order_id)
        return {
            "order_id": int(row[0]),
            "order_date_display": _format_datetime(row[1], "N/A"),
            "total_amount": float(row[2]) if row[2] is not None else 0.0,
            "order_status": str(row[3]) if row[3] else "Pending",
            "invoice_id": int(row[4]) if row[4] is not None else None,
            "invoice_number": str(row[5]) if row[5] else f"INV-ORDER-{order_id}",
            "invoice_date_display": _format_datetime(row[6], "Not generated yet"),
            "invoice_status": str(row[7]) if row[7] else "Unpaid",
            "customer_email": str(row[8]) if row[8] else "",
            "payment_status": str(row[9]) if row[9] else "Pending",
            "customer_name": str(row[10]) if row[10] else "Customer",
            "items": items,
        }
    finally:
        _safe_close(cursor, conn)


def _build_invoice_download_html(invoice):
    item_rows = ""
    for item in invoice["items"]:
        line_total = float(item["line_total"])
        item_rows += f"""
        <tr>
            <td style=\"padding:12px;border:1px solid #cbd5e1;\">{item['product_name']}</td>
            <td style=\"padding:12px;border:1px solid #cbd5e1;text-align:center;\">{item['quantity']}</td>
            <td style=\"padding:12px;border:1px solid #cbd5e1;text-align:right;\">LKR {item['unit_price']:.2f}</td>
            <td style=\"padding:12px;border:1px solid #cbd5e1;text-align:right;\">LKR {line_total:.2f}</td>
        </tr>
        """

    return f"""<!DOCTYPE html>
<html lang=\"en\"><head><meta charset=\"UTF-8\"><title>{invoice['invoice_number']}</title>
<style>
body{{font-family:Arial,sans-serif;background:#f8fafc;margin:0;padding:30px;color:#0f172a;}}
.wrapper{{max-width:900px;margin:0 auto;background:#ffffff;border:1px solid #cbd5e1;border-radius:18px;overflow:hidden;}}
.header{{background:#0f172a;color:#ffffff;padding:28px;}}
.header h1{{margin:0 0 8px;font-size:28px;}}
.content{{padding:28px;}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:24px;}}
.card{{background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px;padding:14px;}}
table{{width:100%;border-collapse:collapse;margin-top:20px;}}
th{{background:#e2e8f0;padding:12px;border:1px solid #cbd5e1;text-align:left;}}
.total{{margin-top:24px;text-align:right;font-size:20px;font-weight:bold;}}
</style></head>
<body><div class=\"wrapper\"><div class=\"header\"><h1>IslandLink Distribution Platform</h1><div>Digital Invoice</div></div>
<div class=\"content\"><div class=\"grid\">
<div class=\"card\"><strong>Invoice Number:</strong><br>{invoice['invoice_number']}</div>
<div class=\"card\"><strong>Invoice Date:</strong><br>{invoice['invoice_date_display']}</div>
<div class=\"card\"><strong>Order ID:</strong><br>#{invoice['order_id']}</div>
<div class=\"card\"><strong>Order Date:</strong><br>{invoice['order_date_display']}</div>
<div class=\"card\"><strong>Customer Name:</strong><br>{invoice['customer_name']}</div>
<div class=\"card\"><strong>Customer Email:</strong><br>{invoice['customer_email']}</div>
<div class=\"card\"><strong>Invoice Status:</strong><br>{invoice['invoice_status']}</div>
<div class=\"card\"><strong>Payment Status:</strong><br>{invoice['payment_status']}</div>
</div>
<table><thead><tr><th>Product</th><th style=\"text-align:center;\">Qty</th><th style=\"text-align:right;\">Unit Price</th><th style=\"text-align:right;\">Line Total</th></tr></thead>
<tbody>{item_rows}</tbody></table><div class=\"total\">Grand Total: LKR {invoice['total_amount']:.2f}</div></div></div></body></html>"""


def load_dashboard_data():
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM dbo.Products")
        total_products = cursor.fetchone()[0]

        cursor.execute(
            """
            SELECT COUNT(*)
            FROM dbo.Orders
            WHERE order_status IN ('Pending', 'Confirmed', 'Packed', 'Out for Delivery')
            """
        )
        active_orders = cursor.fetchone()[0]

        cursor.execute(
            """
            SELECT COUNT(*)
            FROM dbo.Deliveries
            WHERE delivery_status IN ('Pending', 'Processing', 'Out for Delivery')
            """
        )
        pending_deliveries = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM dbo.Products WHERE stock_quantity < 10")
        low_stock = cursor.fetchone()[0]

        cursor.execute(
            """
            SELECT TOP 7 CONVERT(VARCHAR(10), order_date, 120) AS order_day, COUNT(*) AS total_orders
            FROM dbo.Orders
            GROUP BY CONVERT(VARCHAR(10), order_date, 120)
            ORDER BY order_day DESC
            """
        )
        chart_rows = list(reversed(cursor.fetchall()))
        chart_labels = [row[0] for row in chart_rows]
        chart_values = [row[1] for row in chart_rows]

        cursor.execute(
            """
            SELECT TOP 4 product_name, stock_quantity
            FROM dbo.Products
            ORDER BY stock_quantity ASC, product_name ASC
            """
        )
        stock_rows = cursor.fetchall()
        stock_data = []
        low_stock_products = []
        for row in stock_rows:
            product_name = row[0]
            stock_quantity = int(row[1])
            percentage = 100 if stock_quantity >= 100 else max(stock_quantity, 0)
            if stock_quantity <= 10:
                color, status_text = "red", "Critical"
                low_stock_products.append(product_name)
            elif stock_quantity <= 25:
                color, status_text = "amber", "Low"
            elif stock_quantity <= 60:
                color, status_text = "cyan", "Moderate"
            else:
                color, status_text = "green", "Good"
            stock_data.append({
                "product_name": product_name,
                "stock_quantity": stock_quantity,
                "percentage": percentage,
                "color": color,
                "status_text": status_text,
            })

        cursor.execute(
            """
            SELECT TOP 5 o.order_id, u.full_name, o.total_amount, o.order_status, o.order_date
            FROM dbo.Orders o
            INNER JOIN dbo.Users u ON o.customer_id = u.user_id
            ORDER BY o.order_date DESC
            """
        )
        recent_orders = cursor.fetchall()

        cursor.execute("SELECT TOP 1 total_amount FROM dbo.Orders ORDER BY order_date DESC")
        latest_order = cursor.fetchone()
        latest_order_amount = float(latest_order[0]) if latest_order else 0.0

        cursor.execute(
            """
            SELECT ISNULL(SUM(total_amount), 0)
            FROM dbo.Orders
            WHERE CAST(order_date AS DATE) = CAST(GETDATE() AS DATE)
            """
        )
        daily_revenue = float(cursor.fetchone()[0])

        cursor.execute(
            """
            SELECT ISNULL(SUM(total_amount), 0)
            FROM dbo.Orders
            WHERE YEAR(order_date) = YEAR(GETDATE()) AND MONTH(order_date) = MONTH(GETDATE())
            """
        )
        monthly_revenue = float(cursor.fetchone()[0])

        return {
            "total_products": total_products,
            "active_orders": active_orders,
            "pending_deliveries": pending_deliveries,
            "low_stock": low_stock,
            "chart_labels": chart_labels,
            "chart_values": chart_values,
            "stock_data": stock_data,
            "recent_orders": recent_orders,
            "latest_order_amount": latest_order_amount,
            "daily_revenue": daily_revenue,
            "monthly_revenue": monthly_revenue,
            "low_stock_products": low_stock_products,
        }
    finally:
        _safe_close(cursor, conn)


@app.route("/")
def home():
    return render_template("home.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "").strip()

        if not email or not password:
            flash("Please enter both email and password.", "error")
            return render_template("login.html")

        conn = None
        cursor = None
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT user_id, full_name, email, password_hash, role, is_active
                FROM dbo.Users
                WHERE LOWER(email) = ?
                """,
                (email,),
            )
            user = cursor.fetchone()
            if user is None:
                flash("Invalid email or password.", "error")
                return render_template("login.html")
            if not user[5]:
                flash("Your account is inactive.", "error")
                return render_template("login.html")

            stored_password = str(user[3] or "")
            password_ok = False
            if stored_password.startswith("pbkdf2:") or stored_password.startswith("scrypt:"):
                try:
                    password_ok = check_password_hash(stored_password, password)
                except Exception:
                    password_ok = False
            else:
                password_ok = stored_password == password

            if not password_ok:
                flash("Invalid email or password.", "error")
                return render_template("login.html")

            session["user_id"] = int(user[0])
            session["full_name"] = str(user[1])
            session["email"] = str(user[2])
            session["role"] = str(user[4])

            if session["role"] == "customer":
                return redirect(url_for("customer_dashboard"))
            if session["role"] in ["staff", "admin"]:
                return redirect(url_for("admin_dashboard"))

            flash("Invalid role.", "error")
            return render_template("login.html")
        except Exception as e:
            flash(f"Database error: {str(e)}", "error")
            return render_template("login.html")
        finally:
            _safe_close(cursor, conn)

    return render_template("login.html")


@app.route("/register/customer", methods=["GET", "POST"])
def customer_register():
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "").strip()
        confirm_password = request.form.get("confirm_password", "").strip()

        if not full_name or not email or not password or not confirm_password:
            flash("Please fill in all fields.", "error")
            return render_template("customer_register.html")
        if password != confirm_password:
            flash("Passwords do not match.", "error")
            return render_template("customer_register.html")
        if len(password) < 6:
            flash("Password must be at least 6 characters.", "error")
            return render_template("customer_register.html")

        conn = None
        cursor = None
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT user_id FROM dbo.Users WHERE LOWER(email) = ?", (email,))
            if cursor.fetchone():
                flash("This email is already registered.", "error")
                return render_template("customer_register.html")

            cursor.execute(
                """
                INSERT INTO dbo.Users (full_name, email, password_hash, role, is_active)
                VALUES (?, ?, ?, 'customer', 1)
                """,
                (full_name, email, generate_password_hash(password)),
            )
            conn.commit()
            flash("Customer account created successfully. Please sign in.", "success")
            return redirect(url_for("login"))
        except Exception as e:
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
            flash(f"Registration error: {str(e)}", "error")
            return render_template("customer_register.html")
        finally:
            _safe_close(cursor, conn)

    return render_template("customer_register.html")


@app.route("/customer/dashboard")
def customer_dashboard():
    guard = _customer_guard()
    if guard:
        return guard
    return render_template("customer_dashboard.html", name=session.get("full_name"))


@app.route("/customer/products")
def customer_products():
    guard = _customer_guard()
    if guard:
        return guard

    search = request.args.get("search", "").strip()
    category = request.args.get("category", "").strip()

    conn = None
    cursor = None

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT DISTINCT category
            FROM dbo.Products
            WHERE is_active = 1
              AND category IS NOT NULL
              AND LTRIM(RTRIM(category)) <> ''
            ORDER BY category ASC
            """
        )
        categories = [row[0] for row in cursor.fetchall()]

        query = """
            SELECT product_id, product_name, category, price, stock_quantity, image_path
            FROM dbo.Products
            WHERE is_active = 1
        """
        params = []

        if search:
            query += " AND product_name LIKE ?"
            params.append(f"%{search}%")

        if category:
            query += " AND category = ?"
            params.append(category)

        query += " ORDER BY product_id DESC"

        cursor.execute(query, params)
        products = cursor.fetchall()

        return render_template(
            "customer_products.html",
            name=session.get("full_name"),
            products=products,
            search=search,
            category=category,
            categories=categories
        )

    except Exception as e:
        flash(f"Products page error: {str(e)}", "error")
        return redirect(url_for("customer_dashboard"))

    finally:
        _safe_close(cursor, conn)


@app.route("/customer/product/<int:product_id>")
def customer_product_detail(product_id):
    guard = _customer_guard()
    if guard:
        return guard

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        product = _fetch_product(cursor, product_id)
        if not product:
            flash("Product not found.", "error")
            return redirect(url_for("customer_products"))
        if int(product[5]) != 1:
            flash("This product is not available right now.", "error")
            return redirect(url_for("customer_products"))

        return render_template(
            "customer_product_detail.html",
            name=session.get("full_name"),
            product=product,
        )
    except Exception as e:
        flash(f"Product details error: {str(e)}", "error")
        return redirect(url_for("customer_products"))
    finally:
        _safe_close(cursor, conn)


@app.route("/customer/add-to-cart/<int:product_id>", methods=["POST"])
def add_to_cart(product_id):
    guard = _customer_guard()
    if guard:
        return guard

    conn = None
    cursor = None
    try:
        quantity = _safe_int(request.form.get("quantity", 1), default=1, minimum=1)
        conn = get_db_connection()
        cursor = conn.cursor()
        product = _fetch_product(cursor, product_id)
        if not product:
            flash("Product not found.", "error")
            return redirect(url_for("customer_products"))
        if int(product[5]) != 1:
            flash("Product is inactive.", "error")
            return redirect(url_for("customer_products"))
        if int(product[4]) <= 0:
            flash("Product is out of stock.", "error")
            return redirect(url_for("customer_product_detail", product_id=product_id))

        quantity = min(quantity, int(product[4]))
        cart = session.get("cart", {})
        key = str(product[0])
        existing_qty = cart.get(key, {}).get("quantity", 0)
        final_qty = min(existing_qty + quantity, int(product[4]))
        cart[key] = {
            "product_id": int(product[0]),
            "product_name": product[1],
            "category": product[2],
            "price": float(product[3]),
            "quantity": final_qty,
            "stock_quantity": int(product[4]),
            "image_path": product[6],
        }
        session["cart"] = cart
        session.modified = True
        flash("Product added to cart.", "success")
        return redirect(url_for("customer_cart"))
    except Exception as e:
        flash(f"Add to cart error: {str(e)}", "error")
        return redirect(url_for("customer_products"))
    finally:
        _safe_close(cursor, conn)


@app.route("/customer/buy-now/<int:product_id>", methods=["POST"])
def buy_now(product_id):
    guard = _customer_guard()
    if guard:
        return guard

    conn = None
    cursor = None
    items = []
    invoice_number = None
    try:
        quantity = _safe_int(request.form.get("quantity", 1), default=1, minimum=1)
        conn = get_db_connection()
        conn.autocommit = False
        cursor = conn.cursor()
        product = _fetch_product(cursor, product_id)

        if not product:
            raise Exception("Product not found.")
        if int(product[5]) != 1:
            raise Exception("Product is inactive.")
        if int(product[4]) <= 0:
            raise Exception("Product is out of stock.")
        if quantity > int(product[4]):
            raise Exception(f"Only {product[4]} item(s) available in stock.")

        items = [{
            "product_id": int(product[0]),
            "product_name": product[1],
            "quantity": quantity,
            "unit_price": float(product[3]),
        }]

        order_id, _, invoice_id, invoice_number = _create_order(
            cursor, session["user_id"], session.get("email"), items
        )
        conn.commit()
    except Exception as e:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        flash(f"Buy now error: {str(e)}", "error")
        return redirect(url_for("customer_product_detail", product_id=product_id))
    finally:
        _safe_close(cursor, conn)

    try:
        checkout_session = _create_checkout_session(order_id, invoice_id, session.get("email"), items)
        if checkout_session:
            return redirect(checkout_session.url)

        sent, email_error = _send_invoice_email(order_id)
        if sent:
            flash(f"Order created. Invoice {invoice_number} emailed successfully.", "success")
        else:
            flash(f"Order created. Invoice {invoice_number} generated, but email failed: {email_error}", "error")
        return redirect(url_for("customer_orders"))
    except Exception as e:
        flash(f"Order created and invoice {invoice_number} generated, but payment checkout could not start: {str(e)}", "error")
        return redirect(url_for("customer_orders"))


@app.route("/customer/cart")
def customer_cart():
    guard = _customer_guard()
    if guard:
        return guard

    conn = None
    cursor = None
    try:
        cart = session.get("cart", {})
        if not cart:
            return render_template("customer_cart.html", name=session.get("full_name"), cart_items=[], total_amount=0)

        conn = get_db_connection()
        cursor = conn.cursor()
        updated_cart = {}
        cart_items = []
        total_amount = 0.0

        for raw_item in cart.values():
            product = _fetch_product(cursor, raw_item["product_id"])
            if not product or int(product[5]) != 1 or int(product[4]) <= 0:
                continue

            quantity = min(int(raw_item["quantity"]), int(product[4]))
            item = {
                "product_id": int(product[0]),
                "product_name": product[1],
                "category": product[2],
                "price": float(product[3]),
                "quantity": quantity,
                "stock_quantity": int(product[4]),
                "image_path": product[6],
            }
            updated_cart[str(product[0])] = item
            cart_items.append(item)
            total_amount += item["price"] * item["quantity"]

        session["cart"] = updated_cart
        session.modified = True
        return render_template("customer_cart.html", name=session.get("full_name"), cart_items=cart_items, total_amount=total_amount)
    except Exception as e:
        flash(f"Cart page error: {str(e)}", "error")
        return redirect(url_for("customer_products"))
    finally:
        _safe_close(cursor, conn)


@app.route("/customer/cart/update/<int:product_id>", methods=["POST"])
def update_cart(product_id):
    guard = _customer_guard()
    if guard:
        return guard

    conn = None
    cursor = None
    try:
        quantity = _safe_int(request.form.get("quantity", 1), default=1, minimum=0)
        cart = session.get("cart", {})
        key = str(product_id)

        if key not in cart:
            flash("Item not found in cart.", "error")
            return redirect(url_for("customer_cart"))
        if quantity == 0:
            del cart[key]
            session["cart"] = cart
            session.modified = True
            flash("Item removed from cart.", "success")
            return redirect(url_for("customer_cart"))

        conn = get_db_connection()
        cursor = conn.cursor()
        product = _fetch_product(cursor, product_id)
        if not product or int(product[5]) != 1 or int(product[4]) <= 0:
            del cart[key]
            session["cart"] = cart
            session.modified = True
            flash("Item is no longer available and was removed from your cart.", "error")
            return redirect(url_for("customer_cart"))

        cart[key]["quantity"] = min(quantity, int(product[4]))
        cart[key]["price"] = float(product[3])
        cart[key]["stock_quantity"] = int(product[4])
        cart[key]["image_path"] = product[6]
        session["cart"] = cart
        session.modified = True
        flash("Cart updated successfully.", "success")
        return redirect(url_for("customer_cart"))
    except Exception as e:
        flash(f"Cart update error: {str(e)}", "error")
        return redirect(url_for("customer_cart"))
    finally:
        _safe_close(cursor, conn)


@app.route("/customer/cart/remove/<int:product_id>", methods=["POST"])
def remove_from_cart(product_id):
    guard = _customer_guard()
    if guard:
        return guard

    cart = session.get("cart", {})
    key = str(product_id)
    if key in cart:
        del cart[key]
        session["cart"] = cart
        session.modified = True
        flash("Item removed from cart.", "success")
    return redirect(url_for("customer_cart"))


@app.route("/customer/cart/checkout", methods=["POST"])
def checkout_cart():
    guard = _customer_guard()
    if guard:
        return guard

    cart = session.get("cart", {})
    if not cart:
        flash("Your cart is empty.", "error")
        return redirect(url_for("customer_cart"))

    conn = None
    cursor = None
    validated_items = []
    invoice_number = None
    try:
        conn = get_db_connection()
        conn.autocommit = False
        cursor = conn.cursor()

        for item in cart.values():
            product = _fetch_product(cursor, item["product_id"])
            if not product:
                raise Exception(f"Product not found: {item['product_name']}")
            if int(product[5]) != 1:
                raise Exception(f"Product is inactive: {product[1]}")
            if int(product[4]) < int(item["quantity"]):
                raise Exception(f"Not enough stock for {product[1]}. Available stock: {product[4]}")

            validated_items.append({
                "product_id": int(product[0]),
                "product_name": product[1],
                "quantity": int(item["quantity"]),
                "unit_price": float(product[3]),
            })

        order_id, _, invoice_id, invoice_number = _create_order(
            cursor, session["user_id"], session.get("email"), validated_items
        )
        conn.commit()
        session["cart"] = {}
        session.modified = True
    except Exception as e:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        flash(f"Checkout error: {str(e)}", "error")
        return redirect(url_for("customer_cart"))
    finally:
        _safe_close(cursor, conn)

    try:
        checkout_session = _create_checkout_session(order_id, invoice_id, session.get("email"), validated_items)
        if checkout_session:
            return redirect(checkout_session.url)

        sent, email_error = _send_invoice_email(order_id)
        if sent:
            flash(f"Order created. Invoice {invoice_number} emailed successfully.", "success")
        else:
            flash(f"Order created. Invoice {invoice_number} generated, but email failed: {email_error}", "error")
        return redirect(url_for("customer_orders"))
    except Exception as e:
        flash(f"Order created and invoice {invoice_number} generated, but payment checkout could not start: {str(e)}", "error")
        return redirect(url_for("customer_orders"))


@app.route("/customer/payment/success")
def customer_payment_success():
    guard = _customer_guard()
    if guard:
        return guard

    session_id = request.args.get("session_id", "").strip()
    if not session_id:
        flash("Missing payment session.", "error")
        return redirect(url_for("customer_orders"))
    if not app.config["STRIPE_SECRET_KEY"]:
        flash("Stripe is not configured.", "error")
        return redirect(url_for("customer_orders"))

    try:
        checkout_session = stripe.checkout.Session.retrieve(session_id)
        metadata = checkout_session.get("metadata", {}) or {}
        order_id = int(metadata.get("order_id"))
        invoice_id = int(metadata.get("invoice_id"))
        payment_intent_id = checkout_session.get("payment_intent")

        if checkout_session.get("payment_status") == "paid":
            _mark_invoice_paid(order_id, invoice_id, payment_intent_id, session_id)
            sent, email_error = _send_invoice_email(order_id)
            if sent:
                flash("Payment successful. Invoice emailed to your account.", "success")
            else:
                flash(f"Payment successful, but invoice email failed: {email_error}", "error")
        else:
            flash("Payment session finished, but payment is not marked as paid yet.", "error")
    except Exception as e:
        flash(f"Payment confirmation error: {str(e)}", "error")

    return redirect(url_for("customer_orders"))


@app.route("/customer/payment/cancel")
def customer_payment_cancel():
    guard = _customer_guard()
    if guard:
        return guard

    order_id = request.args.get("order_id", "").strip()
    invoice_id = request.args.get("invoice_id", "").strip()
    try:
        if order_id and invoice_id:
            _mark_payment_cancelled(int(order_id), int(invoice_id))
    except Exception:
        pass

    flash("Payment was cancelled. Your order exists, but payment is still pending.", "error")
    return redirect(url_for("customer_orders"))


@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature", "")
    if not app.config["STRIPE_WEBHOOK_SECRET"]:
        return jsonify({"error": "Webhook secret not configured"}), 400

    try:
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=app.config["STRIPE_WEBHOOK_SECRET"],
        )
    except ValueError:
        return jsonify({"error": "Invalid payload"}), 400
    except stripe.error.SignatureVerificationError:
        return jsonify({"error": "Invalid signature"}), 400

    if event["type"] == "checkout.session.completed":
        checkout_session = event["data"]["object"]
        metadata = checkout_session.get("metadata", {}) or {}
        order_id = metadata.get("order_id")
        invoice_id = metadata.get("invoice_id")
        payment_intent_id = checkout_session.get("payment_intent")
        session_id = checkout_session.get("id")
        if order_id and invoice_id:
            try:
                _mark_invoice_paid(int(order_id), int(invoice_id), payment_intent_id, session_id)
                _send_invoice_email(int(order_id))
            except Exception:
                pass

    return jsonify({"received": True}), 200


@app.route("/customer/orders")
def customer_orders():
    guard = _customer_guard()
    if guard:
        return guard

    order_filter = request.args.get("order_id", "").strip()
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        query = """
            SELECT
                o.order_id,
                o.order_date,
                o.total_amount,
                o.order_status,
                d.delivery_status,
                d.estimated_date,
                d.delivered_date,
                d.tracking_note,
                i.invoice_id,
                i.invoice_number,
                i.invoice_status,
                i.invoice_date,
                p.gateway_status
            FROM dbo.Orders o
            LEFT JOIN dbo.Deliveries d ON d.order_id = o.order_id
            LEFT JOIN dbo.Invoices i ON i.order_id = o.order_id
            LEFT JOIN dbo.Payments p ON p.invoice_id = i.invoice_id
            WHERE o.customer_id = ?
        """
        params = [session["user_id"]]
        if order_filter:
            query += " AND CAST(o.order_id AS VARCHAR(50)) LIKE ?"
            params.append(f"%{order_filter}%")
        query += " ORDER BY o.order_date DESC"

        cursor.execute(query, params)
        rows = cursor.fetchall()

        orders = []
        active_count = delivered_count = cancelled_count = 0
        for row in rows:
            order_status = str(row[3]).strip() if row[3] is not None else "Pending"
            delivery_status = str(row[4]).strip() if row[4] is not None else "Pending"
            invoice_number = str(row[9]).strip() if row[9] else "Not Generated"
            invoice_status = str(row[10]).strip() if row[10] else "Unpaid"
            payment_status = str(row[12]).strip() if row[12] else "Pending"
            orders.append({
                "order_id": int(row[0]),
                "order_date": _to_datetime(row[1]),
                "order_date_display": _format_datetime(row[1], "N/A"),
                "total_amount": float(row[2]) if row[2] is not None else 0.0,
                "order_status": order_status,
                "delivery_status": delivery_status,
                "estimated_date": _to_datetime(row[5]),
                "estimated_date_display": _format_date(row[5], "Will be updated soon"),
                "delivered_date": _to_datetime(row[6]),
                "delivered_date_display": _format_date(row[6], "Not delivered yet"),
                "tracking_note": str(row[7]).strip() if row[7] else "No tracking note available yet.",
                "invoice_id": int(row[8]) if row[8] is not None else None,
                "invoice_number": invoice_number,
                "invoice_status": invoice_status,
                "invoice_date": _to_datetime(row[11]),
                "invoice_date_display": _format_datetime(row[11], "Not generated yet"),
                "payment_status": payment_status,
            })
            if order_status == "Delivered":
                delivered_count += 1
            elif order_status == "Cancelled":
                cancelled_count += 1
            else:
                active_count += 1

        return render_template(
            "customer_orders.html",
            name=session.get("full_name"),
            orders=orders,
            order_filter=order_filter,
            total_orders=len(orders),
            active_count=active_count,
            delivered_count=delivered_count,
            cancelled_count=cancelled_count,
        )
    except Exception as e:
        flash(f"Orders page error: {str(e)}", "error")
        return redirect(url_for("customer_dashboard"))
    finally:
        _safe_close(cursor, conn)


@app.route("/customer/invoice/download/<int:order_id>")
def download_invoice(order_id):
    guard = _customer_guard()
    if guard:
        return guard
    try:
        invoice = _load_invoice_download_data(order_id, session["user_id"])
        if not invoice:
            flash("Invoice not found for this order.", "error")
            return redirect(url_for("customer_orders"))
        html = _build_invoice_download_html(invoice)
        filename = f"{invoice['invoice_number']}.html"
        return Response(html, mimetype="text/html", headers={"Content-Disposition": f"attachment; filename={filename}"})
    except Exception as e:
        flash(f"Invoice download error: {str(e)}", "error")
        return redirect(url_for("customer_orders"))


@app.route("/customer/tracking")
def customer_tracking():
    guard = _customer_guard()
    if guard:
        return guard

    order_filter = request.args.get("order_id", "").strip()
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        query = """
            SELECT d.delivery_id, o.order_id, o.order_date, o.total_amount, o.order_status,
                   d.delivery_status, d.estimated_date, d.delivered_date, d.tracking_note
            FROM dbo.Deliveries d
            INNER JOIN dbo.Orders o ON d.order_id = o.order_id
            WHERE o.customer_id = ?
        """
        params = [session["user_id"]]
        if order_filter:
            query += " AND CAST(o.order_id AS VARCHAR(50)) LIKE ?"
            params.append(f"%{order_filter}%")
        query += " ORDER BY o.order_date DESC, d.delivery_id DESC"

        cursor.execute(query, params)
        rows = cursor.fetchall()
        deliveries = []
        pending_count = processing_count = out_for_delivery_count = delivered_count = 0

        for row in rows:
            delivery_status = str(row[5]).strip() if row[5] is not None else "Pending"
            order_status = str(row[4]).strip() if row[4] is not None else "Pending"
            step = 1
            if delivery_status == "Processing":
                step = 2
            elif delivery_status == "Out for Delivery":
                step = 3
            elif delivery_status == "Delivered":
                step = 4

            deliveries.append({
                "delivery_id": int(row[0]),
                "order_id": int(row[1]),
                "order_date": _to_datetime(row[2]),
                "order_date_display": _format_datetime(row[2], "N/A"),
                "total_amount": float(row[3]) if row[3] is not None else 0.0,
                "order_status": order_status,
                "delivery_status": delivery_status,
                "estimated_date": _to_datetime(row[6]),
                "estimated_date_display": _format_date(row[6], "Will be updated soon"),
                "delivered_date": _to_datetime(row[7]),
                "delivered_date_display": _format_date(row[7], "Not delivered yet"),
                "tracking_note": str(row[8]).strip() if row[8] else "No tracking note available yet.",
                "step": step,
            })

            if delivery_status == "Pending":
                pending_count += 1
            elif delivery_status == "Processing":
                processing_count += 1
            elif delivery_status == "Out for Delivery":
                out_for_delivery_count += 1
            elif delivery_status == "Delivered":
                delivered_count += 1

        return render_template(
            "customer_tracking.html",
            name=session.get("full_name"),
            deliveries=deliveries,
            order_filter=order_filter,
            pending_count=pending_count,
            processing_count=processing_count,
            out_for_delivery_count=out_for_delivery_count,
            delivered_count=delivered_count,
            total_deliveries=len(deliveries),
        )
    except Exception as e:
        flash(f"Tracking page error: {str(e)}", "error")
        return redirect(url_for("customer_dashboard"))
    finally:
        _safe_close(cursor, conn)


@app.route("/admin/dashboard")
def admin_dashboard():
    guard = _admin_guard()
    if guard:
        return guard
    try:
        data = load_dashboard_data()
        return render_template(
            "admin_dashboard.html",
            name=session.get("full_name"),
            total_products=data["total_products"],
            active_orders=data["active_orders"],
            pending_deliveries=data["pending_deliveries"],
            low_stock=data["low_stock"],
            chart_labels=data["chart_labels"],
            chart_values=data["chart_values"],
            stock_data=data["stock_data"],
            recent_orders=data["recent_orders"],
            daily_revenue=data["daily_revenue"],
            monthly_revenue=data["monthly_revenue"],
            latest_order_amount=data["latest_order_amount"],
            low_stock_products=data["low_stock_products"],
        )
    except Exception as e:
        flash(f"Dashboard error: {str(e)}", "error")
        return redirect(url_for("login"))


@app.route("/admin/dashboard/data")
def admin_dashboard_data():
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    if session.get("role") not in ["staff", "admin"]:
        return jsonify({"error": "Forbidden"}), 403
    try:
        data = load_dashboard_data()
        return jsonify({
            "total_products": data["total_products"],
            "active_orders": data["active_orders"],
            "pending_deliveries": data["pending_deliveries"],
            "low_stock": data["low_stock"],
            "chart_labels": data["chart_labels"],
            "chart_values": data["chart_values"],
            "daily_revenue": data["daily_revenue"],
            "monthly_revenue": data["monthly_revenue"],
            "latest_order_amount": data["latest_order_amount"],
            "low_stock_products": data["low_stock_products"],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/admin/products")
def admin_products():
    guard = _admin_guard()
    if guard:
        return guard

    search = request.args.get("search", "").strip()
    category = request.args.get("category", "").strip()
    stock_filter = request.args.get("stock_filter", "").strip()
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        base_query = """
            SELECT product_id, product_name, category, price, stock_quantity, is_active, image_path
            FROM dbo.Products
            WHERE 1=1
        """
        params = []
        if search:
            base_query += " AND product_name LIKE ?"
            params.append(f"%{search}%")
        if category and category != "All Categories":
            base_query += " AND category = ?"
            params.append(category)
        if stock_filter == "In Stock":
            base_query += " AND stock_quantity > 25"
        elif stock_filter == "Low Stock":
            base_query += " AND stock_quantity BETWEEN 1 AND 25"
        elif stock_filter == "Out of Stock":
            base_query += " AND stock_quantity = 0"
        base_query += " ORDER BY product_id DESC"

        cursor.execute(base_query, params)
        products = cursor.fetchall()

        cursor.execute("SELECT COUNT(*) FROM dbo.Products")
        total_products = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM dbo.Products WHERE stock_quantity > 25")
        in_stock = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM dbo.Products WHERE stock_quantity BETWEEN 1 AND 25")
        low_stock = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM dbo.Products WHERE stock_quantity = 0")
        out_of_stock = cursor.fetchone()[0]

        return render_template(
            "admin_products.html",
            name=session.get("full_name"),
            products=products,
            total_products=total_products,
            in_stock=in_stock,
            low_stock=low_stock,
            out_of_stock=out_of_stock,
            search=search,
            category=category,
            stock_filter=stock_filter,
        )
    except Exception as e:
        flash(f"Products page error: {str(e)}", "error")
        return redirect(url_for("admin_dashboard"))
    finally:
        _safe_close(cursor, conn)


@app.route("/admin/products/add", methods=["GET", "POST"])
def add_product():
    guard = _admin_guard()
    if guard:
        return guard

    if request.method == "POST":
        product_name = request.form.get("product_name", "").strip()
        category = request.form.get("category", "").strip()
        price = request.form.get("price", "").strip()
        stock_quantity = request.form.get("stock_quantity", "").strip()
        is_active = request.form.get("is_active", "1")
        image_file = request.files.get("product_image")

        if not product_name or not category or not price or not stock_quantity:
            flash("Please fill in all required fields.", "error")
            return render_template("add_product.html", name=session.get("full_name"))

        image_path = None
        conn = None
        cursor = None
        try:
            if image_file and image_file.filename:
                if not allowed_image(image_file.filename):
                    flash("Only PNG, JPG, JPEG, and WEBP images are allowed.", "error")
                    return render_template("add_product.html", name=session.get("full_name"))
                filename = f"{datetime.now().strftime('%Y%m%d%H%M%S%f')}_{secure_filename(image_file.filename)}"
                save_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
                image_file.save(save_path)
                image_path = f"uploads/products/{filename}"

            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO dbo.Products (product_name, category, price, stock_quantity, is_active, image_path)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (product_name, category, float(price), int(stock_quantity), 1 if is_active == "1" else 0, image_path),
            )
            conn.commit()
            flash("Product added successfully.", "success")
            return redirect(url_for("admin_products"))
        except Exception as e:
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
            flash(f"Error adding product: {str(e)}", "error")
        finally:
            _safe_close(cursor, conn)

    return render_template("add_product.html", name=session.get("full_name"))


@app.route("/admin/products/edit/<int:product_id>", methods=["GET", "POST"])
def edit_product(product_id):
    guard = _admin_guard()
    if guard:
        return guard

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        if request.method == "POST":
            product_name = request.form.get("product_name", "").strip()
            category = request.form.get("category", "").strip()
            price = request.form.get("price", "").strip()
            stock_quantity = request.form.get("stock_quantity", "").strip()
            is_active = request.form.get("is_active", "1")
            current_image_path = request.form.get("current_image_path", "").strip() or None
            image_file = request.files.get("product_image")
            image_path = current_image_path

            if image_file and image_file.filename:
                if not allowed_image(image_file.filename):
                    flash("Only PNG, JPG, JPEG, and WEBP images are allowed.", "error")
                    return redirect(url_for("edit_product", product_id=product_id))
                filename = f"{datetime.now().strftime('%Y%m%d%H%M%S%f')}_{secure_filename(image_file.filename)}"
                save_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
                image_file.save(save_path)
                image_path = f"uploads/products/{filename}"

            cursor.execute(
                """
                UPDATE dbo.Products
                SET product_name = ?, category = ?, price = ?, stock_quantity = ?, is_active = ?, image_path = ?
                WHERE product_id = ?
                """,
                (product_name, category, float(price), int(stock_quantity), 1 if is_active == "1" else 0, image_path, product_id),
            )
            conn.commit()
            flash("Product updated successfully.", "success")
            return redirect(url_for("admin_products"))

        cursor.execute(
            """
            SELECT product_id, product_name, category, price, stock_quantity, is_active, image_path
            FROM dbo.Products
            WHERE product_id = ?
            """,
            (product_id,),
        )
        product = cursor.fetchone()
        if not product:
            flash("Product not found.", "error")
            return redirect(url_for("admin_products"))
        return render_template("edit_product.html", name=session.get("full_name"), product=product)
    except Exception as e:
        flash(f"Edit product error: {str(e)}", "error")
        return redirect(url_for("admin_products"))
    finally:
        _safe_close(cursor, conn)


@app.route("/admin/products/update-stock/<int:product_id>", methods=["POST"])
def update_stock(product_id):
    guard = _admin_guard()
    if guard:
        return guard

    stock_quantity = request.form.get("stock_quantity", "").strip()
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE dbo.Products
            SET stock_quantity = ?
            WHERE product_id = ?
            """,
            (int(stock_quantity), product_id),
        )
        conn.commit()
        flash("Stock updated successfully.", "success")
    except Exception as e:
        flash(f"Stock update error: {str(e)}", "error")
    finally:
        _safe_close(cursor, conn)

    return redirect(url_for("admin_products"))


@app.route("/admin/products/export")
def export_products():
    guard = _admin_guard()
    if guard:
        return guard

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT product_id, product_name, category, price, stock_quantity, is_active, image_path
            FROM dbo.Products
            ORDER BY product_id DESC
            """
        )
        rows = cursor.fetchall()
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Product ID", "Product Name", "Category", "Price", "Stock Quantity", "Active", "Image Path"])
        for row in rows:
            writer.writerow(row)
        csv_data = output.getvalue()
        output.close()
        return Response(csv_data, mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=products_export.csv"})
    except Exception as e:
        flash(f"Export error: {str(e)}", "error")
        return redirect(url_for("admin_products"))
    finally:
        _safe_close(cursor, conn)


@app.route("/admin/orders")
def admin_orders():
    guard = _admin_guard()
    if guard:
        return guard

    search = request.args.get("search", "").strip()
    status_filter = request.args.get("status", "").strip()
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM dbo.Orders")
        total_orders = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM dbo.Orders WHERE order_status = 'Packed'")
        processing_count = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM dbo.Orders WHERE order_status = 'Pending'")
        on_hold_count = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM dbo.Orders WHERE order_status = 'Delivered'")
        completed_count = cursor.fetchone()[0]

        query = """
            SELECT o.order_id, u.full_name, o.order_date, o.total_amount, o.order_status
            FROM dbo.Orders o
            INNER JOIN dbo.Users u ON o.customer_id = u.user_id
            WHERE 1=1
        """
        params = []
        if search:
            query += " AND (CAST(o.order_id AS VARCHAR) LIKE ? OR u.full_name LIKE ?)"
            params.extend([f"%{search}%", f"%{search}%"])
        if status_filter and status_filter != "All Status":
            query += " AND o.order_status = ?"
            params.append(status_filter)
        query += " ORDER BY o.order_date DESC"

        cursor.execute(query, params)
        orders = cursor.fetchall()
        return render_template(
            "admin_orders.html",
            name=session.get("full_name"),
            orders=orders,
            total_orders=total_orders,
            processing_count=processing_count,
            on_hold_count=on_hold_count,
            completed_count=completed_count,
            search=search,
            status_filter=status_filter,
        )
    except Exception as e:
        flash(f"Orders page error: {str(e)}", "error")
        return redirect(url_for("admin_dashboard"))
    finally:
        _safe_close(cursor, conn)


@app.route("/admin/orders/update-status/<int:order_id>", methods=["POST"])
@app.route("/admin/orders/update-status-alt/<int:order_id>", methods=["POST"])
def update_order_status(order_id):
    guard = _admin_guard()
    if guard:
        return guard

    new_status = request.form.get("status", "").strip()
    allowed_statuses = ["Pending", "Confirmed", "Packed", "Out for Delivery", "Delivered", "Cancelled"]
    if new_status not in allowed_statuses:
        flash("Invalid status selected.", "error")
        return redirect(url_for("admin_orders"))

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE dbo.Orders SET order_status = ? WHERE order_id = ?", (new_status, order_id))

        if new_status == "Out for Delivery":
            cursor.execute("UPDATE dbo.Deliveries SET delivery_status = 'Out for Delivery' WHERE order_id = ?", (order_id,))
        elif new_status == "Delivered":
            cursor.execute(
                """
                UPDATE dbo.Deliveries
                SET delivery_status = 'Delivered', delivered_date = CAST(GETDATE() AS DATE)
                WHERE order_id = ?
                """,
                (order_id,),
            )
        elif new_status in ["Pending", "Confirmed", "Packed"]:
            cursor.execute(
                """
                UPDATE dbo.Deliveries
                SET delivery_status = CASE WHEN ? = 'Packed' THEN 'Processing' ELSE 'Pending' END
                WHERE order_id = ?
                """,
                (new_status, order_id),
            )

        conn.commit()
        flash("Order status updated successfully.", "success")
    except Exception as e:
        flash(f"Status update error: {str(e)}", "error")
    finally:
        _safe_close(cursor, conn)

    return redirect(url_for("admin_orders"))


@app.route("/admin/orders/export")
def export_orders():
    guard = _admin_guard()
    if guard:
        return guard

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT o.order_id, u.full_name, o.order_date, o.total_amount, o.order_status
            FROM dbo.Orders o
            INNER JOIN dbo.Users u ON o.customer_id = u.user_id
            ORDER BY o.order_date DESC
            """
        )
        rows = cursor.fetchall()
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Order ID", "Customer", "Order Date", "Total Amount", "Order Status"])
        for row in rows:
            writer.writerow(row)
        csv_data = output.getvalue()
        output.close()
        return Response(csv_data, mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=orders_export.csv"})
    except Exception as e:
        flash(f"Export error: {str(e)}", "error")
        return redirect(url_for("admin_orders"))
    finally:
        _safe_close(cursor, conn)


@app.route("/admin/deliveries")
def admin_deliveries():
    guard = _admin_guard()
    if guard:
        return guard

    search = request.args.get("search", "").strip()
    status_filter = request.args.get("status", "").strip()
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM dbo.Deliveries WHERE delivery_status = 'Pending'")
        pending_count = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM dbo.Deliveries WHERE delivery_status = 'Processing'")
        processing_count = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM dbo.Deliveries WHERE delivery_status = 'Out for Delivery'")
        out_for_delivery_count = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM dbo.Deliveries WHERE delivery_status = 'Delivered'")
        delivered_count = cursor.fetchone()[0]

        query = """
            SELECT d.delivery_id, d.order_id, u.full_name, d.estimated_date, d.delivered_date, d.delivery_status, d.tracking_note
            FROM dbo.Deliveries d
            INNER JOIN dbo.Orders o ON d.order_id = o.order_id
            INNER JOIN dbo.Users u ON o.customer_id = u.user_id
            WHERE 1=1
        """
        params = []
        if search:
            query += " AND (CAST(d.order_id AS VARCHAR) LIKE ? OR u.full_name LIKE ?)"
            params.extend([f"%{search}%", f"%{search}%"])
        if status_filter and status_filter != "All Status":
            query += " AND d.delivery_status = ?"
            params.append(status_filter)
        query += " ORDER BY d.delivery_id DESC"

        cursor.execute(query, params)
        deliveries = cursor.fetchall()
        return render_template(
            "admin_deliveries.html",
            name=session.get("full_name"),
            deliveries=deliveries,
            pending_count=pending_count,
            processing_count=processing_count,
            out_for_delivery_count=out_for_delivery_count,
            delivered_count=delivered_count,
            search=search,
            status_filter=status_filter,
        )
    except Exception as e:
        flash(f"Deliveries page error: {str(e)}", "error")
        return redirect(url_for("admin_dashboard"))
    finally:
        _safe_close(cursor, conn)


@app.route("/admin/deliveries/update/<int:delivery_id>", methods=["POST"])
def update_delivery(delivery_id):
    guard = _admin_guard()
    if guard:
        return guard

    delivery_status = request.form.get("delivery_status", "").strip()
    tracking_note = request.form.get("tracking_note", "").strip()
    estimated_date = request.form.get("estimated_date", "").strip()
    allowed_statuses = ["Pending", "Processing", "Out for Delivery", "Delivered"]
    if delivery_status not in allowed_statuses:
        flash("Invalid delivery status.", "error")
        return redirect(url_for("admin_deliveries"))

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        if delivery_status == "Delivered":
            cursor.execute(
                """
                UPDATE dbo.Deliveries
                SET delivery_status = ?, tracking_note = ?, estimated_date = ?, delivered_date = CAST(GETDATE() AS DATE)
                WHERE delivery_id = ?
                """,
                (delivery_status, tracking_note, estimated_date if estimated_date else None, delivery_id),
            )
        else:
            cursor.execute(
                """
                UPDATE dbo.Deliveries
                SET delivery_status = ?, tracking_note = ?, estimated_date = ?
                WHERE delivery_id = ?
                """,
                (delivery_status, tracking_note, estimated_date if estimated_date else None, delivery_id),
            )

        cursor.execute("SELECT order_id FROM dbo.Deliveries WHERE delivery_id = ?", (delivery_id,))
        row = cursor.fetchone()
        if row:
            order_id = int(row[0])
            if delivery_status == "Delivered":
                cursor.execute("UPDATE dbo.Orders SET order_status = 'Delivered' WHERE order_id = ?", (order_id,))
            elif delivery_status == "Out for Delivery":
                cursor.execute("UPDATE dbo.Orders SET order_status = 'Out for Delivery' WHERE order_id = ?", (order_id,))
            elif delivery_status == "Processing":
                cursor.execute("UPDATE dbo.Orders SET order_status = 'Packed' WHERE order_id = ?", (order_id,))

        conn.commit()
        flash("Delivery updated successfully.", "success")
    except Exception as e:
        flash(f"Delivery update error: {str(e)}", "error")
    finally:
        _safe_close(cursor, conn)

    return redirect(url_for("admin_deliveries"))


@app.route("/admin/deliveries/export")
def export_deliveries():
    guard = _admin_guard()
    if guard:
        return guard

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT d.delivery_id, d.order_id, u.full_name, d.estimated_date, d.delivered_date, d.delivery_status, d.tracking_note
            FROM dbo.Deliveries d
            INNER JOIN dbo.Orders o ON d.order_id = o.order_id
            INNER JOIN dbo.Users u ON o.customer_id = u.user_id
            ORDER BY d.delivery_id DESC
            """
        )
        rows = cursor.fetchall()
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Delivery ID", "Order ID", "Customer", "Estimated Date", "Delivered Date", "Delivery Status", "Tracking Note"])
        for row in rows:
            writer.writerow(row)
        csv_data = output.getvalue()
        output.close()
        return Response(csv_data, mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=deliveries_export.csv"})
    except Exception as e:
        flash(f"Export error: {str(e)}", "error")
        return redirect(url_for("admin_deliveries"))
    finally:
        _safe_close(cursor, conn)


@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out successfully.", "success")
    return redirect(url_for("login"))


if __name__ == "__main__":
    app.run(debug=True)
