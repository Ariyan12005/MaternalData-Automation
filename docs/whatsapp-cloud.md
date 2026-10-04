# WhatsApp Cloud API adapter

**Status:** implemented and tested against a local fake of the Graph API (`tests/test_whatsapp_cloud.py`). Partly run against Meta on 2026-10-03/04: webhook verification, a signed dashboard test webhook, and real media upload and download, with the inbound message simulated. **No real WhatsApp message has been received or sent**; see [Observed against Meta](#observed-against-meta-2026-10-0304-unpublished-app-meta-test-number). The browser simulator remains the default and is unchanged.

Code: `dayone/whatsapp.py` (adapter, Graph client, CLI), `dayone/server.py` (webhook-only listener, cloud-mode startup), `dayone/store.py` (two job tables, one trigger, one sender column).

## Modes

| Mode | Started with | Inbound | Outbound |
|------|--------------|---------|----------|
| `simulator` (default) | `python -m dayone` | Browser phone panel → `POST /api/whatsapp/messages` | Stored and shown in the simulated thread; never sent |
| `cloud` | `python -m dayone --env-file .env --whatsapp-mode cloud` | Meta webhook → separate listener on `127.0.0.1:8001` | Sent through the Cloud API, only to senders registered with the `WHATSAPP` channel |

Both modes can run together: the simulator keeps working for the seeded demo number, while real numbers use the Cloud API. A simulator sender is never sent a real message, because outbound jobs are only created for `WHATSAPP` senders.

## What happens to a photo

1. **Webhook (inside the HTTP request, no network calls).**
   1. Read exactly `Content-Length` bytes; refuse anything over 3 MB.
   2. Check `X-Hub-Signature-256` (HMAC-SHA256 of the raw bytes with the app secret, constant-time compare). This happens before any JSON parsing; a missing or wrong signature gets `401`.
   3. Parse the batch (every `entry`, every `change`). Only `field = messages` changes for our `phone_number_id` are used.
   4. Write one row per incoming message to `whatsapp_inbound`, keyed by the WhatsApp message ID (`wamid`):
      - an image from a registered number → `PENDING`;
      - any other message type from a registered number → `IGNORED`;
      - any message from an unregistered number → `REJECTED`, with neither the number nor the media ID stored.
   5. Apply status events (`sent` / `delivered` / `read` / `failed`) to `whatsapp_outbound`.
   6. Return `200` only after that transaction commits. A database error returns `500`, so Meta retries; redeliveries are dropped by `wamid`.
2. **Worker (every second, outside the request).**
   1. `GET /{version}/{media-id}?phone_number_id=…` with the Bearer token. Then download the returned URL with the token. Both calls use a timeout, and the download read is bounded.
   2. Accept only `image/jpeg` / `image/png` whose bytes start with the matching file signature, within the size limit, and whose SHA-256 matches the webhook's `image.sha256`.
   3. Write the file atomically as `<sha256>.jpg|png` in `WHATSAPP_MEDIA_DIR`. The name never comes from the media ID or a caption.
   4. Call the existing `DayOneService.ingest_photo(sender_id, message_id=wamid, media_ref="whatsapp-media/<file>")`. That transaction persists the page and creates the usual *« Reçu : page N. Merci, le traitement est en cours. »* message. So the acknowledgment only exists once the photo and the page are stored.
   5. From there it is the normal workflow: grouping window, extraction queue, back-office review, retake, confirmation. The fixture extractor has no fixture for real photos, so these documents go to `PROCESSING_FAILED` and then **manual entry**, exactly like any unknown page set today.
3. **Outbound.**
   1. A SQLite trigger copies every `OUT` message for a `WHATSAPP` sender into `whatsapp_outbound`, in the same transaction as the message. The service code is unchanged.
   2. The worker sends it as a text message. A retake request quotes the original photo (`context.message_id` = the page's `wamid`).
   3. The returned `wamid` is stored, so status webhooks and replies can be matched.
4. **Retake replies.** If an incoming photo quotes (replies to) our retake request, the photo becomes that request's replacement. Otherwise it is ingested as a normal new page, and the request stays pending. A photo quoting a request that was already closed is also kept as a normal page rather than dropped.

## Configuration

Copy `.env.example` to `.env` (git-ignored). Variables already set in the environment take precedence over the file.

| Variable | Required in cloud mode | Meaning |
|----------|------------------------|---------|
| `DAYONE_WHATSAPP_MODE` | no | `simulator` (default) or `cloud`; `--whatsapp-mode` overrides it |
| `WHATSAPP_VERIFY_TOKEN` | yes | Random string you choose; must equal the dashboard's *Verify token* |
| `WHATSAPP_APP_SECRET` | yes | App secret; signs webhook payloads |
| `WHATSAPP_ACCESS_TOKEN` | yes | System user token (permanent) or the temporary API Setup token |
| `WHATSAPP_PHONE_NUMBER_ID` | yes | Numeric phone number ID from API Setup (not the phone number) |
| `WHATSAPP_API_VERSION` | yes | Graph API version, `vNN.N`. Not defaulted in code; `.env.example` uses `v26.0`, the latest version in Meta's changelog on 2026-10-03 |
| `WHATSAPP_GRAPH_BASE_URL` | no | Default `https://graph.facebook.com`. `http` is accepted only for `127.0.0.1` (tests) |
| `WHATSAPP_MEDIA_DIR` | no | Default `var/media/whatsapp` (relative paths are relative to the repo) |
| `WHATSAPP_MAX_MEDIA_BYTES` | no | Default 5 242 880. Meta documents a 5 MB limit for JPEG/PNG images |
| `WHATSAPP_HTTP_TIMEOUT_SECONDS` | no | Default 10. Per-request socket timeout for Graph calls |

Check a configuration without printing any secret:

```powershell
python -m dayone.whatsapp --env-file .env check-config
```

## Webhook-only exposure

The back-office and the demo/admin routes (`/api/*`, including `POST /api/demo/reset`, and `/media/`) have **no authentication**. They must never be reachable from the internet.

- **Back-office:** `127.0.0.1:8000`. Cloud mode refuses to start if `--host` is not a loopback address.
- **Webhook listener:** a separate server, `127.0.0.1:8001` by default (`--webhook-host`, `--webhook-port`). It answers only `GET` and `POST /webhooks/whatsapp`; every other path returns `404`. The back-office server has no webhook route.
- **Tunnel:** point it at the webhook port only, for example:

  ```powershell
  cloudflared tunnel --url http://127.0.0.1:8001
  # or: ngrok http 127.0.0.1:8001
  ```

  Never tunnel port 8000.
- **For a longer-lived setup:** a reverse proxy with a real certificate (Caddy/nginx) that forwards only `location = /webhooks/whatsapp` to `127.0.0.1:8001`. Meta also supports mTLS for webhooks, which the proxy can enforce.

Meta requires HTTPS with a valid certificate (self-signed certificates are not supported). The tunnel or proxy provides it; the listener itself speaks plain HTTP on loopback.

## External setup (you must do this; nothing here is automated)

1. **Meta app.** In the Meta App Dashboard, create an app with the *Connect with customers through WhatsApp* use case and select or create a business portfolio.
2. **Test number.** Open *Use cases → Customize → API Setup* and connect a WhatsApp Business Account (Meta calls it a Messaging account).
   1. Note the **phone number ID** of the test *From* number.
   2. Add your own phone as a *To* recipient and confirm it.
   3. Send the dashboard's test message.
   4. Reply from your phone. That reply opens the 24-hour customer service window during which free-text messages, such as our acknowledgments, are allowed.
3. **Credentials.**
   - **Access token:** *Business Settings → System users*.
     1. Create a system user.
     2. Assign the app (*Manage app*) and the WhatsApp account.
     3. Generate a token with `whatsapp_business_messaging` and `whatsapp_business_management` (Meta's guide also lists `business_management`).

     The temporary token from API Setup works for a first test but expires quickly.
   - **App secret:** *App settings → Basic → App secret*.
   - **Verify token:** any long random string, for example `python -c "import secrets; print(secrets.token_urlsafe(32))"`.

   Put all values in `.env` only, and run `check-config`.
4. **Register the midwife's number** (repeat after **Réinitialiser la démo**, which wipes senders):

   ```powershell
   python -m dayone.whatsapp --env-file .env add-sender --phone +2126XXXXXXXX --label "Sage-femme – C/S Sidi Smail"
   ```

5. **Start** with `python -m dayone --env-file .env --whatsapp-mode cloud`, then start the tunnel to port 8001. The public **callback URL** is `https://<tunnel-host>/webhooks/whatsapp`.
6. **Webhook subscription.**
   1. Open *App Dashboard → WhatsApp → Configuration* (or *Use cases → Customize → Configuration*).
   2. Enter the callback URL and the verify token, then *Verify and save*. Meta sends the `GET` challenge, and the log shows `webhook GET /webhooks/whatsapp -> 200`.
   3. Subscribe to the **`messages`** field.
7. **Test with specimen images only** (see Privacy):
   1. Send a photo of a specimen page from the registered phone.
   2. Expect *« Reçu : page 1… »* on the phone, a new document in the back-office queue, and rows in `GET /api/whatsapp/deliveries`.
   3. Request a retake in the back-office, then on the phone **reply** to the retake message (long-press → *Répondre*) with a new photo.
8. **Duplicate-delivery check (optional).** The tunnel keeps pointing at port 8001.
   1. Restart DayOne with `--webhook-port 8003`.
   2. Run `python tools/webhook_relay.py --fail-first 1`, which listens on `127.0.0.1:8001`. It forwards only `/webhooks/whatsapp`, unchanged, to the listener.
   3. Send a photo. The relay answers Meta `503` once, after the listener has stored the event, so Meta redelivers the same signed payload.
   4. The listener log then shows `'duplicates': 1`, with no second page or acknowledgment.
9. **Receive-only runs.** `--hold-outbound` queues outgoing messages but never sends them. The hold is written to the database, so it holds across restarts and mode changes:
   - cloud mode refuses to start on that database without `--hold-outbound`;
   - the worker sends nothing while the hold is set.

   `python -m dayone.whatsapp --env-file .env discard-held --db <path>` marks every queued message `FAILED` (`DISCARDED_HELD`), never sent, and lifts the hold for future messages. There is deliberately no command that releases held messages. **Réinitialiser la démo** deletes them along with the hold.
10. **If no webhook arrives:**
   - Send a test payload from the Configuration panel.
   - Unpublished apps receive only dashboard test webhooks. Real messages need a published app, which requires business verification.
   - Check, read-only, that your app is subscribed to the WABA: `GET /{waba-id}/subscribed_apps` must list it. Subscribing is `POST /{waba-id}/subscribed_apps`.
   - Check that the tunnel points at port 8001.

## Privacy and security

- **Third parties.** With the Cloud API, photos pass through and are stored by Meta: webhook media stay downloadable for 7 days. A tunnel provider also terminates TLS and sees the traffic. The consignes forbid sending real patient data to a third-party service, so **use specimen or synthetic pages only** until the organizers confirm in writing.
- **At rest.** Downloaded photos are stored **unencrypted** in `var/media/whatsapp/`, like the SQLite database (blocker 3 in `tasks.md`).
- **Logs** contain internal IDs (`WIN-…`, outbound message IDs, `PAGE-…`), counts and error codes only. They never contain tokens, signatures, phone numbers, message text, media URLs or image bytes. The webhook listener logs paths without query strings, because `hub.verify_token` travels in the query.
- **Token handling.** The token is sent only to the configured Graph base URL and to the media URL returned by it, which must be HTTPS. It is removed if a redirect leaves the original host, and an HTTPS → HTTP redirect is refused.
- **Unregistered numbers.** Their media are never downloaded and their numbers are not stored.

## Retries and limits

| Situation | Behaviour |
|-----------|-----------|
| Webhook redelivered (Meta retries up to 7 days) | Dropped by `wamid` (`whatsapp_inbound` unique key, then `pages.source_message_id`) |
| Media metadata or download: network error, timeout, HTTP 5xx/429, download 404 (expired URL), retryable Graph code, SHA-256 mismatch | Job stays `PENDING`, retried with backoff (30 s doubling, capped at 1 h, 10 attempts). No page and no acknowledgment until it succeeds |
| Media too large, not JPEG/PNG, wrong file signature, invalid media ID | `FAILED` at once. The midwife gets *« Nous n'avons pas pu recevoir votre photo. Merci de la renvoyer. »* |
| Send: network error, timeout, HTTP 5xx/429, retryable Graph code | Stays `PENDING` with backoff (same schedule). The document, draft and review are untouched |
| Send: other errors, e.g. `131047` (more than 24 h since the midwife's last message) | `FAILED`, not retried |
| Status webhook `failed` with a retryable code | Re-queued; otherwise `FAILED` |
| Crash between storing the page and closing the job | Re-download on the next tick; `ingest_photo` returns the existing page; no second acknowledgment |

Retryable Graph codes: `0`, `2`, `4`, `190`, `80007`, `130429`, `131000`, `131016`, `131056`, `131057`, `133004`, `2494100`. These are the codes Meta's error reference describes as temporary, plus token errors (`0`, `190`), which recover once an admin renews the token.

**Sending is at-least-once.** If Meta accepts a message but the response is lost (timeout), the retry can deliver it twice.

## What works locally and what still needs the sandbox

### Verified by automated tests (mocked Meta, no credentials)

- verify-token challenge;
- signature on raw bytes before parsing;
- batched webhooks;
- image versus status versus other messages;
- other phone number IDs skipped;
- authenticated two-step media download;
- size bound with lying metadata;
- MIME and file-signature checks;
- SHA-256 check;
- timeout;
- safe filenames;
- token dropped on a cross-host redirect;
- deduplication across redelivery and crash replay;
- acknowledgment only after the page is stored;
- outbound retry and backoff without touching review;
- permanent failures;
- status progression;
- retake request quoting the photo;
- reply-to-retake replacement;
- simulator senders never queued;
- webhook listener serving nothing else;
- logs free of secrets, numbers and image data;
- held outgoing messages never sent, across restarts, until `discard-held`;
- the hybrid tool's signed payload accepted by the adapter, and the tool refusing unheld databases and non-loopback URLs.

### Observed against Meta (2026-10-03/04, unpublished app, Meta test number)

**No real WhatsApp message was received or sent.** Only the following actually involved Meta.

**1. Sent by Meta to DayOne (real, through the HTTPS tunnel):**

- **Exposure:** the listener answers only `/webhooks/whatsapp`. Every other path, including `/api/demo/reset`, returns `404`.
- **Verification:** Meta's GET verification returned `200` once the callback URL and verify token were saved.
- **Dashboard test webhook:** a `messages` "Incoming Message" sample arrived signed, and passed the signature check with the real app secret. It was skipped because its sample `phone_number_id` (`123456123`) is not ours.
- **Unpublished app:** the dashboard says "Apps will only be able to receive test webhooks sent from the dashboard while the app is unpublished." Publishing requires business verification, which we don't have. A "Bonjour" sent twice from the registered phone never reached the listener.

**2. Read-only configuration check (Graph API `GET` only, 2026-10-04 01:10 UTC):**

| Check | Result |
|-------|--------|
| `GET /debug_token` | Valid user token, scopes `whatsapp_business_management` and `whatsapp_business_messaging`, both limited to WABA …3246. Temporary: expires 2026-10-04 02:00 UTC |
| `GET /{phone-number-id}` | `platform_type` `CLOUD_API`, `status` `CONNECTED`, quality `GREEN`. Listed under WABA …3246 (`GET /{waba-id}/phone_numbers`) |
| `GET /{app-id}/subscriptions` (app token) | Object `whatsapp_business_account`, active, callback = the tunnel's `/webhooks/whatsapp`, field `messages` v26.0. There is also a leftover `user` object subscription with no fields; harmless |
| `GET /{waba-id}/subscribed_apps` | Only "WA DevX Webhook Events 1P App" (Meta's own). **Our app (…0876) is not subscribed to the WABA** |

So **publishing is not the only blocker for real inbound messages.** The WABA must also have our app subscribed, which takes `POST /{waba-id}/subscribed_apps` with the access token. That is a configuration write; it was not made. Given the dashboard banner, subscribing alone is not expected to deliver real messages while the app is unpublished, but this was not tried.

**3. Hybrid run (2026-10-04 01:02–01:04 UTC)**

The setup was:

- Meta's media API was called for real.
- The inbound webhook was **simulated locally**.
- DayOne ran with outgoing messages held, on a separate database (`var/sandbox-hybrid.sqlite3`).
- Synthetic specimen pages `dossiers_specimen_10_patientes-01.png` and `-03.png` were used.

The calls were made by hand; [`tools/hybrid_media_test.py`](../tools/hybrid_media_test.py) now repeats them (see [demo-script.md](./demo-script.md#part-c-optional-hybrid-media-test-real-meta--simulated-webhook--local)).

| # | Stage | Source | Evidence |
|---|-------|--------|----------|
| 1 | Upload | **Real Meta** | `POST /{phone-number-id}/media` returned media IDs …6555 (-01) and …4926 (-03) |
| 2 | Media metadata | **Real Meta** | `GET /{media-id}?phone_number_id=…` returned `200`, `image/png`, exact `file_size`, URL on `lookaside.fbsbx.com`. `sha256` is **hex** here; the webhook reference shows base64, and `_sha256_matches` accepts both |
| 3 | Photo receipt | **Simulated webhook** | Payload in Meta's documented shape, sender = the registered test number, `sha256` in base64, signed with the real app secret, posted to `127.0.0.1:8011`, answered `200` |
| 4 | Durable ingest | Local | `whatsapp_inbound` `WIN-000001` (01:02:23) and `WIN-000002` (01:03:41) committed before the `200`, then `DONE` after 1 attempt each |
| 5 | Download | **Real Meta**, called by DayOne's worker | Metadata and authenticated download through the Graph API. Stored files' SHA-256 equal the specimens': -01 `4434209e…`, -03 `653fbc54…` |
| 6 | Page | Local | `PAGE-000005` → `DOC-000003` (01:02:25). `PAGE-000006` → `DOC-000004` (01:03:43): sent 78 s later, outside the 8 s grouping window |
| 7 | Acknowledgment | Local, **held** | Messages 10 and 12, « Reçu : page 1. Merci, le traitement est en cours. », queued in the same transactions as the pages. `whatsapp_outbound`: `PENDING`, 0 attempts, no `wamid` |
| 8 | Extraction | Local | `NO_FIXTURE_FOR_PAGE_SET` → `PROCESSING_FAILED` (01:02:34) |
| 9 | Regroup | Local | `PAGES_REGROUPED`: `PAGE-000006` moved from `DOC-000004` to `DOC-000003` (01:04:13); extraction failed again, as expected |
| 10 | Manual entry and review | Local | `MANUAL_ENTRY_STARTED`, then 12 `FIELD_REVIEWED` events (10 corrections, 2 set to `NOT_PROVIDED`) → `VALIDATED`. Reviewer "Agent sandbox" |
| 11 | Patient choice | Local | `PATIENT_SELECTED` `NEW` → `PATIENT_MATCHED` |
| 12 | Confirmation | Local | `REGISTERED` (01:04:27): `PAT-000002` created, `VIS-000004` (antenatal, 2025-07-20, slot `MANUAL`, source `DOC-000003`) |
| 13 | Timeline | Local | `GET /api/patients/PAT-000002/timeline` lists `VIS-000004` with the entered values (84 days, 58.8 kg, 109/74, both tests negative); rechecked 01:12 UTC |
| 14 | Duplicate delivery | **Simulated webhook** | The identical signed body was posted again: answered `200`, listener logged `duplicates: 1`. Still one inbound job, one page and one acknowledgment per message |
| 15 | Nothing sent | Local | `var/dayone.sqlite3`: 0 outgoing rows. Hybrid database: 2 `PENDING`, 0 attempts, no `wamid`, rechecked 01:09 UTC after both instances were restarted |

**Synthetic test data.** `PAT-000002` and `VIS-000004` exist only in `var/sandbox-hybrid.sqlite3` (git-ignored). Their values were typed from synthetic specimen page -03, not from a real person. The patient carries the flag `SYNTHETIC_TEST_DATA`, shown in the back-office patient list. Delete that database file to remove them.

**Held messages.** Both sandbox databases are marked held (see step 9 under *External setup*). A restart without `--hold-outbound`, or a switch to cloud mode, cannot send the two queued acknowledgments. Only `discard-held` lifts the hold, and it marks them `FAILED` without sending them.

**Usernames.** The dashboard's test payloads now offer a *Username scenario*, with `username` and `from_user_id` fields. DayOne recognises senders only by phone number (`from`), so a sender whose number is not shared would be rejected as unregistered. This needs a decision before real use.

### Still needs a published app, with our app subscribed to the WABA

- A real inbound photo, the `from` format, and Meta's own redelivery (with `tools/webhook_relay.py --fail-first 1`).
- Sending the acknowledgment and retake request for real. Not run: sending has not been authorised.
- **Whether an image sent as a reply to our retake message carries `context.id` = our message's `wamid`.** The current image-webhook reference documents `context` only for forwarded messages, so the retake mapping depends on behaviour that is not documented there. Without it, replacements arrive as normal pages and the request stays pending.
- The `phone_number_id` media-lookup parameter for media *received* from a user (it works for media uploaded on our number). If it fails, drop the parameter in `GraphClient.fetch_image`.
- The `from` format (with or without `+`). We keep the digits only and match them to the registered number. Meta notes that a user's phone number and WhatsApp ID "may not always match"; confirm that `from` equals the number registered with `add-sender`.
- Error codes seen in practice (`131047`, `131026`, `190`), and the test number's recipient restrictions.
- Page grouping: pages are grouped by ingest time (8 s window). Download delays or retries can split photos sent together into separate documents (the back-office can regroup them).

### Not implemented

- template messages (needed for retake requests outside the 24-hour window);
- images sent as *documents* (they are recorded as `IGNORED`);
- a back-office screen for delivery status (only `GET /api/whatsapp/deliveries`);
- encryption at rest;
- authentication;
- automatic deletion of downloaded media.

## Meta documentation consulted (2026-10-03)

- [Create a webhook endpoint](https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/create-webhook-endpoint): GET verification, `X-Hub-Signature-256`, batching up to 1000 updates, retries for 7 days.
- [WhatsApp webhooks overview](https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/overview): `messages` field, 3 MB payloads, Dev mode note, mTLS.
- [Image messages webhook](https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/reference/messages/image) and [Status messages webhook](https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/reference/messages/status).
- [Media](https://developers.facebook.com/documentation/business-messaging/whatsapp/business-phone-numbers/media): URL valid 5 minutes, token required for download, 404 means re-query, webhook media IDs valid 7 days, 5 MB images.
- [Text messages](https://developers.facebook.com/documentation/business-messaging/whatsapp/messages/text-messages), [Service messages](https://developers.facebook.com/documentation/business-messaging/whatsapp/messages/send-messages) (24-hour window) and [Contextual replies](https://developers.facebook.com/documentation/business-messaging/whatsapp/messages/contextual-replies).
- [Error codes](https://developers.facebook.com/documentation/business-messaging/whatsapp/support/error-codes), [Get started](https://developers.facebook.com/documentation/business-messaging/whatsapp/get-started) and the [Graph API versions](https://developers.facebook.com/docs/graph-api/changelog/versions) list.
