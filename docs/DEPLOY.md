# Deploy en Raspberry Pi

> El hardware (Pi 5 + NVMe, IP fija, uv instalado) ya existe — es el mismo Pi que corre `spotify-liked-sync`. Esta guía asume que ese setup base ya está hecho y solo cubre lo específico de este proyecto.

## Conviviendo con `spotify-liked-sync` en el mismo Pi

Los dos jobs no interfieren entre sí, pero vale la pena tenerlo explícito:

- **Directorios y venvs independientes.** Cada repo vive en su propia carpeta (`~/spotify-liked-sync`, `~/tu-arte-mi-arte`) con su propio `uv sync` — no comparten `.venv` ni dependencias.
- **Estado independiente.** Cada uno tiene su propio SQLite bajo su propio `data/` (o `~/.local/share/...`) — no hay lock contention ni colisión de rutas.
- **Sin conflicto de puertos.** Ninguno de los dos abre un puerto de escucha: `spotify-liked-sync` es un `oneshot` que corre y termina; el bot de Telegram usa *long polling* (conexión saliente), y `samsungtvws` habla con las TVs por su IP en la LAN, no por un puerto propio del Pi.
- **Nombres de unidad distintos**, para no pisarse en `systemctl`/`journalctl`: `spotify-sync.service`/`.timer` vs. `tu-arte-mi-arte.service` (nombres abajo).
- **Carga del Pi:** el trabajo pesado (generación de imágenes, razonamiento del agente) vive en la nube (Gemini); el Pi solo orquesta I/O (red, SQLite, composición de preview). El Pi 5 tiene sobra de margen para correr ambos servicios sin contención de CPU — ver PRD §5, "Principio de reparto".
- **Mismo usuario Linux** que corre `spotify-liked-sync` (`YOUR_USERNAME`) — no hace falta un usuario de servicio dedicado para esto; ambos procesos ya corren con permisos de usuario normal, sin acceso a nada del otro.

## 1. Clonar y preparar el proyecto

```bash
ssh YOUR_USERNAME@<pi-ip>
cd ~
git clone https://github.com/juanroman/tu-arte-mi-arte.git
cd tu-arte-mi-arte
uv sync
```

## 2. Secretos: `.env`

Copiar la plantilla y llenarla en el Pi (nunca se versiona):

```bash
cp .env.example .env
nano .env
```

```bash
GEMINI_API_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_USER_IDS=...
SESSION_INACTIVITY_TIMEOUT_SECONDS=...
```

`TELEGRAM_ALLOWED_USER_IDS` es la lista blanca (§7.1/§9 del PRD) — sin esto el bot rehúsa arrancar (`main()` hace `sys.exit` si falta o viene vacía).

## 3. Datos persistentes

`data/` (sesiones ADK, historial de despliegue a TVs, tokens por TV, imágenes) vive en el repo pero está en `.gitignore` — en el Pi se genera solo al primer arranque. Confirmar que el repo está clonado sobre el NVMe (no la SD), igual que `spotify-liked-sync`, por las escrituras frecuentes de sesión (PRD §11).

## 4. Instalar el systemd service

A diferencia de `spotify-sync` (un `oneshot` + `timer` porque corre y termina cada 30 min), el bot de Telegram es un **proceso de larga duración**: `application.run_polling()` se queda escuchando mensajes indefinidamente. No necesita timer — necesita mantenerse vivo y reiniciarse solo si truena.

Crear `/etc/systemd/system/tu-arte-mi-arte.service`:

```ini
[Unit]
Description=Tu Arte Mi Arte — Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=YOUR_USERNAME
WorkingDirectory=/home/YOUR_USERNAME/tu-arte-mi-arte
ExecStart=/home/YOUR_USERNAME/.local/bin/uv run python -m bot.telegram_bot
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Notas sobre el unit:

- `WorkingDirectory` importa: `load_dotenv()` busca `.env` relativo al directorio de trabajo, y es la carpeta desde donde `uv run` resuelve el proyecto.
- `Restart=on-failure` + `RestartSec=5` cubre tanto un crash como el arranque antes de que la red esté lista — `run_polling()` ya maneja SIGTERM/SIGINT con shutdown ordenado (viene de fábrica en `python-telegram-bot` v20+), así que `systemctl stop/restart` no corta nada a la mitad.
- No hace falta `.timer` — sería incorrecto aquí, este servicio nunca debe "terminar".

Habilitar y arrancar:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now tu-arte-mi-arte.service
```

## 5. Validar

```bash
# Estado del servicio:
systemctl status tu-arte-mi-arte.service

# Logs en vivo:
journalctl -u tu-arte-mi-arte.service -f

# Confirmar que spotify-sync sigue corriendo sin verse afectado:
systemctl status spotify-sync.timer
```

Mandar un mensaje al bot desde Telegram y confirmar respuesta.

## 6. Actualizaciones futuras

```bash
ssh YOUR_USERNAME@<pi-ip> "cd tu-arte-mi-arte && git pull && uv sync && sudo systemctl restart tu-arte-mi-arte.service"
```

`systemctl restart` envía SIGTERM (shutdown ordenado), espera a que termine, y levanta el proceso con el código nuevo. Revisar `journalctl -u tu-arte-mi-arte.service -f` después del restart para confirmar que arrancó limpio.

## Reiniciar el Pi

Con `enable` ya corrido, ambos servicios (`spotify-sync.timer` y `tu-arte-mi-arte.service`) arrancan solos tras un reboot, sin intervención manual. El estado (sesiones ADK, historial de despliegue, tokens por TV) sobrevive porque vive en SQLite sobre el NVMe, no en memoria — ver PRD §11.
