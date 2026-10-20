# Torrent Guard v0.3.1

## Bug fix: qBittorrent 5.2+ authentication

- qBittorrent 5.2 and newer can return `204 No Content` (empty response body) when an API login succeeds.
- v0.3 incorrectly required the legacy `200 Ok.` body and logged `qBittorrent refused login: ''` indefinitely.
- Accept the legacy `200 Ok.` and new `204 No Content` login responses; preserve cookie-based authentication, including `QBT_SID_<port>`.
- Verify the newly authenticated session by fetching the protected `/api/v2/app/version` endpoint before allowing torrent inspection or deletion.
- Reject invalid credentials, unexpected empty `200` responses, and sessions that fail verification; add regression tests.
- All previous v0.3 features, file cleanup safeguards, Compose setup, and persistent journal behavior are unchanged.

## Update

```bash
cd torrent-guard
git pull --ff-only
docker compose up -d --build --force-recreate
docker compose logs -f torrent-guard
```

Keep your existing `.env` credentials and `DRY_RUN` preference. No configuration changes required.
