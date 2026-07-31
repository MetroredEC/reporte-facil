"""
ReporteFácil v3 — Portal de autogestión de clientes (UI premium)
Backend idéntico a v2 (probado). Capa visual rediseñada: landing SaaS moderna,
gráficas Chart.js en reportes, design system consistente.
"""
import io
import json
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from functools import wraps

import pandas as pd
from dotenv import load_dotenv
from flask import (Flask, jsonify, redirect, render_template_string, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY") or secrets.token_hex(32)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024

MODEL = os.getenv("MODEL", "claude-sonnet-4-5")
API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "cambiame")
PAYMENT_LINK = os.getenv("PAYMENT_LINK", "")
PLAN_PRICE = os.getenv("PLAN_PRICE", "$29/mes")
SUPPORT_EMAIL = os.getenv("SUPPORT_EMAIL", "")
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
CRON_TOKEN = os.getenv("CRON_TOKEN", "")
MAIL_FROM = os.getenv("MAIL_FROM", "ReporteFacil <onboarding@resend.dev>")
APP_URL = os.getenv("APP_URL", "https://reporte-facil.onrender.com")

INDUSTRIES = ["Retail / tienda", "Restaurante / comida", "Servicios profesionales",
              "Distribución / mayorista", "Belleza / cuidado personal", "Salud",
              "Construcción / ferretería", "Otro"]
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "portal.db")

STATUSES = ["borrador", "enviada", "respondio", "ganada", "perdida"]


