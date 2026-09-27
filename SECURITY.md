# Security notes

AnalogTo21st uses a Home Assistant long-lived access token. Treat `.env` as a secret and never commit it.

The dashboard has no built-in user authentication. Keep it on a trusted LAN, behind Home Assistant, or behind an authenticated reverse proxy. Do not expose the Flask/Gunicorn port directly to the public Internet.

The supplied ESPHome example uses encrypted native API access and password-protected OTA updates. Replace every placeholder in `esphome/secrets.example.yaml` before flashing a device.

If you discover a security issue, contact the repository owner privately rather than opening a public issue with credentials or network details.
