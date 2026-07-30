# ReporteFácil — cómo desplegarlo (10 minutos, $0)

El MVP ya funciona local (`setup.ps1` → `run.ps1` → http://localhost:8090). Pero para validar necesitas una URL pública que puedas compartir con negocios reales. Ruta gratuita:

## Opción recomendada: Render (plan gratuito)

1. Crea cuenta gratis en https://render.com (con tu Google o GitHub). Esto solo puedes hacerlo tú.
2. Sube esta carpeta a un repositorio de GitHub (si no tienes cuenta, créala en https://github.com):
   - En GitHub: New repository → nombre `reporte-facil` → crear.
   - En PowerShell dentro de esta carpeta:
     ```powershell
     git init
     git add app.py requirements.txt Procfile README.md
     git commit -m "MVP ReporteFacil"
     git remote add origin https://github.com/TU_USUARIO/reporte-facil.git
     git push -u origin main
     ```
3. En Render: New → Web Service → conecta el repo `reporte-facil`.
   - Runtime: Python. Build: `pip install -r requirements.txt`. Start: `gunicorn app:app --bind 0.0.0.0:$PORT`.
   - Instance type: **Free**.
4. En Environment agrega las variables: `ADMIN_PASSWORD` (obligatoria — protege tu panel), `SECRET_KEY` (cadena larga aleatoria), `PAYMENT_LINK` y `PLAN_PRICE` (tu link y precio), y opcionalmente `ANTHROPIC_API_KEY` para el resumen con IA. Ojo: demo público + API key = cualquiera puede gastarte crédito; para validación inicial déjala fuera, los KPIs funcionan igual.
5. Render te da una URL tipo `https://reporte-facil.onrender.com`. Esa es la que compartes.

Limitación del plan gratuito: el servidor "duerme" tras 15 min sin tráfico y la base SQLite de correos se borra en cada redeploy. Para validación está bien; anota los correos de `/leads` cada pocos días (visita `TU_URL/leads`).

## Qué hacer con la URL (esto ES la validación)

Compártela en 2 semanas con: grupos de Facebook/WhatsApp de comerciantes de tu ciudad, 10–20 negocios que conozcas directamente, y tus futuros clientes de Upwork. Meta: 20–30 correos o 5 negocios que usen el demo con SUS datos. Si no llegas, el candidato #2 muere y no perdiste nada — pasas al #1 o a lo que Upwork te revele.

## Si quieres, lo hacemos juntos

Cuando tengas las cuentas de GitHub y Render creadas, dime y te guío paso a paso — o lo ejecuto contigo en tu navegador pidiéndote confirmación en cada paso importante.
