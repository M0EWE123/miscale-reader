# miscale-reader

Liest eine Xiaomi Mi (Body Composition) Scale passiv per BLE aus und postet
stabile Messungen an ein Webhook (PWA-Backend). Laeuft als Docker-Container auf
einem Raspberry Pi, verwaltet ueber Portainer.

## Dateien
- `scale_reader.py` — der BLE-Reader
- `requirements.txt` — Python-Abhaengigkeiten
- `Dockerfile` — baut das Image
- `docker-compose.yml` — fuer Portainer (benanntes Volume, ENV aus Portainer-UI)

## Deploy ueber Portainer (Git-Stack)
1. Diese vier Dateien in ein Git-Repo legen (public reicht — es sind keine
   Geheimnisse enthalten, Token kommt als ENV in Portainer).
2. Portainer -> **Stacks** -> **Add stack**
   - Name: `miscale`
   - Build method: **Repository**
   - Repository URL: URL deines Repos
   - Repository reference: `refs/heads/main`
   - Compose path: `docker-compose.yml`
3. **Environment variables** setzen:
   - `SCALE_MAC` — erstmal leer lassen (MAC in Schritt 5 finden)
   - `WEBHOOK_URL` — Backend-Endpoint, oder leer (Messungen werden im Volume
     gepuffert und nachgeliefert, sobald die URL gesetzt ist)
   - `WEBHOOK_TOKEN` — dein geheimes Token
4. **Deploy the stack** (erstes Build dauert 1-2 Min).
5. **Containers** -> `miscale-reader` -> **Logs**. Auf die Waage steigen ->
   Log zeigt `Mi-Waage gesehen: MAC=...`. Diese MAC bei `SCALE_MAC` eintragen
   (Stack -> **Update the stack**). Alternativ MAC aus Zepp Life:
   Profil › Meine Geraete › Waage › Bluetooth-Adresse.

Spaetere Aenderungen: ins Repo pushen -> Portainer Stack -> **Pull and redeploy**.

## Host-Voraussetzungen (Pi, einmalig)
- Bluetooth aktiv: `sudo systemctl enable --now bluetooth`
- Findet der Scan nichts (D-Bus-/BlueZ-Fehler im Log): in
  `docker-compose.yml` die Zeile `privileged: true` einkommentieren, pushen,
  in Portainer "Pull and redeploy".

## Konfiguration (ENV)
| Variable         | Default              | Zweck |
|------------------|----------------------|-------|
| `SCALE_MAC`      | (leer)               | Nur diese Waage verarbeiten; leer = alle loggen |
| `WEBHOOK_URL`    | (leer)               | Ziel-Endpoint; leer = nur lokal puffern |
| `WEBHOOK_TOKEN`  | (leer)               | Bearer-Token fuer den POST |
| `DB_PATH`        | `/data/readings.db`  | SQLite-Pfad (Volume) |
| `EMIT_COOLDOWN_S`| `30`                 | Dauer-Broadcasts derselben Messung unterdruecken |
| `WEIGHT_EPS`     | `0.3`                | kg-Aenderung, ab der eine Messung als neu gilt |
| `RETRY_INTERVAL_S`| `60`                | Intervall fuer Nachlieferung offener Messungen |
| `LOG_LEVEL`      | `INFO`               | Log-Level |
