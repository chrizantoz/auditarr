# Installing Auditarr (first time)

You need: a machine that runs Docker (Unraid, a NAS, a Linux box), Sonarr and/or Radarr already working, and the folder they write media to. Bazarr and Plex are optional but recommended. Ten minutes.

## The one thing to get right: the media path

Auditarr opens your video files, so it must see them **at the same path your arrs use**. Open any series in Sonarr and look at a file path. If it starts with `/data/media/...`, mount your media folder at `/data/media` inside the Auditarr container. If it starts with `/tv/...` and `/movies/...`, mount those two instead. The Setup tab tells you afterwards which folders it can and cannot see.

## Unraid

1. Docker tab → **Add Container** → at the bottom, paste the template URL: `https://raw.githubusercontent.com/chrizantoz/auditarr/main/docs/unraid-template.xml` (or search Community Apps once it is listed).
2. Set **TZ**, check the **Media** path (see above), leave everything else.
3. Apply, then open the Web UI (port 3091).
4. **Setup tab**: paste the address and API key of Sonarr, Radarr, Bazarr, Plex. Press **Test** on each; green means it works. **Save connections**.
5. Step 3 on that page: **Start the first audit**. It opens every file once, about 25 minutes per 10,000 files. The page shows progress. From then on it runs every night at RUN_AT.

Until Auditarr shows up in Community Applications: Docker tab → **Template Repositories** → add `https://github.com/chrizantoz/unraid-templates` → Save, then **Add Container** and pick Auditarr. The template fills in the ports, the config folder and the common settings.

## Docker compose (any Linux / NAS)

```bash
mkdir auditarr && cd auditarr
curl -O https://raw.githubusercontent.com/chrizantoz/auditarr/main/docs/docker-compose.yml
# edit the media line: /path/to/media:/data/media
docker compose up -d
```

Open `http://<host>:3091` and follow the Setup tab as above.

## Where to find the keys

- Sonarr / Radarr / Bazarr: Settings → General → Security → API Key.
- Plex: open any item in Plex Web, the three-dot menu → Get Info → View XML; the address bar ends with `X-Plex-Token=...`. That value is the token.

## Updating

- **Unraid:** Docker tab → check for updates → **update** on Auditarr. Your settings, history and ignore list live in the appdata folder and survive.
- **Docker compose:** `docker compose pull auditarr && docker compose up -d auditarr`.
- **Deploy script** (`docs/auditarr-deploy.sh update`): stages the new image against a copy of your database first and only promotes it if the staged run looks sane.
- **Update now button:** the About tab shows one when the container is started with `UPDATE_HOOK_URL` (plus `UPDATE_HOOK_TOKEN` if the hook wants one). Point it at Watchtower's HTTP API (`http://watchtower:8080/v1/update`) or any webhook that pulls and recreates the container; the app cannot replace itself from inside.
- The page checks GitHub for the newest release once a day and shows an **update available** chip in the header. `UPDATE_CHECK=0` turns that off.

## After the first audit

- Read the **Today** panel at the top of Findings. If it says nothing needs you, you are done for the day.
- Everything Auditarr does by itself at night is in Settings as plain switches. All of it is off or reporting-only until you turn it on, except the safe things (rescans, labelling, default tracks).
- Nothing is deleted. Moved files sit in a trash folder inside your media share (`.media-audit-trash`) with a countdown; the Trash tab puts them back with one click.

The full tour of the page is in [USER-GUIDE.md](USER-GUIDE.md).
