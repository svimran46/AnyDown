# YouTube on Render

The old AnyDown configuration pointed `127.0.0.1:4416` at a PO-token provider without actually starting that provider. That cannot work on a normal Render Python service.

This version includes a Docker deployment that runs the bgutil provider and FastAPI in the same container.

## YouTube player clients (important)

AnyDown no longer forces a fixed list of YouTube player clients. Older versions
hard-coded `mweb,tv,web_embedded`, and YouTube/yt-dlp changes made every client
in that chain fail — which surfaced as "Sign in to confirm you're not a bot"
even though yt-dlp's own defaults would have worked.

The current behavior:

- By default AnyDown defers to yt-dlp's own currently-supported client
  selection, which tracks YouTube's changes with every yt-dlp release.
- `YOUTUBE_CLIENTS` optionally forces a chain (comma-separated), and
  yt-dlp's defaults are always tried last. Example: `YOUTUBE_CLIENTS=mweb,tv`.
- `YOUTUBE_PRIMARY_CLIENT` optionally puts one client first without dropping
  the rest.
- The bgutil PO-token provider stays configured; clients that need PO tokens
  (e.g. mweb) will use it when selected.

## Render

Use **Docker** for the service. Render will build `Dockerfile` and run `start.sh`.

The container starts:

- bgutil-ytdlp-pot-provider 2.0.0 on `127.0.0.1:4416`
- AnyDown FastAPI on `$PORT`

Do not expose port 4416 publicly.

## Optional YouTube cookies

If YouTube still requires authentication for particular videos, export a server-side cookies.txt for an account/content you are authorized to access and upload it in Render under **Environment → Secret Files** as `youtube_cookies.txt`.

Set:

`YOUTUBE_COOKIES_FILE=/etc/secrets/youtube_cookies.txt`

Never commit the cookies file or accept cookies through the AnyDown website. Cookies are session credentials.

PO tokens are not a universal bypass: YouTube can still reject an IP/session, and cookies expire/rotate. Keep request rates conservative.

## Diagnosing future failures

YouTube extraction breaks regularly as YouTube and yt-dlp evolve. When it does:

1. Set `YTDLP_VERBOSE=true` and check the server logs for which client was used.
2. Update yt-dlp first (`pip install -U yt-dlp`) — new releases usually fix new
   blocks within days.
3. Only then pin clients via `YOUTUBE_CLIENTS`, and remove the pin once fixed
   upstream.
