# MaaganM Gmail command-bus worker

This worker reads signed commands from a Gmail mailbox and executes the allowlisted operations against both the Maagan Michael budget portal and the help portal at `help.mmm.org.il`. Gmail is the command bus: there is no inbound service endpoint. The worker uses the installed `gapi` command-line tool through subprocess argument lists and stores durable processing state in SQLite.

## Mail protocol

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

### HMAC verification

The secret is required at worker startup. To calculate the signature, remove `hmac` from the envelope, sort JSON object keys recursively as applicable to the canonical object, serialize with compact separators `(',', ':')` and `ensure_ascii=False`, then compute HMAC-SHA256. Compare signatures with a constant-time `compare_digest`; reject missing, malformed, or mismatched signatures. Do not place secrets in command or result payloads.

### Allowlist and argument contracts

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
| `help.call.schedule.options` | read | `get_schedule_options` | not required | exactly `{"call_id":"<nonempty string>"}` |
| `help.call.schedule.book` | write | `book_schedule` | required | exactly `{"call_id":"<nonempty string>","slot_id":"<nonempty string>"}` |
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

The `reports.catalog` response includes transport-neutral `example_args` objects for each report. Pass one of those objects as the `args` value of a `reports.generate` command; they do not contain Gmail- or CLI-specific fields.
For `help.call.create`, each attachment must be an object with a safe, nonempty `filename`, a safe, nonempty MIME `content_type`, and strictly valid base64 `content_base64`. The attachment list has at most 5 items, and the aggregate decoded attachment content is limited to 4 MiB.

### Help-call scheduling

Creation and calendar scheduling use a strict create → options → book flow:

1. Send `help.call.create` and wait for its result. Creation never books a
   calendar slot automatically.
2. Send the read command `help.call.schedule.options` with exactly
   `{"call_id":"<nonempty string>"}`. The response contains opaque slot IDs;
   clients choose one of those IDs without interpreting its contents.
3. Send `help.call.schedule.book` with exactly
   `{"call_id":"<nonempty string>","slot_id":"<nonempty string>"}`. Booking is
   a separate write and must pass its own approval and expiry gate; approval
   for creation does not approve booking. Request-ID idempotency still applies
   to the booking command.
Scheduling metadata is documented only where observed: category `616` yields
`"scheduling":{"mode":"calendar"}`; category `624` yields
`"scheduling":{"mode":"contact","phone":"077-7076023","extension":"2"}`. Every
other category yields `"scheduling":{"mode":"unknown"}`; no other
classification is supported. When creation follows a scheduler redirect, its
result includes `"scheduling":{"mode":"calendar","call_id":"..."}`. A normal
creation includes `"scheduling":{"mode":"assigned","handler":"..."}` only for
a nonempty handler and includes `"scheduling":{"mode":"unknown"}` otherwise.
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
