# YouTube on Render

The old AnyDown configuration pointed `127.0.0.1:4416` at a PO-token provider without actually starting that provider. That cannot work on a normal Render Python service.

This version includes a Docker deployment that runs the bgutil provider and FastAPI in the same container.

## YouTube player clients (important)

Older versions hard-coded `mweb,tv,web_embedded`, and YouTube/yt-dlp changes
made every client in that chain fail — which surfaced as "Sign in to confirm
you're not a bot" even though yt-dlp's own defaults would have worked.

The current behavior:

- The default chain is tuned for datacenter IPs (Render et al.), where the
  bot check bites hardest:

  1. `mweb` — yt-dlp's recommended client when an IP is flagged. It needs a
     GVS PO token, which the bundled bgutil provider supplies.
  2. `tv` — needs no PO token and is usually not bot-checked.
  3. yt-dlp's own currently-supported client selection, which tracks
     YouTube's changes with every yt-dlp release (and handles
     cookies/authenticated defaults correctly).

  Each client is only tried when the previous one fails with a known
  YouTube block (bot check, 403, 429, login required, ...).

- `YOUTUBE_CLIENTS` optionally forces a chain (comma-separated), and
  yt-dlp's defaults are always tried last. Example: `YOUTUBE_CLIENTS=mweb,tv`.
- `YOUTUBE_PRIMARY_CLIENT` optionally puts one client first without dropping
  the rest.
- The bgutil PO-token provider stays configured; clients that need PO tokens
  (e.g. mweb) will use it when selected.

Note the mweb attempt only works if the bgutil provider is actually running
and reachable. `/api/health` reports `pot_provider_configured` and
`pot_provider_reachable`; if the provider is down the chain still proceeds
to `tv` and yt-dlp's defaults.

## Render

Use **Docker** for the service. Render will build `Dockerfile` and run `start.sh`.

The container starts:

- bgutil-ytdlp-pot-provider 2.0.0 on `127.0.0.1:4416`
- AnyDown FastAPI on `$PORT`

Do not expose port 4416 publicly.

### Verify the deploy is fresh (do this first)

YouTube fixes only help once they are actually running. Every release of this
app carries a version marker (`APP_VERSION` in `downloader.py`) that shows up
in two places:

- `GET /api/health` → `"app_version": "2.1.0"`
- Every bot-check error message → prefixed with `[AnyDown 2.1.0]`

If you ever see a raw yt-dlp message such as `ERROR: [youtube] ... Use
--cookies-from-browser ...` **without** the `[AnyDown x.y.z]` prefix, the
service is running pre-fix code and no configuration change will help.

To force a fresh image after merging a fix, use Render's **"Manual Deploy →
Clear build cache & deploy"**. Docker layer caching can otherwise keep serving
an old image even after a normal deploy.

## Optional YouTube cookies

If YouTube still requires authentication for particular videos, export a server-side cookies.txt for an account/content you are authorized to access and upload it in Render under **Environment → Secret Files** as `youtube_cookies.txt`.

Set:

`YOUTUBE_COOKIES_FILE=/etc/secrets/youtube_cookies.txt`

Never commit the cookies file or accept cookies through the AnyDown website. Cookies are session credentials.

PO tokens are not a universal bypass: YouTube can still reject an IP/session, and cookies expire/rotate. Keep request rates conservative.

## Diagnosing future failures

YouTube extraction breaks regularly as YouTube and yt-dlp evolve. When it does:

1. Verify the deployed version first (see above): `/api/health` must show the
   latest `app_version` and errors must carry the `[AnyDown x.y.z]` prefix. A
   raw yt-dlp error means the service is running stale code — redeploy with
   cleared build cache before touching anything else.
2. Set `YTDLP_VERBOSE=true` and check the server logs for which client was
   used (each fallback attempt is logged with its client name).
3. Update yt-dlp first (`pip install -U yt-dlp`) — new releases usually fix new
   blocks within days.
4. Check `/api/health`: `yt_dlp` shows the running version,
   `pot_provider_reachable` must be true for the mweb attempt to help, and
   `js_runtime` must be non-null (the Dockerfile provides Node).
5. Only then pin clients via `YOUTUBE_CLIENTS`, and remove the pin once fixed
   upstream.
6. If every attempt still fails with the bot check on a datacenter IP, add an
   authenticated YouTube cookies file (see above) — cookies clear IP-level
   flagging that PO tokens cannot.
