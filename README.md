# MaaganM API

This service exposes a bearer-protected REST endpoint for the existing allowlisted command dispatcher on local port 8001. Remote access is through `https://ors-macbook-air.taila51d65.ts.net/maaganm-api/`: sign in at `/login`, then use the browser session cookie. The gateway injects its bearer token from `local-api-gateway/.env`; callers do not need the API token. The previous signed Gmail command worker remains in the repository for migration and rollback, but is retired and is not part of the current runtime.

Run `uvicorn http_api:app --host 127.0.0.1 --port 8001` with `BUDGET_USERNAME` and `BUDGET_PASSWORD` configured locally in `.env` or `.env.local`. The gateway supplies the service bearer token from its private `local-api-gateway/.env`. The service uses its local Budget credentials and retains authenticated Budget sessions in memory. The stable gateway URL remains the same after a pause or restart.

## REST caller contract

The remote gateway authenticates browser sessions with its `/login` form and
injects the API bearer token. Direct local API requests use
`Authorization: Bearer <MAAGANM_API_TOKEN>`. Send JSON with these top-level
fields:

```json
{
  "id": "<unique-request-id>",
  "verb": "balance",
  "args": {},
  "approval": null
}
```

`id` may be omitted to generate one, but callers should supply a stable ID for retries. `approval` is optional for reads and required for writes. Do not send Budget credentials in command bodies, arguments, URLs, or logs. The Budget driver uses the locally configured credentials and keeps authenticated sessions in memory.

Completed Budget and help results replay only when both the request ID and locally configured Budget username match. This preserves safe replay of previously scoped requests for the same account. A request ID previously used by another username, or one created before account scoping was added, returns HTTP 409 with `Request ID is unavailable`; use a fresh ID. A retry with the same username can replay after a password change. Keep request IDs unique across those operations because an ID is never re-executed, even if its verb or arguments differ. Help commands use the locally provisioned `HELP_MEMBER_ID`.

## Kehila-Net member reads

Two read-only commands use the locally configured Kehila-Net account and the
same bearer-protected `POST /commands` route:

| Verb | `args` | Result item fields |
|---|---|---|
| `kehilanet.phonebook.search` | `query` (required nonempty string, up to 120 characters), `limit` (optional, 1–50; default 20) | `name`, `phones` (`label`, `number`), `emails` |
| `kehilanet.announcements.list` | `query` (optional string, up to 120 characters; default empty for latest), `limit` (optional, 1–20; default 10), `forum_id` (optional positive decimal category ID), `page` (optional, 1–100; default 1) | `id`, `title`, `date`, `category`, `teaser`, `content`, `links`, `has_image` |
| `kehilanet.announcements.categories` | `{}` | `id`, `name` |

For example, send `{"verb":"kehilanet.announcements.list","args":{"query":"ישיבה","limit":5}}`.
Both return the standard command result with `payload.items` and
`payload.total`. Announcement `content` and HTTPS `links` come from the full
message view, rather than only its teaser. An image-only message has empty
`content` and `has_image: true`; this API does not transcribe its image. The portal
also has some announcements with an empty detail body, which return empty
`content` and `has_image: false`. The portal is read through a local
headless Chrome session because a plain HTTP login did not reliably reach the
member announcements page. The service performs no Kehila-Net writes.

The latest announcements view is capped at 20; the portal ignores `page` there,
so the API rejects `page > 1` without `forum_id`. For older items, call
`kehilanet.announcements.categories`, then call `kehilanet.announcements.list`
with that `forum_id` and successive `page` values. Category pages always
return their full 20-item page so advancing pages does not skip results.
The result also includes `payload.page` and `payload.forum_id`. The portal has
no date-filter control; use each item's `date` while traversing the relevant
category pages. Search uses the portal's `searchTXT` form.

Set `KEHILANET_USERNAME` and `KEHILANET_PASSWORD` only in the owner-only
`.env.local`; they are never accepted in request bodies. Member directory and
announcement results are live reads and are not saved in the command replay
database. If the local credentials are missing, these commands return HTTP
503. Portal errors return safe command errors without exposing member pages or
credentials in logs.

## Retired signed Gmail command worker (reference only)

