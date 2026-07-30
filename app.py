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
CATEGORY_HINTS = ["producto", "product", "item", "categoria", "categoría", "category", "servicio", "cliente", "customer", "vendedor", "sku", "nombre"]


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
    amount_col = find_col(df, AMOUNT_HINTS, want_numeric=True)
    date_col = find_col(df, DATE_HINTS)
    cat_col = find_col(df, CATEGORY_HINTS)
    out = {"filas": int(len(df)), "col_monto": amount_col, "col_fecha": date_col, "col_categoria": cat_col}
    if amount_col:
        s = pd.to_numeric(df[amount_col], errors="coerce").dropna()
        out.update(total=round(float(s.sum()), 2), promedio=round(float(s.mean()), 2) if len(s) else 0,
                   maximo=round(float(s.max()), 2) if len(s) else 0, transacciones=int(len(s)))
    if cat_col and amount_col:
        top = (df.assign(_m=pd.to_numeric(df[amount_col], errors="coerce"))
               .groupby(cat_col)["_m"].sum().sort_values(ascending=False).head(5))
        out["top_categorias"] = [{"nombre": str(k), "total": round(float(v), 2)} for k, v in top.items()]
    if date_col and amount_col:
        d = df.copy()
        d["_f"] = pd.to_datetime(d[date_col], errors="coerce", dayfirst=True)
        d["_m"] = pd.to_numeric(d[amount_col], errors="coerce")
        d = d.dropna(subset=["_f", "_m"])
        if len(d):
            serie = d.groupby(d["_f"].dt.to_period("W"))["_m"].sum()
            out["serie_semanal"] = [{"semana": str(k), "total": round(float(v), 2)} for k, v in serie.tail(12).items()]
            if len(serie) >= 2 and float(serie.iloc[-2]) != 0:
                out["variacion_pct"] = round((float(serie.iloc[-1]) / float(serie.iloc[-2]) - 1) * 100, 1)
    return out


def ai_summary(metrics):
    if not API_KEY:
        return None
    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=API_KEY)
        resp = client.messages.create(
            model=MODEL, max_tokens=400,
            system=("Eres un analista de negocios para PYMEs latinoamericanas. Con las métricas dadas, "
                    "escribe un resumen ejecutivo de 4-6 frases en español: qué va bien, qué preocupa, "
                    "y UNA recomendación accionable. Sin inventar datos. Tono directo."),
            messages=[{"role": "user", "content": json.dumps(metrics, ensure_ascii=False)}])
        return resp.content[0].text.strip()
    except Exception:  # noqa: BLE001
        return None


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
    except Exception as e:  # noqa: BLE001
        return jsonify(error=str(e)), 400
    if df.empty:
        return jsonify(error="El archivo está vacío."), 400
    m = analyze(df)
    return jsonify(metrics=m, resumen=ai_summary(m))