# ------------------------------- base de datos ------------------------------
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    con = db()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS leads (
        id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE NOT NULL,
        source TEXT NOT NULL DEFAULT 'landing',
        created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE NOT NULL,
        pw_hash TEXT NOT NULL, business TEXT DEFAULT '',
        industry TEXT DEFAULT '',
        plan_status TEXT NOT NULL DEFAULT 'free',
        created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
        filename TEXT NOT NULL, metrics TEXT NOT NULL, summary TEXT,
        created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
        subject TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'abierto',
        created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS ticket_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER NOT NULL,
        sender TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL);
    """)
    con.commit()
    con.close()


def now():
    return datetime.now(timezone.utc).isoformat()


# --------------------------------- analisis ---------------------------------
DATE_HINTS = ["fecha", "date", "dia", "día", "day", "created", "emitido"]
AMOUNT_HINTS = ["total", "monto", "amount", "venta", "valor", "precio", "price", "revenue", "importe", "subtotal"]
CATEGORY_HINTS = ["producto", "product", "item", "categoria", "categoría", "category", "servicio", "sku", "nombre"]
SELLER_HINTS = ["vendedor", "sucursal", "canal", "tienda", "staff", "seller", "empleado", "cajero"]
DOW_NAMES = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]


def find_col(df, hints, want_numeric=None):
    for h in hints:
        for c in df.columns:
            if h in str(c).lower():
                if want_numeric is True and not pd.api.types.is_numeric_dtype(df[c]):
                    continue
                return c
    if want_numeric is True:
        nums = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
        return nums[0] if nums else None
    return None


def analyze(df):
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    df = df.loc[:, ~df.columns.duplicated()]          # columnas duplicadas rompen groupby
    df = df.dropna(axis=1, how="all")                  # columnas 100% vacías no aportan
    amount_col = find_col(df, AMOUNT_HINTS, want_numeric=True)
    date_col = find_col(df, DATE_HINTS)
    cat_col = find_col(df, CATEGORY_HINTS)
    seller_col = None
    for h in SELLER_HINTS:
        for c in df.columns:
            if h in str(c).lower() and c != cat_col:
                seller_col = c
                break
        if seller_col:
            break
    out = {"filas": int(len(df)), "col_monto": amount_col, "col_fecha": date_col,
           "col_categoria": cat_col, "col_vendedor": seller_col}
    alerts = []

    if amount_col:
        s = pd.to_numeric(df[amount_col], errors="coerce").dropna()
        out.update(total=round(float(s.sum()), 2), promedio=round(float(s.mean()), 2) if len(s) else 0,
                   maximo=round(float(s.max()), 2) if len(s) else 0, transacciones=int(len(s)))

    if cat_col and amount_col:
        by_cat = (df.assign(_m=pd.to_numeric(df[amount_col], errors="coerce"))
                  .groupby(cat_col)["_m"].sum().sort_values(ascending=False))
        out["top_categorias"] = [{"nombre": str(k), "total": round(float(v), 2)} for k, v in by_cat.head(5).items()]
        if out.get("total"):
            conc = float(by_cat.iloc[0]) / out["total"] * 100
            out["concentracion_pct"] = round(conc, 1)
            if conc >= 40:
                alerts.append(f"'{by_cat.index[0]}' concentra el {conc:.0f}% de tus ventas. "
                              "Si ese producto falla, tu negocio lo siente entero: diversifica o asegura su inventario.")

    if seller_col and amount_col:
        by_seller = (df.assign(_m=pd.to_numeric(df[amount_col], errors="coerce"))
                     .groupby(seller_col)["_m"].sum().sort_values(ascending=False).head(5))
        out["por_vendedor"] = [{"nombre": str(k), "total": round(float(v), 2)} for k, v in by_seller.items()]

    if date_col and amount_col:
        d = df.copy()
        d["_f"] = pd.to_datetime(d[date_col], errors="coerce", dayfirst=True)
        d["_m"] = pd.to_numeric(d[amount_col], errors="coerce")
        d = d.dropna(subset=["_f", "_m"])
        if len(d):
            serie = d.groupby(d["_f"].dt.to_period("W"))["_m"].sum()
            out["serie_semanal"] = [{"semana": str(k), "total": round(float(v), 2)} for k, v in serie.tail(12).items()]
            if len(serie) >= 2 and float(serie.iloc[-2]) != 0:
                var = (float(serie.iloc[-1]) / float(serie.iloc[-2]) - 1) * 100
                out["variacion_pct"] = round(var, 1)
                if var <= -15:
                    alerts.append(f"Tus ventas cayeron {abs(var):.0f}% la última semana frente a la anterior. "
                                  "Revisa qué cambió: inventario, días de apertura o competencia.")
            # Ventas por día de la semana
            dow = d.groupby(d["_f"].dt.dayofweek)["_m"].sum()
            out["por_dia"] = [{"dia": DOW_NAMES[i], "total": round(float(dow.get(i, 0.0)), 2)} for i in range(7)]
            dow_nz = dow[dow > 0]
            if len(dow_nz) >= 3:
                best_i, worst_i = int(dow_nz.idxmax()), int(dow_nz.idxmin())
                out["mejor_dia"], out["peor_dia"] = DOW_NAMES[best_i], DOW_NAMES[worst_i]
                if float(dow_nz.max()) > 0 and float(dow_nz.min()) / float(dow_nz.max()) < 0.4:
                    alerts.append(f"El {DOW_NAMES[worst_i]} vendes menos de la mitad que el {DOW_NAMES[best_i]}. "
                                  "Es tu mejor día para promociones, o para reducir horario y costos.")
            # Serie mensual y crecimiento
            monthly = d.groupby(d["_f"].dt.to_period("M"))["_m"].sum()
            if len(monthly) >= 2:
                out["serie_mensual"] = [{"mes": str(k), "total": round(float(v), 2)} for k, v in monthly.tail(6).items()]
                if float(monthly.iloc[-2]) != 0:
                    mg = (float(monthly.iloc[-1]) / float(monthly.iloc[-2]) - 1) * 100
                    out["crecimiento_mensual_pct"] = round(mg, 1)
            # Productos en caída: 2a mitad del período vs 1a mitad
            if cat_col:
                mid = d["_f"].min() + (d["_f"].max() - d["_f"].min()) / 2
                first = d[d["_f"] <= mid].groupby(cat_col)["_m"].sum()
                second = d[d["_f"] > mid].groupby(cat_col)["_m"].sum()
                falling = []
                for name in first.index:
                    f1, f2 = float(first[name]), float(second.get(name, 0.0))
                    if f1 > 0:
                        chg = (f2 - f1) / f1 * 100
                        if chg <= -25:
                            falling.append({"nombre": str(name), "cambio_pct": round(chg, 1)})
                falling.sort(key=lambda x: x["cambio_pct"])
                if falling:
                    out["productos_caida"] = falling[:5]
                    worst = falling[0]
                    alerts.append(f"'{worst['nombre']}' cayó {abs(worst['cambio_pct']):.0f}% en la segunda mitad del período. "
                                  "Decide: ¿promoción para moverlo o dejar de reponerlo?")
    out["alertas"] = alerts
    return out


def ai_summary(metrics, industry=""):
    if not API_KEY:
        return None
    sys_prompt = ("Eres un analista de negocios para PYMEs de Ecuador. Con las métricas dadas, "
                  "escribe un resumen ejecutivo en español de 5-8 frases: qué va bien, qué preocupa, "
                  "y UNA recomendación accionable esta semana. Sin inventar datos de las métricas. Tono directo.")
    if industry:
        sys_prompt += (
            f" El negocio es del sector: {industry}. Añade al final un párrafo corto titulado "
            "'Contexto de tu sector' con 2-3 observaciones útiles sobre ese sector en Ecuador "
            "(estacionalidad, hábitos de compra, dinámica típica). Acláralo como orientación general, "
            "no como cifras oficiales, y no inventes estadísticas específicas.")
    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=API_KEY)
        resp = client.messages.create(
            model=MODEL, max_tokens=600, system=sys_prompt,
            messages=[{"role": "user", "content": json.dumps(metrics, ensure_ascii=False)}])
        return resp.content[0].text.strip()
    except Exception:  # noqa: BLE001
        return None


# ------------------------------ motor semanal -------------------------------
def send_email(to, subject, html):
    """Envía por Resend. Devuelve (ok, detalle)."""
    if not RESEND_API_KEY:
        return False, "RESEND_API_KEY no configurada"
    import urllib.request
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps({"from": MAIL_FROM, "to": [to], "subject": subject, "html": html}).encode(),
        headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return True, r.read().decode()[:200]
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:200]


def weekly_email_html(user, metrics, summary):
    total = metrics.get("total")
    kpis = ""
    if total is not None:
        pairs = [(f"${total:,}", "Ventas totales"), (metrics.get("transacciones", "—"), "Transacciones"),
                 (f"${metrics.get('promedio', 0):,}", "Ticket promedio")]
        if metrics.get("variacion_pct") is not None:
            v = metrics["variacion_pct"]
            pairs.append((f"{'+' if v > 0 else ''}{v}%", "vs semana anterior"))
        kpis = "".join(
            f"<td style='padding:14px 18px;background:#f6f8fb;border-radius:10px;text-align:center'>"
            f"<div style='font-size:22px;font-weight:800;color:#0e1b2c'>{v}</div>"
            f"<div style='font-size:11px;color:#42526b;text-transform:uppercase;letter-spacing:.05em'>{l}</div></td>"
            f"<td style='width:8px'></td>" for v, l in pairs)
    top = ""
    if metrics.get("top_categorias"):
        rows = "".join(
            f"<tr><td style='padding:7px 0;border-bottom:1px solid #e5eaf1'>{esc(t['nombre'])}</td>"
            f"<td style='padding:7px 0;border-bottom:1px solid #e5eaf1;text-align:right'>${t['total']:,}</td></tr>"
            for t in metrics["top_categorias"])
        top = (f"<h3 style='margin:22px 0 6px;font-size:15px'>Top {esc(str(metrics.get('col_categoria') or 'productos'))}</h3>"
               f"<table style='width:100%;border-collapse:collapse;font-size:14px'>{rows}</table>")
    sum_html = (f"<div style='background:#f0fdf7;border-left:3px solid #0e9f6e;border-radius:8px;"
                f"padding:14px 18px;margin:18px 0;font-size:14px;white-space:pre-wrap'>{esc(summary)}</div>"
                if summary else "")
    alerts_html = "".join(
        f"<div style='background:#fffbeb;border:1px solid #fde68a;border-radius:9px;"
        f"padding:10px 14px;margin:8px 0;font-size:13px;color:#78350f'>⚠️ {esc(a)}</div>"
        for a in metrics.get("alertas", []))
    if alerts_html:
        alerts_html = "<h3 style='margin:18px 0 4px;font-size:15px'>Atención esta semana</h3>" + alerts_html
    return f"""
    <div style="max-width:560px;margin:0 auto;font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#0e1b2c;padding:24px">
      <div style="font-weight:800;font-size:18px;margin-bottom:4px">Reporte<span style="color:#0e9f6e">Fácil</span></div>
      <h2 style="margin:8px 0 2px;font-size:20px">Tu resumen semanal, {esc(user['business'] or user['email'].split('@')[0])}</h2>
      <p style="color:#42526b;font-size:13px;margin:2px 0 18px">Basado en tu último reporte subido.</p>
      <table style="width:100%;border-collapse:collapse"><tr>{kpis}</tr></table>
      {alerts_html}{sum_html}{top}
      <a href="{APP_URL}/dashboard" style="display:inline-block;margin-top:20px;background:#0e9f6e;color:#fff;
        font-weight:700;padding:11px 22px;border-radius:10px;text-decoration:none;font-size:14px">
        Subir ventas de esta semana</a>
      <p style="color:#8fa3bf;font-size:11px;margin-top:26px">Recibes este correo por tu plan Pro de ReporteFácil.
      El contexto sectorial es orientación general, no cifras oficiales.</p>
    </div>"""


@app.route("/tasks/weekly")
def weekly_task():
    if not CRON_TOKEN or request.args.get("token") != CRON_TOKEN:
        return jsonify(error="no autorizado"), 403
    con = db()
    users = con.execute("SELECT * FROM users WHERE plan_status='active'").fetchall()
    sent, skipped, errors = 0, 0, []
    for u in users:
        r = con.execute("SELECT * FROM reports WHERE user_id=? ORDER BY id DESC LIMIT 1", (u["id"],)).fetchone()
        if not r:
            skipped += 1
            continue
        m = json.loads(r["metrics"])
        summary = ai_summary(m, u["industry"]) or r["summary"]
        ok, detail = send_email(u["email"], "Tu resumen semanal de ventas — ReporteFácil",
                                weekly_email_html(u, m, summary))
        if ok:
            sent += 1
        else:
            errors.append(f"{u['email']}: {detail}")
    con.close()
    return jsonify(pro_users=len(users), sent=sent, sin_reportes=skipped, errors=errors)


def parse_upload(f):
    name = (f.filename or "").lower()
    raw = f.read()
    if name.endswith(".csv"):
        try:
            return pd.read_csv(io.BytesIO(raw))
        except UnicodeDecodeError:
            return pd.read_csv(io.BytesIO(raw), encoding="latin-1")
    if name.endswith((".xlsx", ".xls")):
        return pd.read_excel(io.BytesIO(raw))
    raise ValueError("Formato no soportado. Usa .csv o .xlsx.")


# --------------------------------- helpers ----------------------------------
def login_required(fn):
    @wraps(fn)
    def w(*a, **k):
        if not session.get("uid"):
            return redirect(url_for("login_page"))
        return fn(*a, **k)
    return w


def admin_required(fn):
    @wraps(fn)
    def w(*a, **k):
        if not session.get("admin"):
            return redirect(url_for("admin_login"))
        return fn(*a, **k)
    return w


def current_user():
    if not session.get("uid"):
        return None
    con = db()
    u = con.execute("SELECT * FROM users WHERE id=?", (session["uid"],)).fetchone()
    con.close()
    return u


def esc(s):
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def page(content, title="ReporteFácil"):
    return render_template_string(
        LAYOUT.replace("__CONTENT__", content).replace("__TITLE__", title))


def kpi(v, label, accent=""):
    return f"<div class='kpi {accent}'><b>{v}</b><span>{label}</span></div>"


def err_box(msg):
    return f"<div class='card errbox'>{msg}</div>"


def back_link(href, txt="Volver"):
    return f"<a class='back' href='{href}'>&larr; {txt}</a>"


# ------------------------------ rutas públicas ------------------------------
@app.route("/")
def landing():
    logged = bool(session.get("uid"))
    nav = ('<a class="btn" href="/dashboard">Mi panel</a>' if logged else
           '<a class="navlink" href="/login">Entrar</a><a class="btn" href="/registro">Crear cuenta gratis</a>')
    return page(LANDING_BODY.replace("__NAV__", nav), "ReporteFácil — Tus ventas, explicadas cada lunes")


@app.route("/privacidad")
def privacy():
    contact = f"<p>Contacto: <a href='mailto:{esc(SUPPORT_EMAIL)}'>{esc(SUPPORT_EMAIL)}</a></p>" if SUPPORT_EMAIL else \
        "<p>Contacto: a través del sistema de soporte de tu cuenta.</p>"
    body = f"""
    <div class="topbar"><a class="brand" href="/">Reporte<span>Fácil</span></a></div>
    {back_link('/', 'Volver al inicio')}
    <div class="card legal">
      <h2>Política de privacidad</h2>
      <p><b>Tu archivo no se guarda.</b> Cuando usas el demo, tu archivo se procesa en memoria y se
      descarta al terminar el análisis. No lo almacenamos, no lo compartimos, no lo usamos para nada más.</p>
      <p><b>Con cuenta, guardamos solo lo mínimo:</b> tu correo, el nombre de tu negocio si lo das, y las
      métricas calculadas de tus reportes (totales, promedios, top de productos) para tu historial.
      El archivo original nunca se almacena.</p>
      <p><b>Tu contraseña está cifrada</b> con hashing estándar de la industria (nunca la vemos ni podemos verla).</p>
      <p><b>Pagos:</b> se procesan en plataformas externas (PayPal o pasarelas locales). Nunca ingresas
      datos de tarjeta en ReporteFácil y nunca tenemos acceso a ellos.</p>
      <p><b>No vendemos ni compartimos tus datos</b> con terceros. Puedes pedir la eliminación de tu
      cuenta y todos tus datos en cualquier momento por soporte.</p>
      {contact}
    </div>"""
    return page(body, "Privacidad — ReporteFácil")


@app.route("/terminos")
def terms():
    body = f"""
    <div class="topbar"><a class="brand" href="/">Reporte<span>Fácil</span></a></div>
    {back_link('/', 'Volver al inicio')}
    <div class="card legal">
      <h2>Términos del servicio</h2>
      <p><b>El servicio:</b> ReporteFácil genera reportes analíticos a partir de archivos de ventas que tú
      subes. Los reportes son informativos y no constituyen asesoría financiera, contable ni tributaria.</p>
      <p><b>Plan gratuito:</b> reportes manuales ilimitados, sin tarjeta ni compromiso.</p>
      <p><b>Plan Pro ({esc(PLAN_PRICE)}):</b> se paga por adelantado vía link de pago externo. La activación se
      confirma en un máximo de 24 horas. Puedes cancelar cuando quieras: la cancelación aplica al
      siguiente período y no se factura nada más.</p>
      <p><b>Reembolsos:</b> si el servicio no funciona como se describe y soporte no logra resolverlo
      en 7 días, te devolvemos el mes en curso.</p>
      <p><b>Uso aceptable:</b> no subas archivos con datos que no tengas derecho a procesar.
      Cada cuenta es para un negocio.</p>
      <p><b>Disponibilidad:</b> el servicio se ofrece "como está"; hacemos lo razonable por mantenerlo
      disponible y tus datos seguros.</p>
    </div>"""
    return page(body, "Términos — ReporteFácil")


def save_lead(email, source):
    con = db()
    try:
        con.execute("INSERT INTO leads (email, source, created_at) VALUES (?,?,?)", (email, source, now()))
        con.commit()
    except sqlite3.IntegrityError:
        con.execute("UPDATE leads SET source=? WHERE email=? AND source='landing'", (source, email))
        con.commit()
    finally:
        con.close()


@app.route("/subscribe", methods=["POST"])
def subscribe():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    source = "pro-waitlist" if data.get("source") == "pro" else "landing"
    if "@" not in email or "." not in email.split("@")[-1] or len(email) < 6:
        return jsonify(error="Correo inválido."), 400
    save_lead(email, source)
    return jsonify(ok=True)


@app.route("/report-demo", methods=["POST"])
def report_demo():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify(error="Sube un archivo .csv o .xlsx."), 400
    try:
        df = parse_upload(f)
        if df.empty:
            return jsonify(error="El archivo está vacío."), 400
        m = analyze(df)
    except Exception as e:  # noqa: BLE001
        app.logger.exception("report-demo fallo")
        return jsonify(error=f"No pude procesar el archivo ({type(e).__name__}). "
                             "Verifica que tenga encabezados en la primera fila y al menos una columna de montos. "
                             f"Detalle: {str(e)[:150]}"), 400
    return jsonify(metrics=m, resumen=ai_summary(m))


# ---------------------------------- auth ------------------------------------
def auth_form(mode):
    return AUTH_FORM.replace("__MODE__", mode).replace("__INDUSTRIES__", json.dumps(INDUSTRIES, ensure_ascii=False))


@app.route("/registro", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        return page(auth_form("registro"), "Crear cuenta — ReporteFácil")
    email = (request.form.get("email") or "").strip().lower()
    pw = request.form.get("password") or ""
    business = (request.form.get("business") or "").strip()
    industry = (request.form.get("industry") or "").strip()
    if industry not in INDUSTRIES:
        industry = ""
    if "@" not in email or len(pw) < 8:
        return page(err_box("Correo inválido o contraseña menor a 8 caracteres.")
                    + auth_form("registro"), "Crear cuenta")
    con = db()
    try:
        cur = con.execute("INSERT INTO users (email, pw_hash, business, industry, created_at) VALUES (?,?,?,?,?)",
                          (email, generate_password_hash(pw), business, industry, now()))
        con.commit()
        session["uid"] = cur.lastrowid
    except sqlite3.IntegrityError:
        con.close()
        return page(err_box("Ese correo ya tiene cuenta. <a href='/login'>Entra aquí</a>.")
                    + auth_form("registro"), "Crear cuenta")
    con.close()
    return redirect(url_for("dashboard"))


@app.route("/login", methods=["GET", "POST"])
def login_page():
    if request.method == "GET":
        return page(auth_form("login"), "Entrar — ReporteFácil")
    email = (request.form.get("email") or "").strip().lower()
    pw = request.form.get("password") or ""
    con = db()
    u = con.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    con.close()
    if not u or not check_password_hash(u["pw_hash"], pw):
        return page(err_box("Correo o contraseña incorrectos.")
                    + auth_form("login"), "Entrar")
    session["uid"] = u["id"]
    return redirect(url_for("dashboard"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


# ------------------------------- portal cliente -----------------------------
def portal_nav(u, active=""):
    open_tickets = ""
    return f"""
    <div class="topbar">
      <a class="brand" href="/">Reporte<span>Fácil</span></a>
      <nav class="tabs">
        <a href="/dashboard" class="{'on' if active == 'dash' else ''}">Panel</a>
        <a href="/plan" class="{'on' if active == 'plan' else ''}">Mi plan</a>
        <a href="/soporte" class="{'on' if active == 'sup' else ''}">Soporte</a>
        <a href="/logout">Salir</a>
      </nav>
    </div>"""


@app.route("/perfil", methods=["POST"])
@login_required
def update_profile():
    industry = (request.form.get("industry") or "").strip()
    business = (request.form.get("business") or "").strip()
    if industry not in INDUSTRIES:
        industry = ""
    con = db()
    con.execute("UPDATE users SET industry=?, business=? WHERE id=?", (industry, business, session["uid"]))
    con.commit()
    con.close()
    return redirect("/dashboard")


@app.route("/dashboard")
@login_required
def dashboard():
    u = current_user()
    con = db()
    reports = con.execute(
        "SELECT id, filename, created_at, metrics FROM reports WHERE user_id=? ORDER BY id DESC LIMIT 50",
        (u["id"],)).fetchall()
    con.close()
    # Tendencia histórica: total de ventas por reporte (cronológico)
    trend = [{"fecha": r["created_at"][:10], "archivo": r["filename"],
              "total": json.loads(r["metrics"]).get("total")}
             for r in reversed(reports) if json.loads(r["metrics"]).get("total") is not None]
    rows = "".join(
        f"<tr><td><span class='chip'>#{r['id']}</span></td><td>{esc(r['filename'])}</td>"
        f"<td>{r['created_at'][:10]}</td>"
        f"<td class='num'>${json.loads(r['metrics']).get('total', '—'):,}</td>"
        f"<td><a class='btn sm ghost' href='/reporte/{r['id']}'>Ver reporte</a></td></tr>"
        if json.loads(r['metrics']).get('total') is not None else
        f"<tr><td><span class='chip'>#{r['id']}</span></td><td>{esc(r['filename'])}</td>"
        f"<td>{r['created_at'][:10]}</td><td class='num'>—</td>"
        f"<td><a class='btn sm ghost' href='/reporte/{r['id']}'>Ver reporte</a></td></tr>"
        for r in reports)
    badge = {"free": "<span class='badge'>Plan Gratis</span>",
             "pending": "<span class='badge warn'>Pago en verificación</span>",
             "active": "<span class='badge ok'>Plan Pro ✓</span>"}[u["plan_status"]]
    ind_opts = "".join(
        f"<option {'selected' if i == u['industry'] else ''}>{i}</option>" for i in INDUSTRIES)
    trend_card = ""
    if len(trend) >= 2:
        trend_card = f"""
    <div class="card">
      <h3>Evolución de tus ventas</h3>
      <p class="muted">Total de ventas de cada reporte que has subido. La foto grande de tu negocio.</p>
      <div class="chartbox" style="border:0;box-shadow:none;padding:6px 0 0"><canvas id="c-trend" height="190"></canvas></div>
      <script>window.__TREND__ = {json.dumps(trend, ensure_ascii=False)};</script>
    </div>"""
    body = f"""
    {portal_nav(u, 'dash')}
    <div class="pagehead"><h1>Hola, {esc(u['business'] or u['email'].split('@')[0])}</h1>{badge}
      {f"<span class='chip'>{esc(u['industry'])}</span>" if u['industry'] else ''}</div>
    <div class="card">
      <h3>Nuevo reporte</h3>
      <p class="muted">Sube tu archivo de ventas (.csv o .xlsx). El reporte se genera al instante y queda en tu historial.</p>
      <form action="/reporte" method="post" enctype="multipart/form-data" class="row">
        <input type="file" name="file" accept=".csv,.xlsx,.xls" required>
        <button class="btn">Generar reporte</button>
      </form>
    </div>
    {trend_card}
    <div class="card">
      <h3>Historial</h3>
      {'<table><tr><th></th><th>Archivo</th><th>Fecha</th><th class="num">Ventas</th><th></th></tr>' + rows + '</table>' if rows else '<p class="muted">Aún no tienes reportes. Sube tu primer archivo arriba.</p>'}
    </div>
    <div class="card">
      <h3>Tu negocio</h3>
      <p class="muted">Con tu sector, el resumen ejecutivo incluye contexto específico de tu industria en Ecuador.</p>
      <form action="/perfil" method="post" class="row">
        <input type="text" name="business" placeholder="Nombre de tu negocio" value="{esc(u['business'])}">
        <select name="industry"><option value="">Sector...</option>{ind_opts}</select>
        <button class="btn ghost">Guardar</button>
      </form>
    </div>
    <script>
    if (window.__TREND__ && typeof Chart !== 'undefined') {{
      Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
      new Chart(document.getElementById('c-trend'), {{ type:'line',
        data:{{ labels:window.__TREND__.map(x=>x.fecha),
          datasets:[{{ data:window.__TREND__.map(x=>x.total), borderColor:'#2563eb',
            backgroundColor:'rgba(37,99,235,.08)', fill:true, tension:.3, pointRadius:4,
            pointBackgroundColor:'#2563eb', borderWidth:2.5 }}]}},
        options:{{ plugins:{{legend:{{display:false}}, tooltip:{{callbacks:{{
            afterLabel:(c)=>window.__TREND__[c.dataIndex].archivo }}}}}},
          scales:{{ y:{{ ticks:{{ callback:v=>money(v) }}, grid:{{color:'#eef2f7'}} }},
            x:{{ grid:{{display:false}} }} }} }} }});
    }}
    </script>"""
    return page(body, "Mi panel — ReporteFácil")


@app.route("/reporte", methods=["POST"])
@login_required
def create_report():
    u = current_user()
    f = request.files.get("file")
    if not f or not f.filename:
        return page(portal_nav(u, 'dash') + err_box("Sube un archivo.") + back_link("/dashboard"))
    try:
        df = parse_upload(f)
        if df.empty:
            raise ValueError("El archivo está vacío.")
        m = analyze(df)
    except Exception as e:  # noqa: BLE001
        return page(portal_nav(u, 'dash') + err_box(f"No pude procesar el archivo: {esc(str(e))}") + back_link("/dashboard"))
    s = ai_summary(m, u["industry"])
    con = db()
    cur = con.execute("INSERT INTO reports (user_id, filename, metrics, summary, created_at) VALUES (?,?,?,?,?)",
                      (u["id"], f.filename, json.dumps(m, ensure_ascii=False), s, now()))
    con.commit()
    rid = cur.lastrowid
    con.close()
    return redirect(f"/reporte/{rid}")


@app.route("/reporte/<int:rid>")
@login_required
def view_report(rid):
    u = current_user()
    con = db()
    r = con.execute("SELECT * FROM reports WHERE id=? AND user_id=?", (rid, u["id"])).fetchone()
    con.close()
    if not r:
        return page(portal_nav(u, 'dash') + err_box("Reporte no encontrado.") + back_link("/dashboard"))
    m = json.loads(r["metrics"])
    body = f"""
    {portal_nav(u, 'dash')}
    {back_link('/dashboard', 'Volver al panel')}
    <div class="pagehead"><h1>{esc(r['filename'])}</h1>
      <span class="muted">{r['created_at'][:16].replace('T', ' ')} UTC</span></div>
    <div id="report-root" class="reportview"></div>
    <script>window.__REPORT__ = {{ metrics: {json.dumps(m, ensure_ascii=False)},
      resumen: {json.dumps(r['summary'], ensure_ascii=False)} }};</script>
    <script>renderReport(document.getElementById('report-root'), window.__REPORT__);</script>"""
    return page(body, f"Reporte — {esc(r['filename'])}")


@app.route("/plan")
@login_required
def plan():
    u = current_user()
    status = u["plan_status"]
    con = db()
    in_waitlist = con.execute("SELECT 1 FROM leads WHERE email=? AND source='pro-waitlist'",
                              (u["email"],)).fetchone() is not None
    con.close()
    wait_cta = ("<span class='badge ok'>Estás en la lista ✓ — te avisamos al lanzar</span>" if in_waitlist else
                "<form action='/plan/lista-pro' method='post'><button class='btn'>Unirme a la lista de Pro</button></form>")
    blocks = {
        "free": f"""
        <div class="plans">
          <div class="plancard">
            <h3>Gratis</h3><div class="price">$0</div>
            <ul><li>Reportes manuales ilimitados</li><li>KPIs y gráficas</li><li>Historial de reportes</li></ul>
            <span class="badge ok">Tu plan actual</span>
          </div>
          <div class="plancard pro">
            <div class="ribbon">Muy pronto</div>
            <h3>Pro</h3><div class="price">{esc(PLAN_PRICE)}</div>
            <ul><li>Reporte automático cada lunes en tu correo</li><li>Resumen ejecutivo con IA</li><li>Soporte prioritario</li></ul>
            <div class="row">{wait_cta}</div>
            <p class="muted sm">Pro está en fase final de desarrollo. Los de la lista entran primero y con precio de lanzamiento.</p>
            <div class="paynote">🔐 <span>Cuando Pro lance, el pago se procesará en la plataforma del proveedor (PayPal o pasarela local).
            <b>Nunca ingresarás datos de tarjeta en ReporteFácil.</b> Ver <a href="/terminos">términos</a>.</span></div>
          </div>
        </div>""",
        "pending": "<div class='card'><h3>Pago en verificación</h3><p>Activamos tu plan Pro en menos de 24 horas. Si tarda más, abre un ticket de soporte y lo resolvemos.</p></div>",
        "active": "<div class='card'><h3>Plan Pro activo ✓</h3><p>Gracias por confiar en ReporteFácil. Tu reporte automático llega cada lunes.</p></div>",
    }
    return page(portal_nav(u, 'plan') + "<div class='pagehead'><h1>Mi plan</h1></div>" + blocks[status],
                "Mi plan — ReporteFácil")


@app.route("/plan/lista-pro", methods=["POST"])
@login_required
def plan_waitlist():
    u = current_user()
    save_lead(u["email"], "pro-waitlist")
    return redirect("/plan")


# --------------------------------- soporte ----------------------------------
@app.route("/soporte", methods=["GET", "POST"])
@login_required
def support():
    u = current_user()
    con = db()
    if request.method == "POST":
        subject = (request.form.get("subject") or "").strip()
        body = (request.form.get("body") or "").strip()
        if subject and body:
            cur = con.execute("INSERT INTO tickets (user_id, subject, created_at) VALUES (?,?,?)",
                              (u["id"], subject, now()))
            con.execute("INSERT INTO ticket_messages (ticket_id, sender, body, created_at) VALUES (?,?,?,?)",
                        (cur.lastrowid, "cliente", body, now()))
            con.commit()
        con.close()
        return redirect("/soporte")
    tickets = con.execute("SELECT * FROM tickets WHERE user_id=? ORDER BY id DESC", (u["id"],)).fetchall()
    con.close()
    rows = "".join(
        f"<tr><td><span class='chip'>#{t['id']}</span></td>"
        f"<td><a href='/soporte/{t['id']}'>{esc(t['subject'])}</a></td>"
        f"<td><span class='badge {'ok' if t['status'] == 'cerrado' else 'warn'}'>{t['status']}</span></td>"
        f"<td>{t['created_at'][:10]}</td></tr>" for t in tickets)
    body = f"""
    {portal_nav(u, 'sup')}
    <div class="pagehead"><h1>Soporte</h1><span class="muted">Respondemos en menos de 24 h</span></div>
    <div class="card">
      <h3>Nuevo ticket</h3>
      <form method="post">
        <input type="text" name="subject" placeholder="Asunto" required class="full">
        <textarea name="body" placeholder="Cuéntanos tu problema o pregunta con detalle..." required></textarea>
        <div class="row"><button class="btn">Enviar ticket</button></div>
      </form>
    </div>
    <div class="card">
      <h3>Mis tickets</h3>
      {'<table><tr><th></th><th>Asunto</th><th>Estado</th><th>Fecha</th></tr>' + rows + '</table>' if rows else '<p class="muted">Sin tickets todavía.</p>'}
    </div>"""
    return page(body, "Soporte — ReporteFácil")


@app.route("/soporte/<int:tid>", methods=["GET", "POST"])
@login_required
def support_thread(tid):
    u = current_user()
    con = db()
    t = con.execute("SELECT * FROM tickets WHERE id=? AND user_id=?", (tid, u["id"])).fetchone()
    if not t:
        con.close()
        return page(portal_nav(u, 'sup') + err_box("Ticket no encontrado.") + back_link("/soporte"))
    if request.method == "POST":
        body = (request.form.get("body") or "").strip()
        if body:
            con.execute("INSERT INTO ticket_messages (ticket_id, sender, body, created_at) VALUES (?,?,?,?)",
                        (tid, "cliente", body, now()))
            con.execute("UPDATE tickets SET status='abierto' WHERE id=?", (tid,))
            con.commit()
        con.close()
        return redirect(f"/soporte/{tid}")
    msgs = con.execute("SELECT * FROM ticket_messages WHERE ticket_id=? ORDER BY id", (tid,)).fetchall()
    con.close()
    thread = "".join(
        f"<div class='msg {m['sender']}'><div class='msghead'>{'Tú' if m['sender'] == 'cliente' else 'Soporte ReporteFácil'}"
        f"<span> · {m['created_at'][:16].replace('T', ' ')}</span></div>{esc(m['body'])}</div>" for m in msgs)
    body = f"""
    {portal_nav(u, 'sup')}
    {back_link('/soporte', 'Volver a soporte')}
    <div class="pagehead"><h1>{esc(t['subject'])}</h1>
      <span class="badge {'ok' if t['status'] == 'cerrado' else 'warn'}">{t['status']}</span></div>
    <div class="card">{thread}
      <form method="post" class="replyform">
        <textarea name="body" placeholder="Escribe tu respuesta..." required></textarea>
        <div class="row"><button class="btn">Responder</button></div>
      </form>
    </div>"""
    return page(body, f"Ticket #{t['id']} — ReporteFácil")


# ---------------------------------- admin -----------------------------------
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["admin"] = True
            return redirect("/admin")
        return page(err_box("Contraseña incorrecta.") + ADMIN_LOGIN_FORM, "Admin")
    return page(ADMIN_LOGIN_FORM, "Admin — ReporteFácil")


@app.route("/admin")
@admin_required
def admin():
    con = db()
    nleads = con.execute("SELECT COUNT(*) n FROM leads").fetchone()["n"]
    users = con.execute("SELECT * FROM users ORDER BY id DESC").fetchall()
    pending = [u for u in users if u["plan_status"] == "pending"]
    tickets = con.execute(
        """SELECT t.*, u.email FROM tickets t JOIN users u ON u.id=t.user_id
           WHERE t.status='abierto' ORDER BY t.id DESC""").fetchall()
    leads_rows = con.execute("SELECT * FROM leads ORDER BY id DESC LIMIT 100").fetchall()
    con.close()
    urows_list = []
    for u in users:
        badge_cls = "ok" if u["plan_status"] == "active" else ("warn" if u["plan_status"] == "pending" else "")
        if u["plan_status"] != "active":
            action = ("<form method='post' action='/admin/activar/" + str(u["id"])
                      + "'><button class='btn sm'>Activar Pro</button></form>")
        else:
            action = "✓"
        urows_list.append(
            f"<tr><td><span class='chip'>{u['id']}</span></td><td>{esc(u['email'])}</td>"
            f"<td>{esc(u['business'])}</td>"
            f"<td><span class='badge {badge_cls}'>{u['plan_status']}</span></td>"
            f"<td>{action}</td></tr>")
    urows = "".join(urows_list)
    trows = "".join(
        f"<tr><td><span class='chip'>#{t['id']}</span></td><td>{esc(t['email'])}</td>"
        f"<td><a href='/admin/ticket/{t['id']}'>{esc(t['subject'])}</a></td>"
        f"<td>{t['created_at'][:10]}</td></tr>" for t in tickets)
    nwait = sum(1 for x in leads_rows if x["source"] == "pro-waitlist")
    lrows = "".join(
        f"<tr><td>{esc(x['email'])}</td>"
        f"<td><span class='badge {'ok' if x['source'] == 'pro-waitlist' else ''}'>{'Lista Pro' if x['source'] == 'pro-waitlist' else 'Landing'}</span></td>"
        f"<td>{x['created_at'][:10]}</td></tr>" for x in leads_rows)
    body = f"""
    <div class="topbar"><a class="brand" href="/">Reporte<span>Fácil</span> <em>admin</em></a>
      <nav class="tabs"><a href="/logout">Salir</a></nav></div>
    <div class="pagehead"><h1>Administración</h1></div>
    <div class="kpis">{kpi(nleads, 'leads captados')}{kpi(len(users), 'usuarios')}{kpi(len(pending), 'pagos por verificar', 'warnk' if pending else '')}{kpi(len(tickets), 'tickets abiertos', 'warnk' if tickets else '')}</div>
    <div class="card"><h3>Usuarios</h3>
      <table><tr><th></th><th>Correo</th><th>Negocio</th><th>Plan</th><th></th></tr>{urows or ''}</table>
      {'' if urows else '<p class="muted">Sin usuarios aún.</p>'}</div>
    <div class="card"><h3>Tickets abiertos</h3>
      {'<table><tr><th></th><th>Cliente</th><th>Asunto</th><th>Fecha</th></tr>' + trows + '</table>' if trows else '<p class="muted">Ninguno.</p>'}</div>
    <div class="card"><h3>Leads <span class="chip">{nwait} en lista Pro</span></h3>
      {'<table><tr><th>Correo</th><th>Origen</th><th>Fecha</th></tr>' + lrows + '</table>' if lrows else '<p class="muted">Ninguno todavía.</p>'}</div>"""
    return page(body, "Admin — ReporteFácil")


@app.route("/admin/activar/<int:uid>", methods=["POST"])
@admin_required
def admin_activate(uid):
    con = db()
    con.execute("UPDATE users SET plan_status='active' WHERE id=?", (uid,))
    con.commit()
    con.close()
    return redirect("/admin")


@app.route("/admin/ticket/<int:tid>", methods=["GET", "POST"])
@admin_required
def admin_ticket(tid):
    con = db()
    t = con.execute("SELECT t.*, u.email FROM tickets t JOIN users u ON u.id=t.user_id WHERE t.id=?",
                    (tid,)).fetchone()
    if not t:
        con.close()
        return page(err_box("Ticket no encontrado.") + back_link("/admin"))
    if request.method == "POST":
        body = (request.form.get("body") or "").strip()
        action = request.form.get("action")
        if body:
            con.execute("INSERT INTO ticket_messages (ticket_id, sender, body, created_at) VALUES (?,?,?,?)",
                        (tid, "soporte", body, now()))
        if action == "cerrar":
            con.execute("UPDATE tickets SET status='cerrado' WHERE id=?", (tid,))
        con.commit()
        con.close()
        return redirect(f"/admin/ticket/{tid}")
    msgs = con.execute("SELECT * FROM ticket_messages WHERE ticket_id=? ORDER BY id", (tid,)).fetchall()
    con.close()
    thread = "".join(
        f"<div class='msg {m['sender']}'><div class='msghead'>{'Cliente' if m['sender'] == 'cliente' else 'Tú (soporte)'}"
        f"<span> · {m['created_at'][:16].replace('T', ' ')}</span></div>{esc(m['body'])}</div>" for m in msgs)
    body = f"""
    <div class="topbar"><a class="brand" href="/admin">Reporte<span>Fácil</span> <em>admin</em></a>
      <nav class="tabs"><a href="/logout">Salir</a></nav></div>
    {back_link('/admin', 'Volver a admin')}
    <div class="pagehead"><h1>{esc(t['subject'])}</h1>
      <span class="muted">Cliente: {esc(t['email'])}</span>
      <span class="badge {'ok' if t['status'] == 'cerrado' else 'warn'}">{t['status']}</span></div>
    <div class="card">{thread}
      <form method="post" class="replyform">
        <textarea name="body" placeholder="Respuesta al cliente..."></textarea>
        <div class="row"><button class="btn" name="action" value="responder">Responder</button>
        <button class="btn ghost" name="action" value="cerrar">Responder y cerrar</button></div>
      </form>
    </div>"""
    return page(body, f"Admin · Ticket #{t['id']}")


# ----------------------------- html / plantillas ----------------------------
LAYOUT = """<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<title>__TITLE__</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="Sube tu Excel de ventas y recibe un reporte ejecutivo claro con gráficas y resumen con IA, en español.">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='7' fill='%230e9f6e'/%3E%3Cpath d='M8 22V14M14 22V10M20 22V16M26 22V12' stroke='white' stroke-width='3' stroke-linecap='round'/%3E%3C/svg%3E">
<link rel="preconnect" href="https://cdn.jsdelivr.net">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:opsz,wght@14..32,400;14..32,500;14..32,600;14..32,700;14..32,800&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
<style>
  /* ===== Sistema "fintech dashboard": superficie neutra, tarjetas blancas, tipografia pesada ===== */
  :root {
    --ink:#0a0d14; --ink2:#5b6577; --ink3:#8b95a7;
    --bg:#f1f3f7; --card:#ffffff; --line:#e6e9ef; --line2:#eef1f6;
    --acc:#0a0d14; --accd:#000000;
    --blue:#2563eb; --green:#0f9d58; --mint:#e8f7ef; --sun:#ffd166; --err:#d92d20;
    --font:'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    --r:24px; --r-sm:16px; --r-pill:999px;
    --sh:0 1px 2px rgba(10,13,20,.04), 0 8px 24px -6px rgba(10,13,20,.07);
    --sh-lg:0 2px 4px rgba(10,13,20,.04), 0 24px 48px -12px rgba(10,13,20,.14);
    --s8:8px; --s16:16px; --s24:24px; --s32:32px; --s48:48px; --s64:64px;
  }
  * { box-sizing:border-box; margin:0; }
  html { scroll-behavior:smooth; -webkit-text-size-adjust:100%; }
  body { background:var(--bg); color:var(--ink); -webkit-font-smoothing:antialiased;
    -moz-osx-font-smoothing:grayscale; font:400 16px/1.6 var(--font);
    font-feature-settings:"cv02","cv03","cv04","ss01"; }
  .wrap { max-width:1180px; margin:0 auto; padding:var(--s16) var(--s24) var(--s64); }
  h1,h2,h3,h4 { letter-spacing:-.035em; font-weight:800; line-height:1.08; }
  h1 { font-size:34px; } h2 { font-size:30px; } h3 { font-size:20px; letter-spacing:-.025em; margin-bottom:var(--s8); }
  a { color:var(--ink); text-decoration:none; }
  a:hover { opacity:.75; }
  p, .muted { color:var(--ink2); } .muted { font-size:15px; } .sm { font-size:13px; }
  ::selection { background:var(--ink); color:#fff; }
  :focus-visible { outline:2px solid var(--ink); outline-offset:3px; border-radius:12px; }
  /* --- nav --- */
  .topbar { display:flex; justify-content:space-between; align-items:center; gap:var(--s16);
    padding:var(--s16) var(--s24); margin:var(--s8) 0 var(--s32); flex-wrap:wrap;
    background:var(--card); border-radius:var(--r-pill); box-shadow:var(--sh); }
  .brand { font-weight:800; font-size:19px; letter-spacing:-.04em; }
  .brand span { color:var(--blue); }
  .brand em { font-style:normal; color:var(--ink3); font-weight:500; font-size:13px; }
  .tabs { display:flex; gap:4px; align-items:center; flex-wrap:wrap; }
  .tabs a { padding:9px 16px; border-radius:var(--r-pill); color:var(--ink2); font-size:15px; font-weight:500; }
  .tabs a.on, .tabs a:hover { background:var(--bg); color:var(--ink); opacity:1; }
  .navlink { padding:9px 16px; color:var(--ink); font-weight:600; font-size:15px; }
  /* --- botones pill --- */
  .btn { display:inline-flex; align-items:center; justify-content:center; gap:8px;
    background:var(--ink); color:#fff; font-weight:600; border:0; border-radius:var(--r-pill);
    padding:13px 26px; cursor:pointer; font:600 15px/1.2 var(--font); letter-spacing:-.01em;
    transition:transform .16s cubic-bezier(.2,.7,.2,1), box-shadow .2s, background .2s;
    box-shadow:0 1px 2px rgba(10,13,20,.16); }
  .btn:hover { background:#1c2333; transform:translateY(-2px); opacity:1;
    box-shadow:0 8px 24px -6px rgba(10,13,20,.35); }
  .btn:active { transform:translateY(0); }
  .btn.ghost { background:var(--card); color:var(--ink); box-shadow:inset 0 0 0 1px var(--line), var(--sh); }
  .btn.ghost:hover { background:#fff; box-shadow:inset 0 0 0 1px var(--ink3), var(--sh-lg); }
  .btn.sm { padding:8px 16px; font-size:13px; }
  .btn.big { padding:16px 34px; font-size:17px; }
  /* --- tarjetas --- */
  .card { background:var(--card); border:0; border-radius:var(--r); padding:var(--s32);
    margin:var(--s24) 0; box-shadow:var(--sh); }
  .errbox { background:#fef3f2; color:var(--err); box-shadow:inset 0 0 0 1px #fecdca; }
  .row { display:flex; gap:var(--s16); flex-wrap:wrap; align-items:center; margin-top:var(--s16); }
  input, textarea, select { background:var(--bg); color:var(--ink); border:1.5px solid transparent;
    border-radius:14px; padding:13px 18px; font:400 15px/1.5 var(--font); outline:none;
    transition:border-color .16s, background .16s; }
  input:focus, textarea:focus, select:focus { border-color:var(--ink); background:var(--card); }
  input::placeholder, textarea::placeholder { color:var(--ink3); }
  textarea { width:100%; min-height:110px; resize:vertical; }
  .full { width:100%; margin-bottom:var(--s8); }
  .pagehead { display:flex; gap:var(--s16); align-items:center; flex-wrap:wrap; margin:var(--s16) 0; }
  /* --- píldoras --- */
  .badge { border-radius:var(--r-pill); padding:5px 14px; font-size:13px; font-weight:600;
    background:var(--bg); color:var(--ink2); }
  .badge.ok { background:var(--mint); color:#065f36; }
  .badge.warn { background:#fff5db; color:#8a5a00; }
  .chip { background:var(--bg); border-radius:8px; padding:3px 10px; font-size:13px;
    color:var(--ink2); font-weight:600; }
  .back { display:inline-block; margin:var(--s8) 0; color:var(--ink2); font-size:14px; font-weight:500; }
  /* --- tablas --- */
  table { width:100%; border-collapse:collapse; font-size:15px; }
  td, th { padding:14px 12px; border-bottom:1px solid var(--line2); text-align:left; }
  th { color:var(--ink3); font-size:12px; font-weight:600; text-transform:uppercase; letter-spacing:.08em; }
  tr:last-child td { border-bottom:0; }
  .num { text-align:right; font-variant-numeric:tabular-nums; font-weight:600; }
  /* --- KPIs estilo dashboard --- */
  .kpis { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:var(--s16); margin:var(--s24) 0; }
  .kpi { background:var(--card); border-radius:20px; padding:22px 24px; box-shadow:var(--sh); position:relative;
    transition:transform .2s cubic-bezier(.2,.7,.2,1), box-shadow .2s; }
  .kpi:hover { transform:translateY(-3px); box-shadow:var(--sh-lg); }
  .kpi b { display:block; font-size:30px; font-weight:800; letter-spacing:-.04em; line-height:1.1;
    font-variant-numeric:tabular-nums; }
  .kpi span { font-size:13px; color:var(--ink3); font-weight:500; display:block; margin-top:6px; }
  .kpi.up { background:linear-gradient(160deg,#0f9d58,#0a7d45); }
  .kpi.up b, .kpi.up span { color:#fff; } .kpi.up span { opacity:.85; }
  .kpi.down b { color:var(--err); } .kpi.warnk b { color:#b45309; }
  /* --- alertas y resumen --- */
  .alerts { margin:var(--s24) 0; }
  .alert { display:flex; gap:14px; align-items:flex-start; background:var(--card);
    border-radius:var(--r-sm); padding:16px 20px; margin:12px 0; font-size:15px;
    box-shadow:var(--sh); border-left:4px solid var(--sun); }
  .sum { background:var(--card); border-radius:var(--r); padding:var(--s24) var(--s32);
    margin:var(--s24) 0; white-space:pre-wrap; font-size:16px; box-shadow:var(--sh);
    border-left:4px solid var(--blue); }
  .sum::before { content:"Resumen ejecutivo"; display:block; font-size:12px; font-weight:700;
    letter-spacing:.09em; text-transform:uppercase; color:var(--blue); margin-bottom:10px; }
  /* --- gráficas --- */
  .charts { display:grid; grid-template-columns:1fr 1fr; gap:var(--s16); margin:var(--s24) 0; }
  .chartbox { background:var(--card); border-radius:20px; padding:var(--s24); box-shadow:var(--sh); }
  .chartbox h4 { font-size:12px; color:var(--ink3); text-transform:uppercase; letter-spacing:.08em;
    margin-bottom:var(--s16); font-weight:600; }
  @media (max-width:760px) { .charts { grid-template-columns:1fr; } }
  /* --- soporte --- */
  .msg { background:var(--bg); border-radius:var(--r-sm); padding:14px 20px; margin:12px 0; font-size:15px; }
  .msg.soporte { background:var(--mint); }
  .msghead { font-weight:700; font-size:13px; margin-bottom:5px; }
  .msghead span { color:var(--ink3); font-weight:500; }
  .replyform { margin-top:var(--s24); }
  /* --- landing --- */
  .hero { text-align:center; padding:var(--s64) 0 var(--s32); }
  .hero h1 { font-size:clamp(42px, 7.5vw, 84px); letter-spacing:-.05em; line-height:.98; font-weight:800; }
  .hero h1 em { font-style:normal; color:var(--blue); }
  .hero p.lead { color:var(--ink2); font-size:clamp(17px,2vw,20px); max-width:620px;
    margin:var(--s24) auto 0; line-height:1.5; }
  .cta { display:flex; gap:12px; justify-content:center; margin:var(--s32) 0 12px; flex-wrap:wrap; }
  .cta input { min-width:290px; background:var(--card); box-shadow:var(--sh); }
  .trust { color:var(--ink3); font-size:14px; font-weight:500; }
  /* marco de producto */
  .browserframe { max-width:1040px; margin:var(--s64) auto 0; background:var(--card);
    border-radius:28px; box-shadow:var(--sh-lg); overflow:hidden; text-align:left; }
  .browserbar { display:flex; align-items:center; gap:8px; padding:16px 20px; border-bottom:1px solid var(--line2); }
  .browserbar i { width:11px; height:11px; border-radius:50%; display:inline-block; background:var(--line); }
  .browserbar .url { flex:1; text-align:center; background:var(--bg); border-radius:var(--r-pill);
    padding:6px 16px; font-size:13px; color:var(--ink3); max-width:400px; margin:0 auto; font-weight:500; }
  .mockbody { padding:var(--s32); }
  .mockbody .kpis { margin:0 0 var(--s16); }
  .mockbody .kpi b { font-size:26px; }
  /* barra de stats */
  .statgrid { display:grid; grid-template-columns:repeat(auto-fit,minmax(230px,1fr)); gap:var(--s24);
    margin:var(--s64) 0; background:var(--ink); border-radius:var(--r); padding:var(--s48) var(--s32); }
  .stat { text-align:left; padding:0 var(--s16); }
  .stat + .stat { border-left:1px solid rgba(255,255,255,.13); }
  @media (max-width:700px) { .stat + .stat { border-left:0; border-top:1px solid rgba(255,255,255,.13);
    padding-top:var(--s24); } }
  .stat b { display:block; font-size:clamp(40px,5.5vw,62px); font-weight:800; letter-spacing:-.05em;
    line-height:1; color:#fff; }
  .stat b em { font-style:normal; font-size:.55em; color:rgba(255,255,255,.65); margin-left:2px; }
  .stat span { color:rgba(255,255,255,.82); font-size:15px; display:block; margin-top:12px; font-weight:500; }
  .stat .src { display:block; font-size:12px; color:rgba(255,255,255,.45); margin-top:6px; font-weight:400; }
  /* secciones */
  .bigsection { text-align:center; padding:var(--s64) 0 var(--s16); }
  .bigsection h2 { font-size:clamp(30px,5vw,54px); letter-spacing:-.045em; line-height:1.02; font-weight:800; }
  .bigsection p { color:var(--ink2); max-width:600px; margin:var(--s16) auto 0; font-size:17px; }
  .sectionhead { text-align:center; margin:var(--s64) 0 var(--s16); }
  .sectionhead .kicker { color:var(--blue); font-weight:700; font-size:12px;
    letter-spacing:.1em; text-transform:uppercase; }
  /* tarjetas de contenido */
  .trustgrid, .steps { display:grid; grid-template-columns:repeat(auto-fit,minmax(290px,1fr));
    gap:var(--s24); margin:var(--s32) 0; }
  .trustcard, .step { background:var(--card); border-radius:var(--r); padding:var(--s32); box-shadow:var(--sh);
    transition:transform .22s cubic-bezier(.2,.7,.2,1), box-shadow .22s; }
  .trustcard:hover, .step:hover { transform:translateY(-4px); box-shadow:var(--sh-lg); }
  .trustcard .ic { display:inline-flex; width:52px; height:52px; align-items:center; justify-content:center;
    background:var(--bg); border-radius:16px; margin-bottom:var(--s16); font-size:22px; }
  .trustcard b, .step b { display:block; font-size:19px; font-weight:700; margin-bottom:8px; letter-spacing:-.025em; }
  .trustcard span, .step span { color:var(--ink2); font-size:15px; }
  .step .n { display:inline-flex; align-items:center; justify-content:center; width:40px; height:40px;
    background:var(--ink); color:#fff; border-radius:14px; margin-bottom:var(--s16);
    font-size:16px; font-weight:700; }
  .step .n::before { content:attr(data-n); }
  .drop { background:var(--card); border:2px dashed var(--line); border-radius:var(--r);
    padding:var(--s48) var(--s24); text-align:center; color:var(--ink2); cursor:pointer;
    margin:var(--s24) 0; font-size:16px; font-weight:500; transition:all .18s; }
  .drop:hover, .drop.on { border-color:var(--blue); color:var(--blue); background:#f5f8ff; }
  /* precios */
  .plans { display:grid; grid-template-columns:repeat(auto-fit,minmax(310px,1fr)); gap:var(--s24); margin:var(--s32) 0; }
  .plancard { background:var(--card); border-radius:var(--r); padding:var(--s32); position:relative; box-shadow:var(--sh);
    transition:transform .22s cubic-bezier(.2,.7,.2,1), box-shadow .22s; }
  .plancard:hover { transform:translateY(-4px); box-shadow:var(--sh-lg); }
  .plancard.pro { background:var(--ink); color:#fff; }
  .plancard.pro h3, .plancard.pro .price { color:#fff; }
  .plancard.pro li { color:rgba(255,255,255,.8); }
  .plancard.pro li::before { background:var(--green); }
  .plancard.pro .btn { background:#fff; color:var(--ink); }
  .plancard.pro .btn:hover { background:#f1f3f7; }
  .plancard .ribbon { position:absolute; top:22px; right:22px; background:var(--sun); color:#3d2c00;
    font-size:11px; font-weight:800; letter-spacing:.08em; text-transform:uppercase;
    padding:5px 12px; border-radius:var(--r-pill); }
  .plancard .price { font-size:52px; font-weight:800; letter-spacing:-.05em; line-height:1; margin:12px 0 var(--s24); }
  .plancard ul { list-style:none; margin:0 0 var(--s24); padding:0; }
  .plancard li { padding:9px 0 9px 30px; position:relative; color:var(--ink2); font-size:15px; }
  .plancard li::before { content:""; position:absolute; left:0; top:16px; width:18px; height:18px;
    border-radius:50%; background:var(--mint); }
  .plancard li::after { content:"✓"; position:absolute; left:5px; top:9px; font-size:11px;
    font-weight:800; color:#0f9d58; }
  /* FAQ */
  details { background:var(--card); border-radius:var(--r-sm); padding:20px 24px; margin:12px 0; box-shadow:var(--sh); }
  details summary { cursor:pointer; font-size:16px; font-weight:600; list-style:none; letter-spacing:-.015em; }
  details summary::-webkit-details-marker { display:none; }
  details summary::after { content:"+"; float:right; color:var(--ink3); font-weight:400; font-size:20px; line-height:1; }
  details[open] summary::after { content:"−"; }
  details p { margin-top:14px; color:var(--ink2); font-size:15px; max-width:720px; }
  .foot { text-align:center; color:var(--ink3); font-size:14px; margin-top:var(--s64);
    padding-top:var(--s32); border-top:1px solid var(--line); }
  .foot a { color:var(--ink2); margin:0 8px; font-weight:500; }
  .authcard { max-width:460px; margin:var(--s48) auto; }
  .legal { max-width:760px; margin:var(--s24) auto; }
  .legal p { margin:14px 0; color:var(--ink2); font-size:16px; }
  .legal b { color:var(--ink); font-weight:600; }
  .paynote { display:flex; gap:12px; align-items:flex-start; background:var(--bg);
    border-radius:var(--r-sm); padding:14px 18px; margin-top:var(--s16); font-size:14px; color:var(--ink2); }
  .reportview { margin-top:var(--s16); }
  /* --- movimiento --- */
  @media (prefers-reduced-motion: no-preference) {
    .hero h1, .hero .lead, .hero .cta, .hero .trust { opacity:0; animation:riseIn .75s cubic-bezier(.2,.7,.2,1) forwards; }
    .hero .lead { animation-delay:.1s; } .hero .cta { animation-delay:.2s; } .hero .trust { animation-delay:.3s; }
    .browserframe { opacity:0; transform:translateY(30px) scale(.98);
      animation:frameIn .95s .35s cubic-bezier(.2,.7,.2,1) forwards; }
    .reveal { opacity:0; transform:translateY(24px);
      transition:opacity .65s cubic-bezier(.2,.7,.2,1), transform .65s cubic-bezier(.2,.7,.2,1); }
    .reveal.in { opacity:1; transform:none; }
    .reveal.d1 { transition-delay:.07s; } .reveal.d2 { transition-delay:.14s; } .reveal.d3 { transition-delay:.21s; }
  }
  @keyframes riseIn { from { opacity:0; transform:translateY(20px); } to { opacity:1; transform:none; } }
  @keyframes frameIn { from { opacity:0; transform:translateY(30px) scale(.98); } to { opacity:1; transform:none; } }
</style>
</head>
<body><div class="wrap">__CONTENT__</div>
<script>
function money(n) { return '$' + Number(n).toLocaleString('es-EC', {maximumFractionDigits:2}); }
function escH(s){const d=document.createElement('div');d.textContent=s??'';return d.innerHTML;}
function kpiH(v,l,cls){ return '<div class="kpi '+(cls||'')+'"><b>'+v+'</b><span>'+l+'</span></div>'; }
function renderReport(root, data) {
  // IDs unicos por invocacion: el mockup del hero y el demo pueden coexistir
  const uid = 'rc' + (renderReport._n = (renderReport._n || 0) + 1) + '-';
  const m = data.metrics; let h = '';
  h += '<div class="kpis">';
  if (m.total !== undefined) {
    h += kpiH(money(m.total),'ventas totales') + kpiH(m.transacciones,'transacciones')
       + kpiH(money(m.promedio),'ticket promedio');
    if (m.variacion_pct !== undefined)
      h += kpiH((m.variacion_pct>0?'+':'')+m.variacion_pct+'%','vs semana anterior', m.variacion_pct>=0?'up':'down');
    if (m.crecimiento_mensual_pct !== undefined)
      h += kpiH((m.crecimiento_mensual_pct>0?'+':'')+m.crecimiento_mensual_pct+'%','crecimiento mensual', m.crecimiento_mensual_pct>=0?'up':'down');
    if (m.mejor_dia) h += kpiH(m.mejor_dia,'tu mejor día');
    if (m.concentracion_pct !== undefined)
      h += kpiH(m.concentracion_pct+'%','ventas del producto #1', m.concentracion_pct>=40?'warnk':'');
  } else h += kpiH(m.filas,'filas leídas');
  h += '</div>';
  if (m.alertas && m.alertas.length) {
    h += '<div class="alerts">' + m.alertas.map(a =>
      '<div class="alert">⚠️ <span>'+escH(a)+'</span></div>').join('') + '</div>';
  }
  if (data.resumen) h += '<div class="sum">'+escH(data.resumen)+'</div>';
  const hasSerie = m.serie_semanal && m.serie_semanal.length > 1;
  const hasTop = m.top_categorias && m.top_categorias.length;
  const hasDow = m.por_dia && m.por_dia.some(x=>x.total>0);
  const hasMes = m.serie_mensual && m.serie_mensual.length > 1;
  if (hasSerie || hasTop || hasDow || hasMes) {
    h += '<div class="charts">';
    if (hasSerie) h += '<div class="chartbox"><h4>Ventas por semana</h4><canvas id="'+uid+'serie" height="220"></canvas></div>';
    if (hasTop) h += '<div class="chartbox"><h4>Top ' + escH(String(m.col_categoria||'categorías')) + '</h4><canvas id="'+uid+'top" height="220"></canvas></div>';
    if (hasDow) h += '<div class="chartbox"><h4>¿Qué días vendes más?</h4><canvas id="'+uid+'dow" height="220"></canvas></div>';
    if (hasMes) h += '<div class="chartbox"><h4>Evolución mensual</h4><canvas id="'+uid+'mes" height="220"></canvas></div>';
    h += '</div>';
  }
  if (m.productos_caida && m.productos_caida.length) {
    h += '<h3 style="margin:18px 0 6px;font-size:1rem">Productos en caída <span class="muted sm">(2ª mitad del período vs 1ª)</span></h3>'
      + '<table><tr><th>' + escH(String(m.col_categoria||'Producto')) + '</th><th class="num">Cambio</th></tr>'
      + m.productos_caida.map(p => '<tr><td>'+escH(p.nombre)+'</td><td class="num" style="color:#b91c1c">'+p.cambio_pct+'%</td></tr>').join('')
      + '</table>';
  }
  if (m.por_vendedor && m.por_vendedor.length) {
    h += '<h3 style="margin:18px 0 6px;font-size:1rem">Ventas por ' + escH(String(m.col_vendedor)) + '</h3>'
      + '<table><tr><th>' + escH(String(m.col_vendedor)) + '</th><th class="num">Total</th></tr>'
      + m.por_vendedor.map(p => '<tr><td>'+escH(p.nombre)+'</td><td class="num">'+money(p.total)+'</td></tr>').join('')
      + '</table>';
  }
  if (!m.col_monto) h += '<p class="muted">No detecté una columna de montos. Nombra una columna "total", "monto" o "venta" para el análisis completo.</p>';
  root.innerHTML = h;
  if (typeof Chart === 'undefined') return;
  // Paleta del sistema: tinta, amarillo racionado, menta como señal. Sin decoración.
  const INK='#0a0d14', BLUE='#2563eb', GREEN='#0f9d58', GRID='#eef1f6', MUT='#8b95a7', SOFT='#dfe4ec';
  Chart.defaults.font.family = "Inter, system-ui, sans-serif";
  Chart.defaults.font.size = 12;
  Chart.defaults.color = MUT;
  const noLegend = { plugins:{legend:{display:false}}, maintainAspectRatio:false };
  const yMoney = { ticks:{ callback:v=>money(v) }, grid:{color:GRID}, border:{display:false} };
  const xPlain = { grid:{display:false}, border:{display:false} };
  if (hasSerie) {
    const cv = document.getElementById(uid+'serie');
    const g = cv.getContext('2d').createLinearGradient(0,0,0,220);
    g.addColorStop(0,'rgba(37,99,235,.22)'); g.addColorStop(1,'rgba(37,99,235,0)');
    new Chart(cv, { type:'line',
      data:{ labels:m.serie_semanal.map(x=>x.semana.split('/')[0]),
        datasets:[{ data:m.serie_semanal.map(x=>x.total), borderColor:BLUE, backgroundColor:g,
          fill:true, tension:.4, pointRadius:0, pointHoverRadius:5, pointHoverBackgroundColor:BLUE,
          borderWidth:3 }]},
      options:{ ...noLegend, scales:{ y:yMoney, x:xPlain } } });
  }
  if (hasTop) {
    new Chart(document.getElementById(uid+'top'), { type:'bar',
      data:{ labels:m.top_categorias.map(x=>x.nombre),
        datasets:[{ data:m.top_categorias.map(x=>x.total), backgroundColor:INK, borderRadius:8,
          barPercentage:.7 }]},
      options:{ indexAxis:'y', ...noLegend, scales:{ x:yMoney, y:{ grid:{display:false}, border:{display:false} } } } });
  }
  if (hasDow) {
    const best = Math.max(...m.por_dia.map(x=>x.total));
    new Chart(document.getElementById(uid+'dow'), { type:'bar',
      data:{ labels:m.por_dia.map(x=>x.dia.slice(0,3)),
        datasets:[{ data:m.por_dia.map(x=>x.total), borderRadius:8, barPercentage:.65,
          backgroundColor:m.por_dia.map(x=>x.total===best?GREEN:SOFT) }]},
      options:{ ...noLegend, scales:{ y:yMoney, x:xPlain } } });
  }
  if (hasMes) {
    new Chart(document.getElementById(uid+'mes'), { type:'bar',
      data:{ labels:m.serie_mensual.map(x=>x.mes),
        datasets:[{ data:m.serie_mensual.map(x=>x.total), backgroundColor:BLUE, borderRadius:8,
          barPercentage:.6 }]},
      options:{ ...noLegend, scales:{ y:yMoney, x:xPlain } } });
  }
}
</script>
</body>
</html>"""

LANDING_BODY = """
  <div class="topbar">
    <a class="brand" href="/">Reporte<span>Fácil</span></a>
    <nav class="tabs">__NAV__</nav>
  </div>

  <div class="hero">
    <h1>Tu Excel entra.<br><em>Las decisiones salen.</em></h1>
    <p class="lead">ReporteFácil convierte tu archivo de ventas en un reporte ejecutivo con gráficas
    y recomendaciones — en segundos, en español, sin configurar nada.</p>
    <div class="cta">
      <input type="email" id="em" placeholder="tu@correo.com">
      <button class="btn big" onclick="sub()">Quiero acceso anticipado</button>
    </div>
    <div class="trust" id="submsg">Gratis · Sin tarjeta · Sin spam</div>

    <div class="browserframe">
      <div class="browserbar">
        <i></i><i></i><i></i>
        <span class="url">reporte-facil.onrender.com · tu reporte semanal</span>
      </div>
      <div class="mockbody" id="mockreport"></div>
    </div>
  </div>

  <div class="statgrid">
    <div class="stat"><b>&lt;10<em>s</em></b><span>de Excel a reporte completo</span>
      <span class="src">medido con archivos de hasta 5 000 filas</span></div>
    <div class="stat"><b>0</b><span>configuración: detecta tus columnas solo</span>
      <span class="src">fechas, productos y montos, automático</span></div>
    <div class="stat"><b>$0</b><span>para empezar, reportes ilimitados</span>
      <span class="src">sin tarjeta, sin compromiso</span></div>
  </div>

  <div class="bigsection">
    <h2>Pruébalo con tus propios datos.</h2>
    <p>Lo que ves arriba es el producto real. Arrastra tu archivo y míralo con tus números.</p>
  </div>

  <div class="card" id="demo">
    <h3>Pruébalo con tus datos — gratis, sin registro</h3>
    <p class="muted">Arrastra tu .csv o .xlsx de ventas. El análisis corre al momento y no guardamos tu archivo.</p>
    <div class="drop" id="drop" onclick="document.getElementById('file').click()">
      Arrastra tu archivo aquí <span style="font-weight:400">o haz click para elegirlo</span></div>
    <input type="file" id="file" accept=".csv,.xlsx,.xls" style="display:none" onchange="up(this.files[0])">
    <div id="result"></div>
  </div>

  <div class="bigsection"><h2>Lo que los grandes ya saben.</h2>
    <p>Las cadenas grandes deciden con dashboards; la mayoría de negocios pequeños todavía no.
    Esa brecha es tu oportunidad.</p></div>
  <div class="trustgrid">
    <div class="trustcard"><span class="ic">📊</span><b>La mayoría de PYMEs aún no usa IA ni analítica</b>
      <span>La adopción sigue siendo baja en negocios pequeños — quien la usa primero, decide mejor que su competencia.
      <a href="https://www.pipedrive.com/en/blog/small-business-stats" target="_blank" rel="noopener">Fuente: Pipedrive, Small Business Stats</a></span></div>
    <div class="trustcard"><span class="ic">📈</span><b>El retail crece apenas ~1.6% anual</b>
      <span>En el sector de crecimiento más lento, cada punto de margen y cada producto estancado cuentan el doble.
      <a href="https://votednumberone.com/small-business-revenue-by-industry-2026-report/" target="_blank" rel="noopener">Fuente: Small Business Revenue Report 2026</a></span></div>
    <div class="trustcard"><span class="ic">🎯</span><b>Los reportes que usa una cadena, para tu tienda</b>
      <span>Ventas por día, crecimiento mensual, productos en caída, desglose por vendedor — el mismo tipo de análisis
      de las plataformas líderes de retail, desde tu Excel.</span></div>
  </div>

  <div class="bigsection"><h2>Tres pasos. Cero configuración.</h2>
    <p>Sin instalar nada, sin capacitación, sin cambiar cómo trabajas hoy.</p></div>
  <div class="steps">
    <div class="step"><span class="n" data-n="1"></span><b>Sube tu Excel</b><span>El mismo archivo que ya usas. Detectamos fechas, productos y montos automáticamente.</span></div>
    <div class="step"><span class="n" data-n="2"></span><b>Recibe tu reporte</b><span>KPIs claros, gráficas de tendencia y tu top de productos — en segundos, no en horas.</span></div>
    <div class="step"><span class="n" data-n="3"></span><b>Decide con datos</b><span>El resumen ejecutivo te dice qué va bien, qué preocupa y qué hacer esta semana.</span></div>
  </div>

  <div class="sectionhead"><div class="kicker">Tu información, protegida</div><h2>Diseñado para que confíes.</h2></div>
  <div class="trustgrid">
    <div class="trustcard"><span class="ic">🗂️</span><b>Tu archivo no se guarda</b>
      <span>El demo procesa tu Excel en memoria y lo descarta. Con cuenta, solo guardamos las métricas — nunca el archivo original.</span></div>
    <div class="trustcard"><span class="ic">🔒</span><b>Contraseñas cifradas</b>
      <span>Hashing estándar de la industria y conexión HTTPS en todo el sitio. Ni nosotros podemos ver tu contraseña.</span></div>
    <div class="trustcard"><span class="ic">💳</span><b>Tu tarjeta, nunca aquí</b>
      <span>Los pagos se procesan en PayPal o pasarelas locales. Jamás ingresas datos de tarjeta en ReporteFácil.</span></div>
  </div>

  <div class="sectionhead"><div class="kicker">Precios</div><h2>Empieza gratis. Crece cuando te sirva.</h2></div>
  <div class="plans">
    <div class="plancard">
      <h3>Gratis</h3><div class="price">$0</div>
      <ul><li>Reportes manuales ilimitados</li><li>KPIs y gráficas interactivas</li><li>Historial en tu cuenta</li></ul>
      <a class="btn ghost" href="/registro">Crear cuenta gratis</a>
    </div>
    <div class="plancard pro">
      <div class="ribbon">Muy pronto</div>
      <h3>Pro</h3><div class="price">__PRICE__</div>
      <ul><li>Reporte automático cada lunes en tu correo</li><li>Resumen ejecutivo con IA</li><li>Soporte prioritario en menos de 24 h</li></ul>
      <button class="btn" onclick="joinPro()">Unirme a la lista de Pro</button>
      <p class="muted sm" id="promsg" style="margin-top:8px">En fase final de desarrollo. La lista entra primero, con precio de lanzamiento.</p>
    </div>
  </div>

  <div class="sectionhead"><div class="kicker">Preguntas frecuentes</div><h2>Lo que suelen preguntarnos</h2></div>
  <details><summary>¿Necesito saber de Excel o de datos?</summary>
    <p>No. Si sabes guardar un archivo, sabes usar ReporteFácil. Subes tu archivo y el análisis sale solo.</p></details>
  <details><summary>¿Qué pasa con mis datos?</summary>
    <p>En el demo, tu archivo se procesa y se descarta — no lo guardamos. Con cuenta, solo guardamos las métricas calculadas para tu historial, nunca el archivo original.</p></details>
  <details><summary>¿Funciona con mi formato de Excel?</summary>
    <p>Aceptamos .csv y .xlsx. Detectamos automáticamente columnas de fecha, producto y monto aunque tengan otros nombres. Si algo no cuadra, soporte te lo resuelve en menos de 24 h.</p></details>
  <details><summary>¿Cuándo lanza el plan Pro y cómo se pagará?</summary>
    <p>Pro está en fase final de desarrollo. Los de la lista de espera entran primero y con precio de lanzamiento.
    El pago será con link seguro (PayPal y medios locales) — nunca ingresas datos de tarjeta en ReporteFácil.</p></details>

  <div class="hero" style="padding-top:40px">
    <h2>Tu próxima decisión, con datos.</h2>
    <div class="cta"><a class="btn big" href="/registro">Crear cuenta gratis</a></div>
  </div>

  <div class="foot">ReporteFácil · hecho en Ecuador<br>
    <a href="/privacidad">Privacidad</a> · <a href="/terminos">Términos</a> · <a href="/login">Entrar</a> · <a href="/registro">Crear cuenta</a></div>

<script>
async function sub() {
  const em = document.getElementById('em').value.trim();
  const msg = document.getElementById('submsg');
  if (!em) { msg.textContent = 'Escribe tu correo primero.'; return; }
  const r = await fetch('/subscribe', { method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({email:em}) });
  const d = await r.json();
  msg.textContent = d.ok ? '✓ Listo. Te avisamos al lanzar.' : (d.error || 'Error.');
}
async function joinPro() {
  const em = document.getElementById('em').value.trim();
  const msg = document.getElementById('promsg');
  if (!em) { msg.textContent = 'Escribe tu correo en el campo de arriba y vuelve a pulsar.';
    document.getElementById('em').focus(); window.scrollTo({top:0, behavior:'smooth'}); return; }
  const r = await fetch('/subscribe', { method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({email:em, source:'pro'}) });
  const d = await r.json();
  msg.textContent = d.ok ? '✓ Estás en la lista de Pro. Te avisamos al lanzar.' : (d.error || 'Error.');
}
const drop = document.getElementById('drop');
['dragover','dragenter'].forEach(e => drop.addEventListener(e, ev => { ev.preventDefault(); drop.classList.add('on'); }));
['dragleave','drop'].forEach(e => drop.addEventListener(e, ev => { ev.preventDefault(); drop.classList.remove('on'); }));
drop.addEventListener('drop', ev => up(ev.dataTransfer.files[0]));

// Animaciones: reveal al hacer scroll + contadores animados
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.statgrid .stat, .trustgrid .trustcard, .steps .step, .plans .plancard, .bigsection, details')
    .forEach((el, i) => { el.classList.add('reveal', 'd' + (i % 3 + 1)); });
  const io = new IntersectionObserver(entries => {
    entries.forEach(en => { if (en.isIntersecting) { en.target.classList.add('in'); io.unobserve(en.target); } });
  }, { threshold: 0.15 });
  document.querySelectorAll('.reveal').forEach(el => io.observe(el));
  // contadores de la fila de stats
  const cio = new IntersectionObserver(entries => {
    entries.forEach(en => {
      if (!en.isIntersecting) return;
      cio.unobserve(en.target);
      const b = en.target.querySelector('b');
      if (!b) return;
      const html = b.innerHTML, txt = b.textContent;
      const num = parseFloat(txt.replace(/[^0-9.]/g, ''));
      if (isNaN(num) || num === 0) return;
      let cur = 0; const steps = 28; const inc = num / steps;
      const tick = () => {
        cur += inc;
        if (cur >= num) { b.innerHTML = html; return; }
        b.textContent = txt.replace(/[0-9.]+/, Math.round(cur));
        requestAnimationFrame(() => setTimeout(tick, 26));
      };
      tick();
    });
  }, { threshold: 0.6 });
  document.querySelectorAll('.statgrid .stat').forEach(el => cio.observe(el));
});

// Mockup del producto en el hero — datos de EJEMPLO, etiquetados como tales.
window.addEventListener('load', () => {
  const mock = document.getElementById('mockreport');
  if (!mock) return;
  renderReport(mock, {
    metrics: {
      filas: 300, col_monto: 'Total', col_categoria: 'Producto', col_vendedor: 'Vendedor',
      total: 18557.06, transacciones: 300, promedio: 61.86, variacion_pct: 12.4,
      crecimiento_mensual_pct: 8.2, mejor_dia: 'Sábado', concentracion_pct: 25.3,
      alertas: ["'Chompa' cayó 31% en la segunda mitad del período. Decide: ¿promoción para moverlo o dejar de reponerlo?"],
      serie_semanal: [
        {semana:'S1', total:1350},{semana:'S2', total:1520},{semana:'S3', total:1410},
        {semana:'S4', total:1680},{semana:'S5', total:1590},{semana:'S6', total:1740},
        {semana:'S7', total:1620},{semana:'S8', total:1890},{semana:'S9', total:1760},
        {semana:'S10', total:1950},{semana:'S11', total:1830},{semana:'S12', total:2055}],
      por_dia: [
        {dia:'Lunes', total:1890},{dia:'Martes', total:2100},{dia:'Miércoles', total:2350},
        {dia:'Jueves', total:2600},{dia:'Viernes', total:3200},{dia:'Sábado', total:4100},{dia:'Domingo', total:2317}],
      serie_mensual: [
        {mes:'2026-05', total:5600},{mes:'2026-06', total:6100},{mes:'2026-07', total:6857}],
      top_categorias: [
        {nombre:'Zapatos', total:4690.9},{nombre:'Camiseta', total:4357.38},
        {nombre:'Pantalón', total:3538.94},{nombre:'Gorra', total:3277.42},{nombre:'Chompa', total:2692.42}],
      productos_caida: [{nombre:'Chompa', cambio_pct:-31.2}]
    },
    resumen: 'Buen cierre: creces 8,2% mensual y el sábado es tu motor — concentra ahí tu mejor inventario y personal. Chompa lleva un mes cayendo: o la mueves con promoción esta semana o libera ese capital. El ticket promedio se mantiene estable, señal de que el crecimiento viene por más clientes, no por compras más grandes.'
  });
  mock.insertAdjacentHTML('beforeend',
    '<p class="muted sm" style="margin-top:10px">Ejemplo con datos ficticios. Abajo puedes generarlo con tus datos reales.</p>');
});
async function up(file, intento) {
  if (!file) return;
  intento = intento || 1;
  const out = document.getElementById('result');
  out.innerHTML = '<p class="muted">Analizando ' + escH(file.name) + '…</p>';
  const fd = new FormData(); fd.append('file', file);
  try {
    const r = await fetch('/report-demo', { method:'POST', body: fd });
    const ct = r.headers.get('content-type') || '';
    if (!ct.includes('application/json')) throw new Error('cold-start');
    const d = await r.json();
    if (d.error) { out.innerHTML = '<div class="card errbox">'+escH(d.error)+'</div>'; return; }
    try {
      renderReport(out, d);
      out.insertAdjacentHTML('beforeend',
        '<div class="row" style="justify-content:center"><a class="btn" href="/registro">Crear cuenta para guardar este reporte</a></div>');
    } catch(re) {
      out.innerHTML = '<div class="card errbox">El análisis funcionó pero falló el dibujado: '+escH(String(re))+'</div>';
    }
    return;
  } catch(e) {
    if (intento < 3) {
      let s = 20;
      out.innerHTML = '<p class="muted">El servidor está despertando (plan gratuito) — reintento en <b id="cd">'+s+'</b> s…</p>';
      const t = setInterval(() => {
        s--; const el = document.getElementById('cd');
        if (el) el.textContent = s;
        if (s <= 0) { clearInterval(t); up(file, intento + 1); }
      }, 1000);
    } else {
      out.innerHTML = '<div class="card errbox">No pude conectar con el servidor. Espera un minuto y vuelve a intentar.</div>';
    }
  }
}
</script>""".replace("__PRICE__", PLAN_PRICE)

AUTH_FORM = """
  <div class="topbar"><a class="brand" href="/">Reporte<span>Fácil</span></a></div>
  <div class="card authcard">
    <h2 id="t" style="margin-bottom:14px"></h2>
    <form method="post">
      <input type="email" name="email" placeholder="tu@correo.com" required class="full">
      <input type="password" name="password" placeholder="Contraseña (mínimo 8 caracteres)" required minlength="8" class="full">
      <span id="biz"></span>
      <button class="btn" style="width:100%">Continuar</button>
    </form>
    <p class="muted sm" style="margin-top:12px" id="alt"></p>
  </div>
  <script>
    const mode = "__MODE__";
    document.getElementById('t').textContent = mode === 'registro' ? 'Crea tu cuenta gratis' : 'Bienvenido de vuelta';
    if (mode === 'registro') {
      document.getElementById('biz').innerHTML =
        '<input type="text" name="business" placeholder="Nombre de tu negocio (opcional)" class="full">'
        + '<select name="industry" class="full"><option value="">Sector de tu negocio (opcional)...</option>'
        + __INDUSTRIES__.map(i => '<option>' + i + '</option>').join('') + '</select>';
      document.getElementById('alt').innerHTML = '¿Ya tienes cuenta? <a href="/login">Entra aquí</a>';
    } else {
      document.getElementById('alt').innerHTML = '¿No tienes cuenta? <a href="/registro">Créala gratis</a>';
    }
  </script>"""

ADMIN_LOGIN_FORM = """
  <div class="card authcard" style="margin-top:70px">
    <h2 style="margin-bottom:14px">Panel de administración</h2>
    <form method="post">
      <input type="password" name="password" placeholder="Contraseña de administrador" required class="full">
      <button class="btn" style="width:100%">Entrar</button>
    </form>
  </div>"""

init_db()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8090"))
    print(f"\n  ReporteFácil v3 -> http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
