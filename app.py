"""
ReporteFácil v2 — Portal de autogestión de clientes
Landing pública + demo, registro/login, dashboard con historial de reportes,
plan con pago por link (activación manual), soporte por tickets, panel admin.

Variables de entorno (.env):
  ANTHROPIC_API_KEY  opcional — resumen ejecutivo con IA
  SECRET_KEY         recomendado en producción (firma de sesiones)
  ADMIN_PASSWORD     obligatorio para entrar a /admin (default: cambiame)
  PAYMENT_LINK       tu link de pago (PayPal.me, Payphone, etc.)
  PLAN_PRICE         precio mostrado (default: $29/mes)

Uso local:  python app.py  ->  http://localhost:8090
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
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "portal.db")


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
        created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE NOT NULL,
        pw_hash TEXT NOT NULL, business TEXT DEFAULT '',
        plan_status TEXT NOT NULL DEFAULT 'free',  -- free | pending | active
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
        sender TEXT NOT NULL,  -- cliente | soporte
        body TEXT NOT NULL, created_at TEXT NOT NULL);
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


def page(content, title="ReporteFácil"):
    return render_template_string(
        LAYOUT.replace("__CONTENT__", content).replace("__TITLE__", title))


# ------------------------------ rutas públicas ------------------------------
@app.route("/")
def landing():
    logged = bool(session.get("uid"))
    return page(LANDING_BODY.replace("__NAV__",
        '<a class="btn" href="/dashboard">Mi panel</a>' if logged
        else '<a class="btn ghost" href="/login">Entrar</a> <a class="btn" href="/registro">Crear cuenta</a>'))


@app.route("/subscribe", methods=["POST"])
def subscribe():
    email = ((request.get_json(silent=True) or {}).get("email") or "").strip().lower()
    if "@" not in email or "." not in email.split("@")[-1] or len(email) < 6:
        return jsonify(error="Correo inválido."), 400
    con = db()
    try:
        con.execute("INSERT INTO leads (email, created_at) VALUES (?,?)", (email, now()))
        con.commit()
    except sqlite3.IntegrityError:
        pass
    finally:
        con.close()
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
        return page(AUTH_FORM.replace("__MODE__", "registro"), "Crear cuenta")
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
        return page(AUTH_FORM.replace("__MODE__", "login"), "Entrar")
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
@app.route("/dashboard")
@login_required
def dashboard():
    u = current_user()
    con = db()
    reports = con.execute(
        "SELECT id, filename, created_at, metrics FROM reports WHERE user_id=? ORDER BY id DESC LIMIT 50",
        (u["id"],)).fetchall()
    open_tickets = con.execute(
        "SELECT COUNT(*) n FROM tickets WHERE user_id=? AND status='abierto'", (u["id"],)).fetchone()["n"]
    con.close()
    rows = "".join(
        f"<tr><td>#{r['id']}</td><td>{esc(r['filename'])}</td>"
        f"<td>{r['created_at'][:10]}</td>"
        f"<td>${json.loads(r['metrics']).get('total', '—')}</td>"
        f"<td><a href='/reporte/{r['id']}'>ver</a></td></tr>" for r in reports)
    plan_badge = {"free": "Gratis", "pending": "Pago en verificación", "active": "Activo ✓"}[u["plan_status"]]
    body = f"""
    <div class="topbar"><h2>Hola, {esc(u['business'] or u['email'])}</h2>
      <div><span class="badge">{plan_badge}</span>
      <a class="btn ghost" href="/plan">Mi plan</a>
      <a class="btn ghost" href="/soporte">Soporte{f' ({open_tickets})' if open_tickets else ''}</a>
      <a class="btn ghost" href="/logout">Salir</a></div></div>
    <div class="card"><h3>Nuevo reporte</h3>
      <div class="note">Sube tu .csv o .xlsx de ventas. El reporte se guarda en tu historial.</div>
      <form action="/reporte" method="post" enctype="multipart/form-data" class="row">
        <input type="file" name="file" accept=".csv,.xlsx,.xls" required>
        <button>Generar y guardar</button></form></div>
    <div class="card"><h3>Historial</h3>
      {'<table><tr><th>#</th><th>Archivo</th><th>Fecha</th><th>Ventas</th><th></th></tr>' + rows + '</table>' if rows else '<div class="note">Aún no tienes reportes.</div>'}
    </div>"""
    return page(body, "Mi panel")


@app.route("/reporte", methods=["POST"])
@login_required
def create_report():
    u = current_user()
    f = request.files.get("file")
    if not f or not f.filename:
        return page(err_box("Sube un archivo.") + back_link("/dashboard"))
    try:
        df = parse_upload(f)
        if df.empty:
            raise ValueError("El archivo está vacío.")
        m = analyze(df)
    except Exception as e:  # noqa: BLE001
        return page(err_box(f"No pude procesar el archivo: {esc(str(e))}") + back_link("/dashboard"))
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
        return page(err_box("Reporte no encontrado.") + back_link("/dashboard"))
    m = json.loads(r["metrics"])
    kpis = ""
    if m.get("total") is not None:
        kpis += kpi(f"${m['total']:,}", "ventas totales") + kpi(m.get("transacciones", "—"), "transacciones")
        kpis += kpi(f"${m.get('promedio', 0):,}", "ticket promedio") + kpi(f"${m.get('maximo', 0):,}", "venta máxima")
        if m.get("variacion_pct") is not None:
            kpis += kpi(f"{m['variacion_pct']}%", "vs semana anterior")
    top = ""
    if m.get("top_categorias"):
        top = ("<table><tr><th>" + esc(str(m.get("col_categoria"))) + "</th><th>Total</th></tr>"
               + "".join(f"<tr><td>{esc(t['nombre'])}</td><td>${t['total']:,}</td></tr>" for t in m["top_categorias"])
               + "</table>")
    body = f"""
    {back_link('/dashboard')}
    <div class="card"><h3>Reporte #{r['id']} — {esc(r['filename'])}</h3>
      <div class="note">{r['created_at'][:16].replace('T', ' ')} UTC</div>
      <div class="kpis">{kpis or kpi(m['filas'], 'filas leídas')}</div>
      {('<div class="sum">' + esc(r['summary']) + '</div>') if r['summary'] else ''}
      {top}</div>"""
    return page(body, f"Reporte #{r['id']}")


@app.route("/plan")
@login_required
def plan():
    u = current_user()
    status = u["plan_status"]
    pay = (f'<a class="btn" href="{esc(PAYMENT_LINK)}" target="_blank">Pagar {esc(PLAN_PRICE)}</a>'
           if PAYMENT_LINK else '<div class="note">El administrador aún no configuró el link de pago (variable PAYMENT_LINK).</div>')
    blocks = {
        "free": f"""<p>Estás en el plan <b>gratuito</b> (reportes manuales ilimitados).</p>
            <p>El plan <b>Pro ({esc(PLAN_PRICE)})</b> incluye: reporte automático cada lunes en tu correo,
            resumen ejecutivo con IA y soporte prioritario.</p>
            <div class="row">{pay}
            <form action="/plan/ya-pague" method="post"><button class="ghost">Ya pagué — verificar</button></form></div>
            <div class="note">Tras pagar, haz click en "Ya pagué". Activamos tu cuenta en menos de 24 h.</div>""",
        "pending": "<p>Tu pago está <b>en verificación</b>. Activamos tu plan en menos de 24 h. Si tarda más, abre un ticket de soporte.</p>",
        "active": "<p>Plan <b>Pro activo</b> ✓ — gracias. Tu reporte automático llega cada lunes.</p>",
    }
    return page(back_link("/dashboard") + f'<div class="card"><h3>Mi plan</h3>{blocks[status]}</div>', "Mi plan")


@app.route("/plan/ya-pague", methods=["POST"])
@login_required
def plan_paid():
    con = db()
    con.execute("UPDATE users SET plan_status='pending' WHERE id=? AND plan_status='free'", (session["uid"],))
    con.commit()
    con.close()
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
        f"<tr><td>#{t['id']}</td><td><a href='/soporte/{t['id']}'>{esc(t['subject'])}</a></td>"
        f"<td>{t['status']}</td><td>{t['created_at'][:10]}</td></tr>" for t in tickets)
    body = f"""
    {back_link('/dashboard')}
    <div class="card"><h3>Nuevo ticket</h3>
      <form method="post">
        <input type="text" name="subject" placeholder="Asunto" required style="width:100%;margin-bottom:8px">
        <textarea name="body" placeholder="Describe tu problema o pregunta..." required></textarea>
        <div class="row"><button>Enviar</button></div></form></div>
    <div class="card"><h3>Mis tickets</h3>
      {'<table><tr><th>#</th><th>Asunto</th><th>Estado</th><th>Fecha</th></tr>' + rows + '</table>' if rows else '<div class="note">Sin tickets.</div>'}</div>"""
    return page(body, "Soporte")


@app.route("/soporte/<int:tid>", methods=["GET", "POST"])
@login_required
def support_thread(tid):
    u = current_user()
    con = db()
    t = con.execute("SELECT * FROM tickets WHERE id=? AND user_id=?", (tid, u["id"])).fetchone()
    if not t:
        con.close()
        return page(err_box("Ticket no encontrado.") + back_link("/soporte"))
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
        f"<div class='msg {m['sender']}'><b>{'Tú' if m['sender'] == 'cliente' else 'Soporte'}</b>"
        f"<span class='note'> · {m['created_at'][:16].replace('T', ' ')}</span><br>{esc(m['body'])}</div>" for m in msgs)
    body = f"""
    {back_link('/soporte')}
    <div class="card"><h3>#{t['id']} — {esc(t['subject'])} <span class="badge">{t['status']}</span></h3>
      {thread}
      <form method="post" style="margin-top:12px">
        <textarea name="body" placeholder="Responder..." required></textarea>
        <div class="row"><button>Responder</button></div></form></div>"""
    return page(body, f"Ticket #{t['id']}")


# ---------------------------------- admin -----------------------------------
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["admin"] = True
            return redirect("/admin")
        return page(err_box("Contraseña incorrecta.") + ADMIN_LOGIN_FORM, "Admin")
    return page(ADMIN_LOGIN_FORM, "Admin")


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
    urows = "".join(
        f"<tr><td>{u['id']}</td><td>{esc(u['email'])}</td><td>{esc(u['business'])}</td><td>{u['plan_status']}</td>"
        f"<td>{'<form method=post action=/admin/activar/' + str(u['id']) + '><button class=mini>Activar Pro</button></form>' if u['plan_status'] != 'active' else '✓'}</td></tr>"
        for u in users)
    trows = "".join(
        f"<tr><td>#{t['id']}</td><td>{esc(t['email'])}</td><td><a href='/admin/ticket/{t['id']}'>{esc(t['subject'])}</a></td>"
        f"<td>{t['created_at'][:10]}</td></tr>" for t in tickets)
    lrows = "".join(f"<tr><td>{esc(x['email'])}</td><td>{x['created_at'][:10]}</td></tr>" for x in leads_rows)
    body = f"""
    <div class="topbar"><h2>Admin</h2><a class="btn ghost" href="/logout">Salir</a></div>
    <div class="kpis">{kpi(nleads, 'leads')}{kpi(len(users), 'usuarios')}{kpi(len(pending), 'pagos por verificar')}{kpi(len(trows and tickets), 'tickets abiertos')}</div>
    <div class="card"><h3>Usuarios {'— ⚠ hay pagos por verificar' if pending else ''}</h3>
      <table><tr><th>ID</th><th>Correo</th><th>Negocio</th><th>Plan</th><th></th></tr>{urows}</table></div>
    <div class="card"><h3>Tickets abiertos</h3>
      {'<table><tr><th>#</th><th>Cliente</th><th>Asunto</th><th>Fecha</th></tr>' + trows + '</table>' if trows else '<div class="note">Ninguno.</div>'}</div>
    <div class="card"><h3>Leads de la landing</h3>
      {'<table><tr><th>Correo</th><th>Fecha</th></tr>' + lrows + '</table>' if lrows else '<div class="note">Ninguno.</div>'}</div>"""
    return page(body, "Admin")


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
        f"<div class='msg {m['sender']}'><b>{'Cliente' if m['sender'] == 'cliente' else 'Tú (soporte)'}</b>"
        f"<span class='note'> · {m['created_at'][:16].replace('T', ' ')}</span><br>{esc(m['body'])}</div>" for m in msgs)
    body = f"""
    {back_link('/admin')}
    <div class="card"><h3>#{t['id']} — {esc(t['subject'])} <span class="badge">{t['status']}</span></h3>
      <div class="note">Cliente: {esc(t['email'])}</div>
      {thread}
      <form method="post" style="margin-top:12px">
        <textarea name="body" placeholder="Respuesta al cliente..."></textarea>
        <div class="row"><button name="action" value="responder">Responder</button>
        <button name="action" value="cerrar" class="ghost">Responder y cerrar</button></div></form></div>"""
    return page(body, f"Admin · Ticket #{t['id']}")


# ----------------------------- html / helpers -------------------------------
def esc(s):
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def kpi(v, label):
    return f"<div class='kpi'><b>{v}</b><span>{label}</span></div>"


def err_box(msg):
    return f"<div class='card err'>{msg}</div>"


def back_link(href):
    return f"<a class='note' href='{href}'>← volver</a>"


LAYOUT = """<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<title>__TITLE__</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root { --bg:#0b1220; --card:#151f33; --acc:#34d399; --acc2:#38bdf8; --txt:#e5edf8; --mut:#8fa3bf; }
  * { box-sizing:border-box; margin:0; }
  body { background:var(--bg); color:var(--txt); font:15px/1.6 system-ui,Segoe UI,sans-serif; }
  .wrap { max-width:880px; margin:0 auto; padding:24px 20px 80px; }
  h1 { font-size:1.9rem; line-height:1.25; } h2 { font-size:1.3rem; } h3 { margin-bottom:8px; }
  h1 span { color:var(--acc); }
  a { color:var(--acc2); text-decoration:none; }
  .topbar { display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px; margin-bottom:16px; }
  .card { background:var(--card); border:1px solid #2a3b58; border-radius:14px; padding:20px; margin:14px 0; }
  .card.err { border-color:#7f1d1d; color:#fca5a5; }
  .row { display:flex; gap:10px; flex-wrap:wrap; align-items:center; margin-top:10px; }
  input, textarea, select { background:#0f1930; color:var(--txt); border:1px solid #2a3b58;
    border-radius:10px; padding:11px 14px; font:inherit; }
  textarea { width:100%; min-height:90px; resize:vertical; }
  button, .btn { background:var(--acc); color:#06281c; font-weight:700; border:0; border-radius:10px;
    padding:11px 20px; cursor:pointer; font:inherit; display:inline-block; }
  .ghost { background:#22304c; color:var(--txt); font-weight:500; }
  .mini { padding:5px 12px; font-size:.82rem; }
  .note { font-size:.82rem; color:var(--mut); }
  .badge { background:#22304c; border-radius:20px; padding:4px 12px; font-size:.78rem; }
  .kpis { display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:10px; margin:14px 0; }
  .kpi { background:#0f1930; border:1px solid #2a3b58; border-radius:10px; padding:12px; }
  .kpi b { display:block; font-size:1.25rem; color:var(--acc); }
  .kpi span { font-size:.75rem; color:var(--mut); }
  .sum { background:#0f1930; border-left:3px solid var(--acc2); border-radius:8px; padding:14px; margin:12px 0; white-space:pre-wrap; }
  table { width:100%; border-collapse:collapse; font-size:.9rem; }
  td, th { padding:7px 10px; border-bottom:1px solid #22304c; text-align:left; }
  .msg { background:#0f1930; border-radius:10px; padding:10px 14px; margin:8px 0; }
  .msg.soporte { border-left:3px solid var(--acc); }
  .msg.cliente { border-left:3px solid var(--acc2); }
  .drop { border:2px dashed #2a3b58; border-radius:10px; padding:24px; text-align:center; color:var(--mut); cursor:pointer; margin:12px 0; }
  .hero { text-align:center; padding:36px 0 20px; }
  .sub { color:var(--mut); margin:12px auto 0; max-width:560px; }
  .cta { display:flex; gap:10px; justify-content:center; margin:24px 0 8px; flex-wrap:wrap; }
  .foot { text-align:center; color:var(--mut); font-size:.8rem; margin-top:40px; }
</style>
</head>
<body><div class="wrap">__CONTENT__</div></body>
</html>"""

LANDING_BODY = """
  <div class="topbar"><b>ReporteFácil</b><div>__NAV__</div></div>
  <div class="hero">
    <h1>Tu negocio en Excel.<br>Tus decisiones, <span>explicadas cada lunes.</span></h1>
    <div class="sub">Sube tu archivo de ventas y recibe un reporte claro: cuánto vendiste, qué producto jala,
    qué está cayendo — con resumen ejecutivo por IA, en español. Crea tu cuenta y guarda tu historial.</div>
    <div class="cta">
      <input type="email" id="em" placeholder="tu@correo.com">
      <button onclick="sub()">Quiero acceso anticipado</button>
    </div>
    <div class="note" id="submsg">Sin spam. Solo el aviso de lanzamiento.</div>
  </div>
  <div class="card">
    <h3>Pruébalo ahora — gratis, sin registro</h3>
    <div class="note">Sube un .csv o .xlsx con tus ventas. Nada se guarda si no tienes cuenta.</div>
    <div class="drop" id="drop" onclick="document.getElementById('file').click()">
      Arrastra tu archivo aquí o haz click para elegirlo</div>
    <input type="file" id="file" accept=".csv,.xlsx,.xls" style="display:none" onchange="up(this.files[0])">
    <div id="result"></div>
  </div>
  <div class="foot">ReporteFácil · hecho en Ecuador</div>
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
const drop = document.getElementById('drop');
['dragover','dragenter'].forEach(e => drop.addEventListener(e, ev => { ev.preventDefault(); }));
drop.addEventListener('drop', ev => { ev.preventDefault(); up(ev.dataTransfer.files[0]); });
function esc(s){const d=document.createElement('div');d.textContent=s??'';return d.innerHTML;}
function money(n){ return '$' + Number(n).toLocaleString('es-EC', {maximumFractionDigits:2}); }
function kpi(v,l){ return '<div class="kpi"><b>'+v+'</b><span>'+l+'</span></div>'; }
async function up(file) {
  if (!file) return;
  const out = document.getElementById('result');
  out.innerHTML = '<div class="note">Analizando ' + esc(file.name) + '...</div>';
  const fd = new FormData(); fd.append('file', file);
  try {
    const r = await fetch('/report-demo', { method:'POST', body: fd });
    const d = await r.json();
    if (d.error) { out.innerHTML = '<div class="card err">'+esc(d.error)+'</div>'; return; }
    const m = d.metrics;
    let h = '<div class="kpis">';
    if (m.total !== undefined) {
      h += kpi(money(m.total),'ventas totales') + kpi(m.transacciones,'transacciones')
         + kpi(money(m.promedio),'ticket promedio') + kpi(money(m.maximo),'venta máxima');
      if (m.variacion_pct !== undefined) h += kpi(m.variacion_pct+'%','vs semana anterior');
    } else h += kpi(m.filas,'filas leídas');
    h += '</div>';
    if (d.resumen) h += '<div class="sum">'+esc(d.resumen)+'</div>';
    if (m.top_categorias) h += '<table><tr><th>'+esc(m.col_categoria)+'</th><th>Total</th></tr>'
      + m.top_categorias.map(t=>'<tr><td>'+esc(t.nombre)+'</td><td>'+money(t.total)+'</td></tr>').join('') + '</table>';
    h += '<div class="row"><a class="btn" href="/registro">Crear cuenta para guardar reportes</a></div>';
    out.innerHTML = h;
  } catch(e) { out.innerHTML = '<div class="card err">Error: '+esc(String(e))+'</div>'; }
}
</script>"""

AUTH_FORM = """
  <div class="hero"><h2><a href="/">ReporteFácil</a></h2></div>
  <div class="card" style="max-width:420px;margin:0 auto">
    <h3 id="t"></h3>
    <form method="post">
      <input type="email" name="email" placeholder="tu@correo.com" required style="width:100%;margin-bottom:8px">
      <input type="password" name="password" placeholder="Contraseña (mín. 8)" required minlength="8" style="width:100%;margin-bottom:8px">
      <span id="biz"></span>
      <button style="width:100%">Continuar</button>
    </form>
    <div class="note" style="margin-top:10px" id="alt"></div>
  </div>
  <script>
    const mode = "__MODE__";
    document.getElementById('t').textContent = mode === 'registro' ? 'Crear cuenta' : 'Entrar';
    if (mode === 'registro') {
      document.getElementById('biz').innerHTML =
        '<input type="text" name="business" placeholder="Nombre de tu negocio (opcional)" style="width:100%;margin-bottom:8px">';
      document.getElementById('alt').innerHTML = '¿Ya tienes cuenta? <a href="/login">Entra aquí</a>';
    } else {
      document.getElementById('alt').innerHTML = '¿No tienes cuenta? <a href="/registro">Créala aquí</a>';
    }
  </script>"""

ADMIN_LOGIN_FORM = """
  <div class="card" style="max-width:380px;margin:60px auto">
    <h3>Panel de administración</h3>
    <form method="post">
      <input type="password" name="password" placeholder="Contraseña admin" required style="width:100%;margin-bottom:8px">
      <button style="width:100%">Entrar</button>
    </form>
  </div>"""

init_db()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8090"))
    print(f"\n  ReporteFácil portal -> http://localhost:{port}")
    print(f"  Admin -> http://localhost:{port}/admin (contraseña: variable ADMIN_PASSWORD)\n")
    app.run(host="0.0.0.0", port=port, debug=False)