# ---------------------------------- auth ------------------------------------
@app.route("/registro", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        return page(AUTH_FORM.replace("__MODE__", "registro"), "Crear cuenta — ReporteFácil")
    email = (request.form.get("email") or "").strip().lower()
    pw = request.form.get("password") or ""
    business = (request.form.get("business") or "").strip()
    if "@" not in email or len(pw) < 8:
        return page(err_box("Correo inválido o contraseña menor a 8 caracteres.")
                    + AUTH_FORM.replace("__MODE__", "registro"), "Crear cuenta")
    con = db()
    try:
        cur = con.execute("INSERT INTO users (email, pw_hash, business, created_at) VALUES (?,?,?,?)",
                          (email, generate_password_hash(pw), business, now()))
        con.commit()
        session["uid"] = cur.lastrowid
    except sqlite3.IntegrityError:
        con.close()
        return page(err_box("Ese correo ya tiene cuenta. <a href='/login'>Entra aquí</a>.")
                    + AUTH_FORM.replace("__MODE__", "registro"), "Crear cuenta")
    con.close()
    return redirect(url_for("dashboard"))


@app.route("/login", methods=["GET", "POST"])
def login_page():
    if request.method == "GET":
        return page(AUTH_FORM.replace("__MODE__", "login"), "Entrar — ReporteFácil")
    email = (request.form.get("email") or "").strip().lower()
    pw = request.form.get("password") or ""
    con = db()
    u = con.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    con.close()
    if not u or not check_password_hash(u["pw_hash"], pw):
        return page(err_box("Correo o contraseña incorrectos.")
                    + AUTH_FORM.replace("__MODE__", "login"), "Entrar")
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


@app.route("/dashboard")
@login_required
def dashboard():
    u = current_user()
    con = db()
    reports = con.execute(
        "SELECT id, filename, created_at, metrics FROM reports WHERE user_id=? ORDER BY id DESC LIMIT 50",
        (u["id"],)).fetchall()
    con.close()
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
    body = f"""
    {portal_nav(u, 'dash')}
    <div class="pagehead"><h1>Hola, {esc(u['business'] or u['email'].split('@')[0])}</h1>{badge}</div>
    <div class="card">
      <h3>Nuevo reporte</h3>
      <p class="muted">Sube tu archivo de ventas (.csv o .xlsx). El reporte se genera al instante y queda en tu historial.</p>
      <form action="/reporte" method="post" enctype="multipart/form-data" class="row">
        <input type="file" name="file" accept=".csv,.xlsx,.xls" required>
        <button class="btn">Generar reporte</button>
      </form>
    </div>
    <div class="card">
      <h3>Historial</h3>
      {'<table><tr><th></th><th>Archivo</th><th>Fecha</th><th class="num">Ventas</th><th></th></tr>' + rows + '</table>' if rows else '<p class="muted">Aún no tienes reportes. Sube tu primer archivo arriba.</p>'}
    </div>"""
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
    s = ai_summary(m)
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
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
<style>
  :root {
    --ink:#0e1b2c; --ink2:#42526b; --bg:#f6f8fb; --card:#ffffff; --line:#e5eaf1;
    --acc:#0e9f6e; --accd:#0b7f58; --blue:#2563eb; --warn:#b45309; --err:#b91c1c;
    --r:14px; --shadow:0 1px 2px rgba(14,27,44,.05), 0 10px 30px rgba(14,27,44,.07);
  }
  * { box-sizing:border-box; margin:0; }
  html { scroll-behavior:smooth; }
  body { background:var(--bg); color:var(--ink); -webkit-font-smoothing:antialiased;
    font:16px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,sans-serif; }
  .wrap { max-width:1040px; margin:0 auto; padding:20px 22px 90px; }
  h1 { font-size:1.7rem; letter-spacing:-.02em; line-height:1.2; }
  h2 { font-size:1.45rem; letter-spacing:-.02em; }
  h3 { font-size:1.05rem; margin-bottom:8px; }
  a { color:var(--blue); text-decoration:none; }
  .muted { color:var(--ink2); font-size:.92rem; } .sm { font-size:.82rem; }
  .topbar { display:flex; justify-content:space-between; align-items:center; padding:6px 0 22px; }
  .brand { font-weight:800; font-size:1.15rem; color:var(--ink); letter-spacing:-.02em; }
  .brand span { color:var(--acc); } .brand em { font-style:normal; color:var(--ink2); font-weight:500; font-size:.8rem; }
  .tabs { display:flex; gap:6px; align-items:center; }
  .tabs a { padding:8px 14px; border-radius:10px; color:var(--ink2); font-weight:500; font-size:.92rem; }
  .tabs a.on, .tabs a:hover { background:#eaeff6; color:var(--ink); }
  .navlink { padding:9px 16px; color:var(--ink2); font-weight:600; }
  .btn { display:inline-block; background:var(--acc); color:#fff; font-weight:650; border:0;
    border-radius:11px; padding:11px 20px; cursor:pointer; font:inherit; font-size:.95rem;
    box-shadow:0 1px 2px rgba(14,27,44,.15); transition:background .15s, transform .1s; }
  .btn:hover { background:var(--accd); } .btn:active { transform:scale(.98); }
  .btn.ghost { background:#eef2f7; color:var(--ink); box-shadow:none; }
  .btn.ghost:hover { background:#e2e8f1; }
  .btn.sm { padding:6px 12px; font-size:.83rem; border-radius:9px; }
  .btn.big { padding:14px 28px; font-size:1.02rem; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:var(--r);
    padding:24px; margin:16px 0; box-shadow:var(--shadow); }
  .errbox { border-color:#fecaca; background:#fef2f2; color:var(--err); }
  .row { display:flex; gap:10px; flex-wrap:wrap; align-items:center; margin-top:12px; }
  input, textarea, select { background:#fff; color:var(--ink); border:1.5px solid var(--line);
    border-radius:11px; padding:11px 14px; font:inherit; font-size:.95rem; outline:none; transition:border .15s; }
  input:focus, textarea:focus { border-color:var(--acc); }
  textarea { width:100%; min-height:100px; resize:vertical; }
  .full { width:100%; margin-bottom:10px; }
  .pagehead { display:flex; gap:12px; align-items:center; flex-wrap:wrap; margin:8px 0 4px; }
  .badge { background:#eef2f7; color:var(--ink2); border-radius:20px; padding:4px 13px; font-size:.78rem; font-weight:650; }
  .badge.ok { background:#d9f2e5; color:var(--accd); }
  .badge.warn { background:#fdf0dd; color:var(--warn); }
  .chip { background:#eef2f7; border-radius:8px; padding:2px 9px; font-size:.78rem; color:var(--ink2); font-weight:600; }
  .back { display:inline-block; margin:4px 0 6px; color:var(--ink2); font-size:.88rem; font-weight:550; }
  table { width:100%; border-collapse:collapse; font-size:.92rem; }
  td, th { padding:10px 12px; border-bottom:1px solid var(--line); text-align:left; }
  th { color:var(--ink2); font-size:.78rem; text-transform:uppercase; letter-spacing:.04em; }
  tr:last-child td { border-bottom:0; } .num { text-align:right; font-variant-numeric:tabular-nums; }
  .kpis { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:12px; margin:16px 0; }
  .kpi { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px 18px; box-shadow:var(--shadow); }
  .kpi b { display:block; font-size:1.45rem; letter-spacing:-.02em; font-variant-numeric:tabular-nums; }
  .kpi span { font-size:.78rem; color:var(--ink2); font-weight:550; text-transform:uppercase; letter-spacing:.04em; }
  .kpi.up b { color:var(--accd); } .kpi.down b { color:var(--err); } .kpi.warnk b { color:var(--warn); }
  .sum { background:linear-gradient(135deg,#f0fdf7,#eff6ff); border:1px solid #d3e9de;
    border-radius:12px; padding:18px 20px; margin:14px 0; white-space:pre-wrap; font-size:.96rem; }
  .sum::before { content:"Resumen ejecutivo"; display:block; font-size:.72rem; font-weight:700;
    letter-spacing:.08em; text-transform:uppercase; color:var(--accd); margin-bottom:6px; }
  .charts { display:grid; grid-template-columns:1fr 1fr; gap:14px; margin:14px 0; }
  .chartbox { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px; box-shadow:var(--shadow); }
  .chartbox h4 { font-size:.8rem; color:var(--ink2); text-transform:uppercase; letter-spacing:.05em; margin-bottom:10px; }
  @media (max-width:760px) { .charts { grid-template-columns:1fr; } }
  .msg { background:#f6f8fb; border-radius:12px; padding:12px 16px; margin:10px 0; font-size:.95rem; }
  .msg.soporte { background:#f0fdf7; border:1px solid #d3e9de; }
  .msg.cliente { background:#eff6ff; border:1px solid #dbe7fb; }
  .msghead { font-weight:700; font-size:.82rem; margin-bottom:4px; }
  .msghead span { color:var(--ink2); font-weight:500; }
  .replyform { margin-top:14px; }
  /* ------ landing ------ */
  .hero { text-align:center; padding:64px 0 30px; }
  .hero h1 { font-size:clamp(2rem, 5vw, 3.1rem); letter-spacing:-.035em; line-height:1.12; }
  .hero h1 em { font-style:normal; color:var(--acc); }
  .hero p.lead { color:var(--ink2); font-size:1.12rem; max-width:620px; margin:18px auto 0; }
  .cta { display:flex; gap:10px; justify-content:center; margin:30px 0 8px; flex-wrap:wrap; }
  .cta input { min-width:280px; }
  .trust { color:var(--ink2); font-size:.82rem; }
  .steps { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:14px; margin:18px 0; }
  .step { background:var(--card); border:1px solid var(--line); border-radius:var(--r); padding:22px; box-shadow:var(--shadow); }
  .step .n { display:inline-flex; width:30px; height:30px; align-items:center; justify-content:center;
    background:#d9f2e5; color:var(--accd); font-weight:800; border-radius:9px; margin-bottom:10px; }
  .step b { display:block; margin-bottom:4px; }
  .step span { color:var(--ink2); font-size:.9rem; }
  .sectionhead { text-align:center; margin:56px 0 8px; }
  .sectionhead .kicker { color:var(--acc); font-weight:700; font-size:.78rem; letter-spacing:.1em; text-transform:uppercase; }
  .drop { border:2px dashed #cdd8e5; border-radius:var(--r); padding:38px 20px; text-align:center;
    color:var(--ink2); cursor:pointer; margin:14px 0; transition:all .15s; background:#fbfcfe; font-weight:550; }
  .drop:hover, .drop.on { border-color:var(--acc); background:#f0fdf7; color:var(--accd); }
  .plans { display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:16px; margin:18px 0; }
  .plancard { background:var(--card); border:1.5px solid var(--line); border-radius:var(--r); padding:26px; box-shadow:var(--shadow); position:relative; }
  .plancard.pro { border-color:var(--acc); }
  .plancard .ribbon { position:absolute; top:-11px; left:22px; background:var(--acc); color:#fff;
    font-size:.7rem; font-weight:750; letter-spacing:.06em; text-transform:uppercase; padding:3px 12px; border-radius:20px; }
  .plancard .price { font-size:1.9rem; font-weight:800; letter-spacing:-.03em; margin:4px 0 12px; }
  .plancard ul { list-style:none; margin:0 0 16px; padding:0; }
  .plancard li { padding:5px 0 5px 26px; position:relative; color:var(--ink2); font-size:.93rem; }
  .plancard li::before { content:"✓"; position:absolute; left:2px; color:var(--acc); font-weight:800; }
  details { background:var(--card); border:1px solid var(--line); border-radius:12px;
    padding:16px 20px; margin:10px 0; box-shadow:var(--shadow); }
  details summary { cursor:pointer; font-weight:650; font-size:.98rem; }
  details p { margin-top:10px; color:var(--ink2); font-size:.93rem; }
  .foot { text-align:center; color:var(--ink2); font-size:.82rem; margin-top:64px; padding-top:24px; border-top:1px solid var(--line); }
  .foot a { color:var(--ink2); text-decoration:underline; margin:0 6px; }
  .authcard { max-width:420px; margin:40px auto; }
  .legal { max-width:720px; margin:20px auto; } .legal p { margin:12px 0; color:var(--ink2); }
  .legal h2 { margin-bottom:6px; color:var(--ink); } .legal b { color:var(--ink); }
  .trustgrid { display:grid; grid-template-columns:repeat(auto-fit,minmax(230px,1fr)); gap:14px; margin:18px 0; }
  .trustcard { background:var(--card); border:1px solid var(--line); border-radius:var(--r); padding:22px; box-shadow:var(--shadow); }
  .trustcard .ic { display:inline-flex; width:38px; height:38px; align-items:center; justify-content:center;
    background:#d9f2e5; border-radius:11px; margin-bottom:10px; font-size:1.1rem; }
  .trustcard b { display:block; margin-bottom:4px; }
  .trustcard span { color:var(--ink2); font-size:.9rem; }
  .paynote { display:flex; gap:10px; align-items:flex-start; background:#f0fdf7; border:1px solid #d3e9de;
    border-radius:11px; padding:12px 16px; margin-top:12px; font-size:.88rem; color:var(--ink2); }
</style>
</head>
<body><div class="wrap">__CONTENT__</div>
<script>
function money(n) { return '$' + Number(n).toLocaleString('es-EC', {maximumFractionDigits:2}); }
function escH(s){const d=document.createElement('div');d.textContent=s??'';return d.innerHTML;}
function kpiH(v,l,cls){ return '<div class="kpi '+(cls||'')+'"><b>'+v+'</b><span>'+l+'</span></div>'; }
function renderReport(root, data) {
  const m = data.metrics; let h = '';
  h += '<div class="kpis">';
  if (m.total !== undefined) {
    h += kpiH(money(m.total),'ventas totales') + kpiH(m.transacciones,'transacciones')
       + kpiH(money(m.promedio),'ticket promedio') + kpiH(money(m.maximo),'venta máxima');
    if (m.variacion_pct !== undefined)
      h += kpiH((m.variacion_pct>0?'+':'')+m.variacion_pct+'%','vs semana anterior', m.variacion_pct>=0?'up':'down');
  } else h += kpiH(m.filas,'filas leídas');
  h += '</div>';
  if (data.resumen) h += '<div class="sum">'+escH(data.resumen)+'</div>';
  const hasSerie = m.serie_semanal && m.serie_semanal.length > 1;
  const hasTop = m.top_categorias && m.top_categorias.length;
  if (hasSerie || hasTop) {
    h += '<div class="charts">';
    if (hasSerie) h += '<div class="chartbox"><h4>Ventas por semana</h4><canvas id="c-serie" height="220"></canvas></div>';
    if (hasTop) h += '<div class="chartbox"><h4>Top ' + escH(String(m.col_categoria||'categorías')) + '</h4><canvas id="c-top" height="220"></canvas></div>';
    h += '</div>';
  }
  if (!m.col_monto) h += '<p class="muted">No detecté una columna de montos. Nombra una columna "total", "monto" o "venta" para el análisis completo.</p>';
  root.innerHTML = h;
  if (typeof Chart === 'undefined') return;
  Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
  Chart.defaults.color = '#42526b';
  if (hasSerie) {
    new Chart(document.getElementById('c-serie'), { type:'line',
      data:{ labels:m.serie_semanal.map(x=>x.semana.split('/')[0]),
        datasets:[{ data:m.serie_semanal.map(x=>x.total), borderColor:'#0e9f6e',
          backgroundColor:'rgba(14,159,110,.09)', fill:true, tension:.35, pointRadius:3,
          pointBackgroundColor:'#0e9f6e', borderWidth:2.5 }]},
      options:{ plugins:{legend:{display:false}}, scales:{ y:{ ticks:{ callback:v=>money(v) }, grid:{color:'#eef2f7'} }, x:{ grid:{display:false} } } } });
  }
  if (hasTop) {
    new Chart(document.getElementById('c-top'), { type:'bar',
      data:{ labels:m.top_categorias.map(x=>x.nombre),
        datasets:[{ data:m.top_categorias.map(x=>x.total),
          backgroundColor:['#0e9f6e','#2563eb','#7c3aed','#d97706','#64748b'], borderRadius:8 }]},
      options:{ indexAxis:'y', plugins:{legend:{display:false}},
        scales:{ x:{ ticks:{ callback:v=>money(v) }, grid:{color:'#eef2f7'} }, y:{ grid:{display:false} } } } });
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
    <h1>Deja de adivinar<br>qué pasa con <em>tus ventas</em>.</h1>
    <p class="lead">Sube tu Excel de siempre y recibe al instante un reporte ejecutivo con gráficas:
    cuánto vendiste, qué producto jala, qué está cayendo — y qué hacer al respecto. En español, para tu negocio.</p>
    <div class="cta">
      <input type="email" id="em" placeholder="tu@correo.com">
      <button class="btn big" onclick="sub()">Quiero acceso anticipado</button>
    </div>
    <div class="trust" id="submsg">Gratis · Sin tarjeta · Sin spam</div>
  </div>

  <div class="card" id="demo">
    <h3>Pruébalo con tus datos — gratis, sin registro</h3>
    <p class="muted">Arrastra tu .csv o .xlsx de ventas. El análisis corre al momento y no guardamos tu archivo.</p>
    <div class="drop" id="drop" onclick="document.getElementById('file').click()">
      Arrastra tu archivo aquí <span style="font-weight:400">o haz click para elegirlo</span></div>
    <input type="file" id="file" accept=".csv,.xlsx,.xls" style="display:none" onchange="up(this.files[0])">
    <div id="result"></div>
  </div>

  <div class="sectionhead"><div class="kicker">Cómo funciona</div><h2>Tres pasos. Cero configuración.</h2></div>
  <div class="steps">
    <div class="step"><span class="n">1</span><b>Sube tu Excel</b><span>El mismo archivo que ya usas. Detectamos fechas, productos y montos automáticamente.</span></div>
    <div class="step"><span class="n">2</span><b>Recibe tu reporte</b><span>KPIs claros, gráficas de tendencia y tu top de productos — en segundos, no en horas.</span></div>
    <div class="step"><span class="n">3</span><b>Decide con datos</b><span>El resumen ejecutivo te dice qué va bien, qué preocupa y qué hacer esta semana.</span></div>
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
async function up(file) {
  if (!file) return;
  const out = document.getElementById('result');
  out.innerHTML = '<p class="muted">Analizando ' + escH(file.name) + '…</p>';
  const fd = new FormData(); fd.append('file', file);
  try {
    const r = await fetch('/report-demo', { method:'POST', body: fd });
    const d = await r.json();
    if (d.error) { out.innerHTML = '<div class="card errbox">'+escH(d.error)+'</div>'; return; }
    renderReport(out, d);
    out.insertAdjacentHTML('beforeend',
      '<div class="row" style="justify-content:center"><a class="btn" href="/registro">Crear cuenta para guardar este reporte</a></div>');
  } catch(e) { out.innerHTML = '<div class="card errbox">Error: '+escH(String(e))+'</div>'; }
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
        '<input type="text" name="business" placeholder="Nombre de tu negocio (opcional)" class="full">';
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