The following mail protocol, worker configuration, prerequisites, and commands
describe retained migration/rollback functionality. This worker is not started
by the current runtime; use the REST API above for current requests.

### Mail protocol

Commands are sent from **`orgal@mail.instinct.com`** to **`orrgal+agents+maaganm@gmail.com`** with subject:
```text
[CMD] <verb> <request-id>
```

The JSON body has exactly this envelope shape (placeholders are illustrative and are not credentials):

```json
{"id":"<request-id>","verb":"<allowlisted-verb>","args":{},"issued_at":"<UTC ISO-8601>","approval":null,"hmac":"<hex HMAC-SHA256>"}
```

`id` is the request identifier and must agree with the subject. `issued_at` is the command timestamp. `approval` is either `null` or an object containing a nonempty `ref` and a future UTC `expires_at`; write commands require a valid approval object. The result reply uses this shape:

```json
{"id":"<request-id>","status":"<ok|error|approval_required|approval_expired>","payload":null,"error":null,"as_of":"<UTC ISO-8601>"}
```

Only the four result statuses shown above are valid. Error text and logs must never contain credentials, the HMAC secret, or other secrets.

#### HMAC verification

The secret is required at worker startup. To calculate the signature, remove `hmac` from the envelope, sort JSON object keys recursively as applicable to the canonical object, serialize with compact separators `(',', ':')` and `ensure_ascii=False`, then compute HMAC-SHA256. Compare signatures with a constant-time `compare_digest`; reject missing, malformed, or mismatched signatures. Do not place secrets in command or result payloads.

#### Allowlist and argument contracts

The driver mapping and command arguments are:

| Verb | Kind | Driver method | Approval | `args` |
|---|---|---|---|---|
| `health` | read | `check_login_status` | not required | `{}` |
| `balance` | read | `get_balance` | not required | `{}` |
| `recipients.search` | read | `search_recipients` | not required | `query` (string, optional), `transaction_type` (integer, optional; default 1) |
| `transactions.list` | read | `get_transactions` | not required | `from_date` (DD/MM/YYYY, optional), `to_date` (DD/MM/YYYY, optional), `types` (string, optional) |
| `approvals.pending` | read | `get_pending_approvals` | not required | `{}` |
| `authorized_users.list` | read | `get_authorized_users` | not required | `{}` |
| `reports.catalog` | read | `get_report_types` | not required | `{}` |
| `reports.generate` | read | `generate_report` | not required | `report` (slug or numeric ID), `format` (optional; default `json`), `year` (integer, optional), `from_month` (integer, optional), `to_month` (integer, optional), `from_date` (optional), `to_date` (optional) |
| `help.catalog` | read | `get_catalog` | not required | `{}` |
| `help.calls.list` | read | `list_calls` | not required | `{}` |
| `help.call.schedule.options` | read | `get_schedule_options` | not required | exactly `{"call_id":"6457734"}` or `{"call_id":6457734}` |
| `help.call.schedule.replacements` | read | `get_schedule_replacements` | not required | exactly `{"call_id":"6457734"}` or `{"call_id":6457734}` |
| `help.call.schedule.book` | write | `book_schedule` | required | exactly `{"call_id":"6457734","slot_id":"<nonempty string>"}` or the same object with integer `call_id` |
| `help.call.schedule.move` | write | `move_schedule` | required | exactly `{"call_id":"6457734","slot_id":"<nonempty string>"}` or the same object with integer `call_id` |
| `transfer.stage` | write | `transfer` | required | `recipient_hid`, `recipient_name`, `amount_ils`, `details_receiver` (optional), `details_sender` (optional), `transaction_type` (integer, optional; default 1) |
| `transfer.approve` | write | `approve_otp` | required | `transaction_id`, `otp_code` |
| `transaction.delete` | write | `cancel_transaction` | required | `transaction_line_id` |
| `approval.decline` | write | `decline_pending_approval` | required | `transaction_line_id` |
| `authorized_user.set` | write | `set_authorized_user` | required | `user_id`, `user_name`, `is_authorized` (boolean) |
| `help.call.create` | write | `create_call` | required | `category_id`, `description`, `contact_id` (nonempty strings); `details`, `contact_name`, `contact_phone`, `contact_email` (optional strings, default empty; provide custom contact fields when `contact_id` is `0`/a custom contact); `attachments` (optional list, default empty; max 5) |
| `help.call.feedback` | write | `act_on_call` (`complain`) | required | `call_id`, `text` (both nonempty strings) |
| `help.call.expedite` | write | `act_on_call` (`hurryup`) | required | `call_id`, `text` (both nonempty strings) |
| `help.call.close` | write | `act_on_call` (`close`) | required | `call_id` (nonempty string) |
| `help.call.reopen` | write | `act_on_call` (`reopen`) | required | `call_id` (nonempty string) |

