# Message box

`messages` downloads the conversations from the kleinanzeigen message box and
writes one YAML file per conversation, so they can be read, grepped and diffed
like ad files.

```bash
kleinanzeigen-bot messages            # sync every conversation with its history
kleinanzeigen-bot messages --unread   # only fetch history for unread conversations
```

`--unread` still writes a file for every conversation, but skips the detail
request for ones that have nothing new; an already-synced transcript is carried
over rather than overwritten with an empty one. Use it for frequent polling; a
full sync costs one request per conversation.

Fetching a conversation does **not** mark it read server-side (verified against a
live account on 2026-09-25), so syncing is safe to run on a schedule.

## Replying

```bash
kleinanzeigen-bot reply --conversation='5p123:456dfzm:7ptv8t9bk' --text='Ja, noch verfuegbar.'
kleinanzeigen-bot mark-read --conversation='5p123:456dfzm:7ptv8t9bk'
```

Both are publicly visible and irreversible, so the openclaw wrapper refuses them
unless `KA_ALLOW_WRITE=1` is set, exactly like `publish` and `delete`.

The conversation must always be named explicitly. Several conversations routinely
concern the same ad - four buyers on one listing is normal - so there is no
"reply to the newest" shorthand: that is the mistake that sends the wrong person
the wrong message. Take the id from the synced YAML file.

**A reply whose text looks like it contains a phone number is refused, not sent.**
This runs unattended against strangers, so the guard errs towards refusing:
`+49 151 ...`, `0151 ...`, `030/123456` and any unbroken run of nine or more
digits all block the send. Send those through the website instead.

Marking as read is a separate, explicit call - the site never does it implicitly
when the bot reads a conversation, only when a human opens one in the browser.

## Output

Conversations are written to `messages.dir` (default `messages`, resolved inside
the workspace). One file per conversation, named after its id with the colons
replaced, because `:` is not a valid filename character on Windows:

```
messages/conversation_5p123_456dfzm_7ptv8t9bk.yaml
```

```yaml
id: '5p123:456dfzm:7ptv8t9bk'
ad_id: '3210987654'
ad_title: Moulinex Super Uno Fritteuse
role: SELLER          # this account's role
partner: Erika        # the other party
unread: true
unread_count: 2
last_received: '2026-09-25T14:21:39.123000+02:00'
latest_offer:               # most recent price proposal that names an amount
  id: aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee
  kind: BUYER_OFFER_MADE_SELLER_MESSAGE
  offered_price_eur: 175.0
  shipping_cost_eur: 0.99
  seller_total_eur: 175.99
offer_status: SELLER_REJECTED_OFFER_SELLER_MESSAGE   # outcome of the latest offer event
payment:
  paid: true
  payment_id: ...
  carrier: HERMES
  buyer_total_eur: 180.5
  seller_total_eur: 176.99
messages:
  - id: 0f8c5a7e-1111-4444-8888-abcdefabcdef
    direction: INBOUND        # INBOUND from the other party, OUTBOUND from this account
    received: '2026-09-25T10:00:00+02:00'
    text: Ist das noch verfuegbar?
  - id: 1a2b3c4d-...
    kind: PAYMENT_AND_SHIPPING_MESSAGE     # only present for platform payment events
    offer:
      kind: BUYER_OFFER_MADE_SELLER_MESSAGE
      offered_price_eur: 150.0
```

## Offers and payment state

Price proposals are not chat messages. They arrive in the same `messages` array as
`PAYMENT_AND_SHIPPING_MESSAGE` entries, with no `boundness` and no `unichatMessage`,
carrying integer cent amounts that are exposed here in euros.

The negotiation shows up as a sequence of these:
`BUYER_OFFER_MADE_SELLER_MESSAGE` → `SELLER_OFFER_MADE_SELLER_MESSAGE` (a counter)
→ `BUYER_REJECTED_OFFER_SELLER_MESSAGE` / `SELLER_REJECTED_OFFER_SELLER_MESSAGE`.
Accepting one offer makes kleinanzeigen auto-reject the rest.

`latest_offer` deliberately skips acceptance and rejection events: those are
offer-shaped but carry only the offer id, so treating them as offers reports a
negotiated item as having no price.

For payment, `paymentStateSummary` *empties* once a sale completes rather than
saying so. The reliable signal is `paymentId` appearing in `paymentAnalyticsData`,
which is what `payment.paid` keys on. `ad_status` flips to `PAUSED` by itself once
the item sells - no need to pause or delete the ad.

Accepting an offer is **not** implemented. It is a `PUT` to a separate payment
service (`/pay/api/users/{userId}/payment-and-shipping/negotiation/{id}/accept`)
that commits real money, and it stays a human action.

Messages are ordered oldest first, so a file reads as a transcript. The API
returns them newest first.

## How it talks to the site

The message box is not served by the same API as ads. It lives on
`gateway.kleinanzeigen.de` and requires a bearer token, where the ads API is
happy with session cookies alone.

The token is handed out by a cookie-authenticated endpoint on the main site, and
arrives in a **response header** rather than the body:

```
GET https://www.kleinanzeigen.de/m-access-token.json
-> authorization: Bearer <token>       (the body only carries {"expiration": ...})

GET https://gateway.kleinanzeigen.de/messagebox/api/users/{userId}/conversations?page=N&size=M
   Authorization: Bearer <token>
```

`{userId}` comes from `https://www.kleinanzeigen.de/messagebox-api/view`.

Both requests go through `WebScrapingMixin.web_request`, i.e. as `fetch()` inside
the logged-in page, so cookies are attached and the traffic looks like the site's
own. The conversation list paginates via `_meta.numFound` / `pageSize`, the same
idiom `published_ads.py` uses for the ads API.

Writes are plain REST on the same gateway, answered with `204` and no body:

```
POST /messagebox/api/users/{userId}/conversations/{conversationId}?warnPhoneNumber=true
     {"message": "..."}

POST /messagebox/api/users/{userId}/conversations/read?ids={conversationId}
```

No CSRF token is involved - the `csrfToken` in `messagebox-api/view` is not used
by these calls - and the bearer token is the same one the reads use. Despite the
`unichatMessage` structure in the payloads, sending is not a websocket.

## Rediscovering the API

When kleinanzeigen changes the message box, `messages-probe` records what the page
actually does:

```bash
kleinanzeigen-bot messages-probe --conversations=3
```

It drives a logged-in session through the overview and some conversations while
capturing every XHR/fetch, then writes a report to the diagnostics directory. The
report is redacted by default: key names, types and format hints survive, message
text and names do not, so it is safe to share. `--include-raw-bodies` adds the
unredacted payloads — those are private correspondence and should stay local.

The probe never signs in when no password is configured; it reuses the session the
browser profile already holds and refuses with a clear message if there is none.
Submitting a placeholder password would be a failed login attempt against a live
account.

## Known gaps

- **Attachments** cannot be sent; the API takes a plain `{"message": "..."}` body
  and attachment support was never captured.
- **Bulk mark-read** is possible at the API level (`?ids=` takes a comma separated
  list) but the CLI deliberately exposes one conversation at a time.
- **`mark-read` has not been exercised against the live API.** Its endpoint comes
  from the same capture as `reply`, which is verified, but the call itself has only
  been observed, never made by the bot.
- **The probe records `XHR`, `Fetch` and `Document`.** Anything the page pulls over
  a websocket is still invisible to it.
