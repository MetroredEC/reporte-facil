# ReporteFácil — Portal de autogestión de clientes

Plataforma completa: el cliente se registra, sube su Excel/CSV de ventas, guarda su historial de reportes (KPIs + resumen con IA), gestiona su plan y abre tickets de soporte. Tú administras todo desde `/admin`.

## Qué incluye

- Landing pública con demo gratuito (sin registro) y captura de correos.
- Registro / login con contraseñas cifradas y sesiones.
- Dashboard del cliente: nuevo reporte + historial guardado, aislado por usuario.
- Plan: gratis vs Pro. El cliente paga con tu link (PAYMENT_LINK), marca "Ya pagué", tú activas desde admin. Sin pasarela automática — a esta escala, la verificación manual (<24 h) es el estándar de los micro-SaaS en etapa 1.
- Soporte: tickets con hilo de mensajes; el cliente reabre al responder.
- Panel admin (`/admin`): leads, usuarios, activar planes, responder y cerrar tickets.

## Correr local

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\setup.ps1     # una vez
.\run.ps1       # abre http://localhost:8090  ·  admin en /admin
```

## Configuración (.env)

```
ANTHROPIC_API_KEY=sk-ant-...   # opcional: resumen ejecutivo con IA
ADMIN_PASSWORD=algo-seguro     # OBLIGATORIO cambiarla (default: cambiame)
SECRET_KEY=cadena-larga-random # producción: firma de sesiones
PAYMENT_LINK=https://paypal.me/tuusuario/29  # o Payphone/Kushki/De Una
PLAN_PRICE=$29/mes
```

## Desplegar público

Ver `DEPLOY.md`. Añade las variables de arriba en el panel del hosting. Nota del plan gratuito de Render: la base SQLite se borra en cada redeploy — exporta usuarios/leads periódicamente o sube a un plan con disco persistente ($7/mes) cuando tengas clientes reales.

## Criterio de éxito (sigue vigente)

2 semanas con URL pública: 20–30 correos o 5 negocios usando el demo con datos reales. El portal completo no cambia esa meta — la facilita.
