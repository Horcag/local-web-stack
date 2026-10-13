# CAPTCHA recovery
The reader can recover recognized text-image challenges and ordered image-click puzzles using an explicitly configured OpenAI-compatible multimodal endpoint. Browser control stays local. It has no CAPTCHA subscription, token-broker integration or downloaded model weights.

## Configuration
Recovery is disabled by default. Keep credentials in a private operator environment file outside Git. The service reads:

| Variable | Behavior |
| --- | --- |
| LOCAL_WEB_CAPTCHA_ENABLED | Explicit opt-in; true enables recovery. |
| LOCAL_WEB_CAPTCHA_BASE_URL | Required API base URL including /v1 when the gateway requires it. |
| LOCAL_WEB_CAPTCHA_API_KEY | Optional Bearer key; never returned in receipts. |
| LOCAL_WEB_CAPTCHA_API_KEY_FILE | Optional key file readable by the service; an unreadable configured file prevents solving. |
| LOCAL_WEB_CAPTCHA_MODEL | One model; default gemini-3.8-flash-high. Configure a model actually exposed by the chosen gateway. |
| LOCAL_WEB_CAPTCHA_TIMEOUT_SECONDS | Per-call timeout; default 15 seconds, capped at 30. |

The gateway receives only the cropped CAPTCHA image, a separate order-reference image when required, and the bounded recognition/puzzle prompt. Each PNG is limited to 1 MiB, with at most two images per call. Full pages, browser cookies and session storage are not sent. Images and model answers stay in memory; responses expose outcome and attempt count.

For Docker, loopback refers to the container. Verify the API route from the service before enabling solving. Compose can load the private configuration with:

```sh
docker compose --env-file /absolute/private/captcha.env up -d crawl4ai mcp
```

An optional Linux relay profile supports a gateway listening on the Docker host's loopback. A host user-level systemd socket owns a private Unix socket; the container forwards to that mounted socket. This works even when the host cannot bind the requested Docker bridge address. Install an operator-owned copy of service/gateway_relay.py outside the checkout and configure matching units, replacing the absolute paths below:

```ini
# local-web-model-gateway.socket
[Socket]
ListenStream=/absolute/private/model-gateway.sock
SocketMode=0600
RemoveOnStop=true
[Install]
WantedBy=sockets.target

# local-web-model-gateway.service
[Service]
ExecStart=/usr/bin/python3 -B /absolute/private/gateway_relay.py
Environment=LOCAL_WEB_GATEWAY_RELAY_LISTEN_UNIX_PATH=/absolute/private/model-gateway.sock
Environment=LOCAL_WEB_GATEWAY_RELAY_TARGET_HOST=127.0.0.1
Environment=LOCAL_WEB_GATEWAY_RELAY_TARGET_PORT=8045
NoNewPrivileges=true
Restart=on-failure
```

The socket's parent directory must be private and already exist. Enable the socket with systemctl --user enable --now local-web-model-gateway.socket after daemon-reload. Keep the installed relay copy aligned with the deployed revision. The host process accepts only one systemd-activated Unix stream socket for its own PID and the exact configured path; it does not create or unlink socket paths.

Configure the private Compose environment:

```dotenv
LOCAL_WEB_GATEWAY_RELAY_SOCKET=/absolute/private/model-gateway.sock
LOCAL_WEB_GATEWAY_RELAY_UID=1000
LOCAL_WEB_GATEWAY_RELAY_GID=1000
LOCAL_WEB_GATEWAY_RELAY_BIND_HOST=container
LOCAL_WEB_GATEWAY_RELAY_BIND_PORT=18045
LOCAL_WEB_CAPTCHA_BASE_URL=http://gateway-relay:18045/v1
```

Set UID/GID to the socket owner's numeric user/group IDs. The container runs as that user with all capabilities dropped, so a root process cannot accidentally bypass the socket's private permissions. Verify that the host path is a socket with mode 0600 before starting the optional profile:

