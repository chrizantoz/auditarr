# Auditarr playground

The real Auditarr image next to a fake Sonarr, Radarr, Bazarr, Plex and Whisper that answer for a handful of synthetic video files with real defects
(a French track on an English show, a Japanese episode without subtitles, a movie with no audio, a duplicate, an orphan, a movie Plex never indexed).

Every button works: move to the trash and undo, download a better copy, make AI subtitles, re-time subtitles, detect the language. Nothing is real,
so nothing can go wrong. In playground mode the connections, hooks, passwords and updates are locked, and a reset recreates everything.

    sandbox/run.sh up                                        # plain docker (Unraid too); then open http://localhost:3095
    docker compose -f sandbox/compose.yml up -d --build      # or with compose
    sandbox/run.sh reset                                     # wipe and start again (cron hourly for a public copy)

For a public playground, put it behind your reverse proxy and reset it on the hour (a cron with the two commands above).