For all `help.call.schedule.*` commands, `call_id` accepts a JSON string of 1–32 digits or a nonnegative JSON integer with up to 32 decimal digits, and is normalized to a decimal string. Strings containing whitespace, signs, or non-digits, and booleans, are invalid. `slot_id` remains a nonempty string. `help.catalog` retains its categories and contacts and adds a machine-readable `command_schemas` mapping whose scheduling entries expose the exact JSON Schema for command arguments.

The `reports.catalog` response includes transport-neutral `example_args` objects for each report. Pass one of those objects as the `args` value of a `reports.generate` command; they do not contain Gmail- or CLI-specific fields.
For `help.call.create`, each attachment must be an object with a safe, nonempty `filename`, a safe, nonempty MIME `content_type`, and strictly valid base64 `content_base64`. The attachment list has at most 5 items, and the aggregate decoded attachment content is limited to 4 MiB.

#### Help-call scheduling

Creation and calendar scheduling use a strict create → options → book flow:

1. Send `help.call.create` and wait for its result. Creation never books a
   calendar slot automatically.
2. Send the read command `help.call.schedule.options` with exactly
   `{"call_id":"6457734"}` or `{"call_id":6457734}`. The response contains
   opaque slot IDs; clients choose one without interpreting its contents.
3. Send `help.call.schedule.book` with exactly
   `{"call_id":"6457734","slot_id":"<nonempty string>"}` (or the same object
   with an integer `call_id`). Booking is a separate write and must pass its
   own approval and expiry gate; approval for creation does not approve
   booking. Request-ID idempotency still applies
   to the booking command. An already-booked call is refused by this operation.

To reschedule an existing calendar appointment, use a replacement-slots then
approved-move flow:

1. Send `help.call.schedule.replacements` with exactly
   `{"call_id":"6457734"}` or `{"call_id":6457734}`. This read needs no
   approval. For a calendar call it returns the `call_id`,
   `scheduling:{"mode":"calendar"}`, the current `appointment` (`start`,
   `end`, `status:"scheduled"`), and `replacement_slots` with opaque IDs. The
   response exposes no event IDs or CSRF values; use each opaque `slot_id`
   exactly as returned.
2. Send `help.call.schedule.move` with exactly
   `{"call_id":"6457734","slot_id":"<nonempty string>"}` (or the same object
   with an integer `call_id`) and an unexpired approval. The worker re-fetches
   current state, requires an existing current appointment and portal evidence
   that the appointment is for the same call, posts the same-call update, and
   re-fetches to confirm.
   Success returns the `call_id`, the selected public `appointment`
   (`id`, `start`, `end`, `status:"scheduled"`), and top-level
   `status:"rescheduled"`. Request-ID idempotency and approval expiry apply.
   The move operation does not cancel and create a new call: the observed
   booked-call page provides a same-call POST update control and explicitly
   says that choosing a new time cancels the current time.
3. A non-calendar call is not treated as reschedulable: its response contains
   the `call_id` and its actual coordination mode (`assigned` with a handler,
   or `unknown`) and no `replacement_slots`.

Scheduling metadata is documented only where observed: category `616` yields
`"scheduling":{"mode":"calendar"}`; category `624` yields
`"scheduling":{"mode":"contact","phone":"077-7076023","extension":"2"}`. Every
other category yields `"scheduling":{"mode":"unknown"}`; no other
classification is supported. When creation follows a scheduler redirect, its result includes
`"scheduling":{"mode":"calendar","call_id":"..."}`. A normal creation includes
`"scheduling":{"mode":"assigned","handler":"..."}` only for a nonempty handler
and includes `"scheduling":{"mode":"unknown"}` otherwise.
These modes do not imply that creation booked a slot.

