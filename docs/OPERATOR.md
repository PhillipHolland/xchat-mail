# Operator notes

Mail in X Chat v0.1.1 posts one owner's important mail into one X Chat. The helper is `mail_post.py`. It runs on the bot's Linux computer. The sender account and the owner already share that X Chat, the same shape as a single relay conversation. State lives in `~/.xchat-mail`. Code lives in `~/xchat-mail`. Those two directories are different, and neither is `~/.imessage-relay`.

The committed `config.example.json` has empty strings. Real ids stay in `~/.xchat-mail/config.json` on the bot. The prompts below read that file. The only placeholder you paste in is `<SENDER_CONNECTION>`, the sender's X Chat connection name.

The three prompts in this file are the only routine text. Do not keep a second copy in a saved routine, a note, or another file. When a saved routine and this file disagree, this file wins. Replace the saved routine from here.

## Install

On the bot computer, install `xchat_lite` from [xchat-grokbot-helper](https://github.com/xdevplatform/xchat-grokbot-helper). Do not copy it into this repo.

```
git clone https://github.com/xdevplatform/xchat-grokbot-helper.git ~/xchat-grokbot-helper
cd ~/xchat-grokbot-helper
python3 -m venv .venv
.venv/bin/pip install -U pip "chatxdk<0.6"
```

Copy the release `mail_post.py` to `~/xchat-mail/mail_post.py`. Copy this file to `~/xchat-mail/OPERATOR.md`. Copy `SHA256SUMS` to `~/xchat-mail/SHA256SUMS`. `SHA256SUMS` lists `mail_post.py` and `docs/OPERATOR.md`. The installed `~/xchat-mail/OPERATOR.md` is that second file, so its hash matches the `docs/OPERATOR.md` line. Create the state directory and the config:

```
mkdir -p ~/.xchat-mail ~/xchat-mail
chmod 700 ~/.xchat-mail
cp mail_post.py ~/xchat-mail/mail_post.py
cp docs/OPERATOR.md ~/xchat-mail/OPERATOR.md
cp SHA256SUMS ~/xchat-mail/SHA256SUMS
cp config.example.json ~/.xchat-mail/config.json
chmod 600 ~/.xchat-mail/config.json
shasum -a 256 mail_post.py docs/OPERATOR.md
```

Compare those two hashes with `SHA256SUMS` before the first run. Each routine below checks them again.

Fill `config.json`:

| Field | Value |
| --- | --- |
| `mailSource` | `own` (the template default) or `agent` (Mail Lead or Gmail boss sends the fields). |
| `mailMode` | `important` or `all`. Default `important`. |
| `conversation_id` | The one X Chat, hyphen form `111-222`. |
| `owner_user_id` | The human who reads the cards and says yes. |
| `sender_user_id` | The bot's sender account. Must be different from the owner. |
| `agent_id` | The Mail Lead bot's id. Required when `mailSource` is `agent`. |
| `gmail_agent_id` | The Gmail boss bot's id. Required in agent mode when the mail id starts with `gmail_`. Empty in the example. |
| `state_dir` | `~/.xchat-mail` |
| `helper.python` | `~/xchat-grokbot-helper/.venv/bin/python` |
| `helper.script` | `~/xchat-grokbot-helper/xchat_lite.py` |
| `helper.user_id` | The sender user id. |
| `helper.key_version` | The sender's Chat key version. |
| `helper.juicebox` | `~/xchat/sender/juicebox.json`. Do not copy it into `~/.xchat-mail`. |
| `helper.xdg_config_home` | `~/xchat/sender` |

`xchat_lite` accepts `get_users_public_keys` entries as the tool returns them. It maps `public_key` to the identity key and `signing_public_key` to the signing key. Pass those entries through unchanged.

## Unlock the sender once

The Chat PIN is collected with a secret request, never typed into chat, and never written into this repo or into a routine prompt. Pass it only as `CHAT_PIN` for one unlock:

```
XDG_CONFIG_HOME=~/xchat/sender \
CHAT_PIN="<from the secret request>" \
  ~/xchat-grokbot-helper/.venv/bin/python ~/xchat-grokbot-helper/xchat_lite.py \
  --user-id "<sender user id>" --key-version "<sender key version>" \
  --juicebox ~/xchat/sender/juicebox.json --remember-key unlock-check
```

The mail routines do not ask for the PIN. If unlock fails, ask again with a new secret request. After three failures, stop and check the PIN in X under Settings. `xchat_lite.py forget` under that `XDG_CONFIG_HOME` drops the saved key.

## State

`~/.xchat-mail` is mode 700. The files inside are mode 600.

| File | Contents |
| --- | --- |
| `config.json` | The table above. |
| `seen_ids.json` | Email ids and a timestamp. No bodies. |
| `xchat_keys.json` | Conversation key events and public signing keys. 24 hours, at most 512 KiB. Never a message. |
| `cards.json` | Card number, name, ids, and hashes of the address, the subject, and the card text. No subject, no address, no body. |
| `pending.json` | The reply the owner typed, and a hash of the address, while a confirm is open. The address itself is not stored. Deleted on yes-ack, no, or expiry. |
| `seen_events.json` | Chat event ids already handled. Capped. |
| `mail.log` | Counts only, such as `important=1`. |
| `watch_until` | A unix timestamp. `--watch` does nothing unless this time is in the future. |
| `spent_effects.json` | Effect ids already returned, and the yes event ids. No reply text. |
| `.lock` | A file lock around one helper run. Created mode 600. |

`sender-juicebox.json` is not a state file. The only juicebox is `~/xchat/sender/juicebox.json`. On startup the helper deletes a copy left in this directory, and it refuses a juicebox path outside `~/xchat/sender`.

`--list-cards` prints `n`, `from_name`, and `suspicious`. It does not print a subject, an address, or a body. Card numbers are not printed on the card. `reply 2` uses this list. Native Reply does not need the number.

## What the helper prints

Stdout is one JSON object.

- `send` carries ciphertext in `sends[]` for `send_chat_message`: `conversation_id`, `message_id`, `encoded_message_create_event`, `encoded_message_event_signature`, `should_notify`. If `add_conversation_keys` is set, call that first. The reply target is already inside the encoded event.
- `phase` is `card`, `confirm`, `approved`, `cancelled`, `expired`, `unknown_card`, `refused`, or `no_address`.
- `approved` also carries `effect`. That object contains the owner's reply text. It is the only plaintext on stdout. Do not write stdout to a log.
- `fetch` means you need `conversation_key_events` and `signing_keys`. Fetch once. A second `fetch` is an error.
- `skip` means do nothing to the chat (`normal`, `duplicate`, `quiet`, `awaiting_ack`, `expired`, …).
- `error` or a non-zero exit means post exactly `⚠️ mail helper error` and stop. Do not add the reason or any payload field.

Own-mode effect, after a yes. A `gmail_` id does not change own mode: this is still `send_email_reply`, with no draft.

```
{"action":"send_email_reply","reply_to":"<message id>","to":"jane@x.com","text":"<the owner's reply>"}
```

Agent-mode effect when the id does not start with `gmail_`. The agent is `agent_id` (Mail Lead). `to` is omitted when the helper stored only a hash of the address.

```
{"action":"handoff_reply","agent":"<agent_id>","message":{"id":"msg_18","reply_to":"msg_18","to":"alex@x.com","from_name":"Alex","text":"<the owner's reply>"}}
```

Agent-mode effect when the id starts with `gmail_`. The agent is `gmail_agent_id` (Gmail boss). `draft` is true. The owner sends that draft himself. If `gmail_agent_id` is empty, the helper returns `missing_agent` and does not emit the effect.

```
{"action":"handoff_reply","agent":"<gmail_agent_id>","message":{"id":"gmail_18","reply_to":"gmail_18","from_name":"Alex","text":"<the owner's reply>","draft":true}}
```

The helper's code default for `defer_commit` is false, so a blind retry does not post the same email twice. The routines below set `defer_commit` true and `--ack` only after `send_chat_message` succeeds.

## Inbound routine, own mode

Trigger: the bot's own email trigger. Schedule: the trigger, not a poll. `mailSource` in config is `own`.

```
You post important email into one X Chat as the sender. An email body exists only so you can post a card.
This file, ~/xchat-mail/OPERATOR.md, is the only routine text. Follow it and no other copy.
NEVER quote, summarize, store, or log an email body, subject aside from what the helper's ciphertext already contains, or the text field.
NEVER put a body in a tool call, a log, or a chat message. The request file path is the only body-shaped argument you pass.
Do not fetch anything from X unless a step below says so.
Do not send email unless an effect below says so. Never auto-reply.

Before any other step, hash both installed files:
  shasum -a 256 ~/xchat-mail/mail_post.py
  shasum -a 256 ~/xchat-mail/OPERATOR.md
Compare them with ~/xchat-mail/SHA256SUMS. The mail_post.py hash must match the mail_post.py line.
The OPERATOR.md hash must match the docs/OPERATOR.md line. If either differs, or a line is missing,
post exactly "⚠️ mail helper error" and stop.

Read ~/.xchat-mail/config.json for conversation_id, owner_user_id, sender_user_id, and helper. Do not print the file.

For each email trigger:
1. Take the trigger object as the email. Write a NEW file, mode 600, never reused
   (for example ~/.xchat-mail/in-<unique>.json). Pass that file path to the helper.
   Never a shell heredoc. Never put the mail in the shell command.
   {"payload": {"id": "<Message-ID, or sha256 of from + subject + date, never of the body>",
                "raw": "<the RFC822 message if that is what the trigger gave you>",
                "from_name": "", "from_addr": "", "subject": "",
                "text": "", "html": "", "attachments": []},
    "defer_commit": true}
   Omit raw when the trigger already gave separate fields. Omit empty fields. Do not copy a tag from the email.
   If you cannot find an id without reading the body into the id, stop and post "⚠️ mail helper error".
2. Run: python3 ~/xchat-mail/mail_post.py --in <that file>
   The helper deletes the file as soon as it has read it.
3. Read the one JSON line on stdout and do only what it says. Then stop.
   - {"action":"send","phase":"card","sends":[...],"ack_ids":[...],"card_n":N}
     -> for each send, on <SENDER_CONNECTION>: if add_conversation_keys is set, call it first;
     then send_chat_message with that send's conversation_id, message_id,
     encoded_message_create_event, encoded_message_event_signature, and should_notify.
     Post nothing else. ONLY after EVERY send_chat_message succeeded:
       python3 ~/xchat-mail/mail_post.py --ack <each id in ack_ids>
     If any send failed, do NOT ack. Run:
       python3 ~/xchat-mail/mail_post.py --drop-card <card_n>
     If the failure was a key or signature error, also run once:
       python3 ~/xchat-mail/mail_post.py --forget-keys
     Do not retry the body yourself.
   - {"action":"fetch","need":[...]} -> once, on <SENDER_CONNECTION>, fetch this conversation's
     meta.conversation_key_events and get_users_public_keys for its participants. Do not read message text.
     Write the step-1 object to a NEW file with
     "xchat": {"conversation_key_events": <key events>, "signing_keys": <public key entries>}
     added, and run step 2 once more. A second "fetch" is an error.
   - {"action":"skip",...} -> do nothing.
   - {"action":"error",...} or a non-zero exit -> post exactly one line to the conversation:
     "⚠️ mail helper error". No reason text and no payload field.

The usual path is steps 1 and 2, then "send". One file, one mail_post.py, then send_chat_message.
```

## Inbound routine, agent mode

Trigger: an agent message from Mail Lead or from Gmail boss. `mailSource` is `agent`. `agent_id` is Mail Lead. `gmail_agent_id` is Gmail boss. Do not open a mailbox. The helper trusts the sender's tag. It still withholds a body when the excerpt itself instructs this bot. When the injection check is unsure, the card has the sender, the subject, and Open, and no excerpt. The excerpt is on the card only when it is clean. The Open host is `mail.google.com` for a Gmail card and `pbh-mail.vercel.app` otherwise.

```
You post mail cards into one X Chat as the sender. The fields arrive from Mail Lead or from Gmail boss.
This file, ~/xchat-mail/OPERATOR.md, is the only routine text. Follow it and no other copy.
NEVER quote, summarize, store, or log the text field or any email body.
NEVER put a body in a tool call, a log, or a chat message.
NEVER open a mailbox. Do not fetch anything from X unless a step below says so.
Do not message Mail Lead or Gmail boss unless an effect in the reply-watch routine says so. Never auto-reply.

Before any other step, hash both installed files:
  shasum -a 256 ~/xchat-mail/mail_post.py
  shasum -a 256 ~/xchat-mail/OPERATOR.md
Compare them with ~/xchat-mail/SHA256SUMS. The mail_post.py hash must match the mail_post.py line.
The OPERATOR.md hash must match the docs/OPERATOR.md line. If either differs, or a line is missing,
post exactly "⚠️ mail helper error" and stop.

Read ~/.xchat-mail/config.json for conversation_id, agent_id, gmail_agent_id, and helper. Do not print the file.

For each agent message that carries mail fields:
1. Write a NEW file, mode 600, never reused (for example ~/.xchat-mail/in-<unique>.json).
   Pass that file path to the helper. Never a shell heredoc. Never put the excerpt in the shell command.
   {"payload": {"id": "<msg_N>", "from_name": "", "from_addr": "", "subject": "",
                "tag": "<important|suspicious|normal>", "reason": "",
                "attachments": [], "link": "", "text": "<the excerpt, if any>"},
    "defer_commit": true}
   The text field is the excerpt Mail Lead sent, at most 600 characters. Do not add a body of your own.
2. Run: python3 ~/xchat-mail/mail_post.py --in <that file>
3. Handle stdout with the same send, fetch, skip, and error rules as the own-mode inbound routine.
   On send failure, --drop-card and do not ack. On key or signature failure, --forget-keys once.
   On error or a non-zero exit, post exactly "⚠️ mail helper error".
```

Run one inbound routine, own or agent, matching `mailSource`. Running both will double-post if both ever see the same mail.

## Reply-watch routine

Trigger: a schedule, every 5 minutes. That is the shortest gap Grok routines allow. The same run then polls for up to 2 minutes after a confirm is on screen. One conversation. On HTTP 429, stop and do not send.

Reply-watch is paused as of 20 Oct 2026. While it is paused, the owner replies through the Open link on the card: Gmail when the id starts with `gmail_`, and the pbh-mail reader otherwise. `--watch` returns `{"action":"skip","reason":"watch_paused"}` unless `~/.xchat-mail/watch_until` is a unix timestamp in the future. The helper does not cap that timestamp. A 30-minute resume is the unix time 1800 seconds from now, written into that file. Do not resume unless the owner asks.

```
You watch one X Chat for the owner's reply to a mail card. You never send email or hand off a reply
unless stdout contains an effect, and you perform that effect at most once.
This file, ~/xchat-mail/OPERATOR.md, is the only routine text. Follow it and no other copy.
NEVER quote, summarize, store, or log an email body or a decrypted chat body.
NEVER put message text in a tool call except the effect fields when you perform an approved reply.
Do not fetch anything from X unless a step below says so. Never auto-reply.

Before any other step, hash both installed files:
  shasum -a 256 ~/xchat-mail/mail_post.py
  shasum -a 256 ~/xchat-mail/OPERATOR.md
Compare them with ~/xchat-mail/SHA256SUMS. The mail_post.py hash must match the mail_post.py line.
The OPERATOR.md hash must match the docs/OPERATOR.md line. If either differs, or a line is missing,
post exactly "⚠️ mail helper error" and stop.

Reply-watch is paused as of 20 Oct 2026. If ~/.xchat-mail/watch_until is missing, past, or not a
number, stop now. Do not fetch events and do not send. The owner replies through the card Open link:
Gmail when the id starts with gmail_, and the pbh-mail reader otherwise.
Resume for 30 minutes only if the owner asks. Write the unix time 1800 seconds from now into
~/.xchat-mail/watch_until, mode 600, and no other text. Then continue below.

Read ~/.xchat-mail/config.json for conversation_id, owner_user_id, sender_user_id, agent_id,
gmail_agent_id, and helper. Do not print the file. Use the hyphen form of conversation_id.

1. On <SENDER_CONNECTION>, get_chat_conversation_events for that conversation, newest page only,
   max_results 30. On HTTP 429, stop. Do not read decoded text. Keep data[].encoded_event and
   meta.conversation_key_events.
2. Write a NEW file, mode 600, never reused (for example ~/.xchat-mail/watch-<unique>.json).
   Pass that file path to the helper. Never a shell heredoc. Never put message text in the shell command.
   {"events": <the data array, encoded events only>,
    "xchat": {"conversation_key_events": <meta.conversation_key_events>}}
   Omit xchat when you do not have key events yet. The helper keeps a 24 hour key cache.
3. Run: python3 ~/xchat-mail/mail_post.py --in <that file> --watch
4. Read the one JSON line.
   - {"action":"fetch"} -> once, fetch meta.conversation_key_events and get_users_public_keys
     for the participants. Pass the public-key entries through unchanged. Write a NEW file with
     those events plus "xchat": {"conversation_key_events": ..., "signing_keys": ...} and pass the path,
     never a heredoc. Run step 3
     once more. A second "fetch" is an error: post "⚠️ mail helper error" and stop.
   - {"action":"skip","reason":"watch_paused"} -> stop. Reply-watch is paused. Do not post and do not send mail.
   - {"action":"skip","reason":"quiet"} -> stop. There is nothing to post.
   - {"action":"skip","reason":"awaiting_ack"} -> stop. An effect was already returned. Do not perform it again.
   - {"action":"skip","reason":"expired"} -> stop.
   - {"action":"send","phase":"confirm","confirm_id":"...","sends":[...]}
     -> post that send with send_chat_message (add_conversation_keys first if set).
     If this run already posted this confirm_id, do not post it again.
     ONLY after send_chat_message succeeds:
       python3 ~/xchat-mail/mail_post.py --confirm-posted <confirm_id>
     If the send failed, do NOT confirm-posted. On a key or signature error, --forget-keys once.
     The next watch will offer the same confirm again. Then stop. Do not start the wait below.
     After --confirm-posted succeeds, stay in this run and poll:
       Repeat for at most 120 seconds, sleeping 20 seconds between tries:
         Fetch the newest events page again (max_results 30). On HTTP 429, stop the wait and do not send.
         Write a NEW mode-600 file with those events and the key material you already hold.
         Pass the path. Never a heredoc.
         Run mail_post.py --in <file> --watch
         - phase approved: perform the effect ONCE (see below), and only if that succeeds post sends[]
           with send_chat_message, then:
             python3 ~/xchat-mail/mail_post.py --ack-reply
           Stop the wait.
         - phase cancelled or expired: post that send ("Not sent." or "Too late to send.") and stop.
           There is no effect. Do not send mail.
         - phase confirm: you missed --confirm-posted. Post only if this confirm_id is not already on screen,
           then --confirm-posted, and keep waiting.
         - skip quiet or awaiting_ack: keep waiting until the 120 seconds end.
         - fetch: one key fetch and one retry. A second fetch stops the wait with "⚠️ mail helper error".
         - error or non-zero: post "⚠️ mail helper error" and stop the wait.
       When the 120 seconds end with no yes:
         python3 ~/xchat-mail/mail_post.py --expire
         Stop. Do not send mail.
   - any other send phase (unknown_card, refused, no_address): post that one send and stop.
   - error or non-zero: post exactly "⚠️ mail helper error" and stop.

Performing an approved effect:
- send_email_reply: own mode. Send one email from this bot's own mailbox. reply_to, to, and text come from the effect.
  An id that starts with gmail_ does not change own mode. There is no draft.
  If you cannot send mail, do not post the sent line. Run --release-reply once and stop.
- handoff_reply: agent mode. Send one agent message to effect.agent with effect.message. Do not open a mailbox.
  If the id starts with gmail_, effect.agent is gmail_agent_id (Gmail boss) and effect.message.draft is true.
  That is a draft the owner sends himself. Do not send the email yourself.
  Every other id uses agent_id (Mail Lead) and has no draft field.
  If the handoff fails, do not post the sent line. Run --release-reply once and stop.
- If the effect succeeded and send_chat_message of the sent line failed, retry that ciphertext once.
  Do NOT --release-reply. --release-reply records the effect in spent_effects.json and will not emit that yes again.
  The sent line is a ciphertext retry, not a new email. If the retry fails, stop.
- If you are unsure the effect ran, do not release and do not retry it.

yes and yes please count. y, yeah, and "yes, send" do not. no, nope, cancel, and stop cancel.
A yes that arrived before the confirm was posted does not count. The owner says yes again after they see it.
Edits, reactions, and unverified messages do not count. One confirm at a time; a second reply waits.
```

Suspicious cards are not replyable. A card with no address is not replyable. The helper answers those with a one-line warning and no effect.

## Why replies work this way

`get_chat_conversation_events` is the read. It is durable: if the five-minute run is late, the next run still sees the owner's message. The helper decrypts with `xchat_lite` and accepts an owner message only when its status is `verified` or `unencrypted`. `unverified` is ignored, including per-message-key events that current `xchat_lite` can open but cannot signature-check. If every owner reply shows up unverified, update `xchat_lite` and chatxdk. Do not teach the helper to trust unverified yeses.

A native Reply is matched by `replying_to_message_sequence_id` after the sender's own card event is bound, first by message id, then by a hash of the card text. `reply N <text>` uses the card map from `--list-cards`.

The two-minute yes cannot be a five-minute schedule. The routine that posts the confirm keeps polling every 20 seconds, about six extra event reads, and only while that confirm is open. Five-minute scans are about 288 reads a day, under the chat-events budget of 1,000 a day and 30 per 15 minutes. A burst during one confirm stays inside the 15-minute budget for this single conversation. Stop on HTTP 429 anyway.

`create_activity_subscription` and `create_webhooks` exist on the X API and are not used. A webhook has to be public HTTPS, on port 443, and has to answer X's CRC challenge with the app's consumer secret within 10 seconds. A Grok routine is not that HTTP server, and the bot computer is not assumed to have a public URL. Without a webhook, an activity subscription delivers only while `GET /2/activity/stream` is held open. A short routine cannot hold that stream, and an always-on stream is the wrong shape for this bot. v0.1.1 uses the events read plus the in-turn wait.

The yes clock starts at `--confirm-posted`, inclusive at 120 seconds and closed at 121. A yes already in the batch that proposed the confirm cannot approve it. If the wait dies, the helper does not send. The owner replies and says yes again. After the effect is returned, further watches say `awaiting_ack` and do not include the reply text, so a retry cannot send a second email. `--release-reply` is only for an effect that failed before the sent line was posted. It writes the effect id to `spent_effects.json`, so that yes cannot emit a second time.

## Failure and quiet

The chat line for a broken helper is exactly:

```
⚠️ mail helper error
```

Cards, confirms, sent lines, `Not sent.`, and `Too late to send.` are the messages the helper encrypts. Do not add a second bubble. Do not include a subject or a body in anything you post yourself.

`mail.log` stays counts. If you need to debug, the JSON `reason` is a fixed code (`xchat_lite_missing`, `bad_payload`, `no_cached_keys`, …). It is not an email body. Do not paste stdout into the chat.
