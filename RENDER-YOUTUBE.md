# YouTube on Render

The old AnyDown configuration pointed `127.0.0.1:4416` at a PO-token provider without actually starting that provider. That cannot work on a normal Render Python service.

This version includes a Docker deployment that runs the bgutil provider and FastAPI in the same container.

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