Unknown verbs and unknown/malformed arguments fail closed. Binary report results are encoded as base64 in `payload`, together with `media_type` and `filename`; JSON reports remain structured JSON.

## Approval and processing lifecycle

Writes must carry an approval object with a nonempty reference and a future UTC expiry. Missing approval produces `approval_required`; an expired approval produces `approval_expired`; neither is executed. Approval is bound to the command being processed and is not a replacement for HMAC verification.

Commands are claimed atomically in a persistent SQLite database by request ID. A completed request replays its stored result without executing the driver again. If the process finds a request left in progress after a crash or restart, it records a persistent error and does not re-execute it. This is fail-closed recovery, not best-effort retry.

Pending state is the absence of the configured processed label name (`Agents` by default), independent of whether a message is read or unread. Message metadata are discovered and sorted oldest first; then one full body is fetched and processed at a time. The exact sender and recipient alias are checked in both the discovered metadata and the fetched full message. An exact-sender message to the configured alias with a valid HMAC is replied to, including when validation or dispatch yields a structured protocol error. Messages from any other sender or alias, and messages with a missing, malformed, or invalid HMAC, are ignored with no reply and no relabeling. Result messages use the exact subject `[RESULT] <request-id>` and are sent as new messages without a thread ID. A successful send is required before the source message is labeled with the configured processed label ID; labeling failures must not expose secrets. Silence means the command is still pending; the worker does not send an intermediate acknowledgement and does not require resending a command. Polling and reply failures must not expose secrets.

## Configuration

The worker also uses `MAAGANM_EMAIL_LABEL_NAME` (default `Agents`) to identify pending messages and `MAAGANM_EMAIL_LABEL_ID` (default `Label_35`) to mark a successfully replied-to message as processed. `GAPI_MAX_OUTPUT_BYTES` bounds each captured `gapi` stdout and stderr stream (default `8388608` bytes). The email alias defaults to `orrgal+agents+maaganm@gmail.com` and the sender defaults to `orgal@mail.instinct.com`.

The worker also needs the local budget portal username and password, plus a locally provisioned help-portal member identifier for help operations. Keep those values only in the local environment and never in command bodies, results, logs, or documentation. `HELP_SCHEDULER_BASE_URL` configures the scheduler endpoint and defaults to `https://hh-add.mmm.org.il`; like `HELP_BASE_URL`, a trailing slash is stripped.

When `MOCK_MODE=true`, budget operations retain their documented mock behavior (empty budget credentials are replaced with local mock credentials), but all help operations are unavailable and fail closed through the unavailable-dependency path; no real help driver or member identifier is configured.

## Operational prerequisites

- Python environment with the dependencies in `requirements.txt`.
- `gapi` installed and authenticated for the Gmail account used by the worker; verify the executable is available on `PATH` (or set `GAPI_BIN`). The installed command's search pagination is hidden and cannot be controlled by the worker; discovery therefore fails closed when a search returns 500 rows.
- Gmail label named `Agents` exists and is accessible for pending-state detection, and label ID `Label_35` exists and is accessible for marking processed messages. Configure `MAAGANM_EMAIL_LABEL_NAME` and `MAAGANM_EMAIL_LABEL_ID` when these defaults differ.
- Run only one worker process for a given `MAAGANM_EMAIL_DB_PATH`; the CLI holds a lifetime lock so restart recovery cannot conflict with a live request.
- A provisioned `MAAGANM_EMAIL_HMAC_SECRET` is available at startup.
- Valid local `BUDGET_USERNAME` and `BUDGET_PASSWORD` are available; set `BUDGET_BASE_URL` for the budget portal as needed.
- `HELP_MEMBER_ID` is provisioned locally (leave the template blank); `HELP_BASE_URL` defaults to `https://help.mmm.org.il`, and `HELP_SCHEDULER_BASE_URL` defaults to `https://hh-add.mmm.org.il`.
- Use `MOCK_MODE=true` only for local, non-production operation.

## Running

One polling pass:

```bash
python main.py --once
```

Continuous oldest-first polling (the configured default interval is 30 seconds):

```bash
python main.py
```

Do not include real credentials, secret values, or real HMACs in examples, test fixtures, logs, or issue reports.