```sh
docker compose --env-file /absolute/private/captcha.env --profile captcha-gateway up -d
```

The container binds its own private IPv4 when BIND_HOST=container, mounts only the socket, and publishes no host port. A missing host socket fails without creating a directory. The relay preserves gateway authentication and refuses mismatched actual listeners. There is no host-network container or public listener. Both relay stages bound connections, buffers, idle time and shutdown; no gateway payloads are logged.

## Supported behavior
- Text-image forms: a visible CAPTCHA-specific image, CAPTCHA input and submit control inside the same form.
- Yandex checkbox introduction: recovery waits for the recognized .CheckboxCaptcha-Button to become a submit control before clicking it and waiting for the challenge transition.
- Yandex silhouette puzzles: the recognized same-form main image and separate painted order canvas are sent in that order. A tainted canvas is photographed at its native resolution against a temporary opaque background. Only the main image wrapper receives the validated image-relative clicks. Recognized numbered selection markers are temporarily hidden during image capture; the pointer moves outside the image so hovered delete icons cannot obscure their numbers. Existing selections and unexpected marker additions or removals stop recovery.
- Ordered-click puzzles: a raster CAPTCHA image, a recognized instruction element and submit control in the same form. The model returns at most eight distinct integer image-relative coordinates. The browser preserves their order and scales them to measured CSS coordinates.
- Crawl4AI installs recovery before HTML retrieval, while the page is still open. Configured CDP reading uses the same recovery logic on its task-owned tab.

On recognized Yandex gates, a lone Dark Reader fallback stylesheet is removed when no active full theme exists. This repairs hidden challenge details without changing an active theme. CDP attachment preserves the existing browser's default options, including download settings.

Recovery is bounded to two model attempts and 45 seconds. A newly presented recognized Yandex gate after submission shares that same attempt budget, deadline and private image-pair history. Single-image calls use a 1,000-token completion limit; two-image matching uses 4,096 to leave room for the configured model's reasoning and JSON answer. An unchanged rejected challenge is not sent twice. Image identity, URL, instructions and layout are checked before applying points. Both silhouette images must fit the current viewport, and capture-induced scrolling or layout changes invalidate the snapshot. Selection markers can change appearance without invalidating the underlying image identity. The challenge must disappear at the requested host/path/query before the outcome is solved. A model response or submitted form alone is not acceptance.

With recovery enabled, reads bypass Crawl4AI's HTML cache so that verification receipts cannot be lost on a later cached read. Responses report requested cache_mode and effective_cache_mode separately. Interrupted recovery retains the observed challenge flag and stays blocked. Actual final document status is captured during navigation; the initial HTTP status remains available and is not silently replaced with a guessed 200.

Sliders, drag/drop, animated challenges, arbitrary cross-origin iframe puzzles, reCAPTCHA/hCaptcha token flows and unknown controls return unsupported_challenge. This implementation does not promise universal CAPTCHA success. Authentication and paid-access gates remain separate.

The same configured model handles OCR and coordinate planning; errors do not select another model, endpoint or browser backend. Timeout, empty/invalid model output, stale challenge, unsupported form and unexpected navigation produce sanitized outcomes. Unverified navigation remains blocked in both direct reading and research collection.

## Evidence and maintenance
Offline tests validate recognition, request boundaries, coordinate schemas, stale state, retries, source identity and task-owned browser lifecycle. Real-browser fixtures and real gateway calls are separate checks. A fixture success does not prove success on a third-party challenge.

Relevant upstream patterns were reviewed in [hcaptcha-challenger](https://github.com/QIN2DIM/hcaptcha-challenger), [Yandex puzzle solver](https://github.com/yoori/yandex-captcha-puzzle-solver) and [Slidex](https://github.com/dengyie/slidex). Their implementations and dependencies are not bundled. The small gateway/coordinate adapter is independently written; no GPL solver source or prompt is copied.
