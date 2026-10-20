# Mail in X Chat

Mail in X Chat v0.1.1 is a Grok Bot specialist. It posts important email into one X Chat as a short card. The owner can reply from that chat, and the bot sends the reply only after a separate yes.

It runs on the bot's own Linux computer with python3. The standard library is enough. There is no Mac app and no browser extension. `xchat_lite` (the [xchat-grokbot-helper](https://github.com/xdevplatform/xchat-grokbot-helper)) is installed beside it and is not part of this repository.

This project has its own repo, its own state directory (`~/.xchat-mail`), and its own helper. It does not import the iMessage relay and it does not read `~/.imessage-relay`.

## Cards

One X Chat message. Names below are fictional.

Own mode has no web viewer, so the card has no Open line:

```
✉️ Jane Doe · Friday's invoice
The March invoice is attached.
Please pay it by Friday.
📎 invoice.pdf
```

Agent mode can add an Open link supplied by the other bot, never a link copied out of the body. Only `mail.google.com` and `pbh-mail.vercel.app` are kept, and the query string and fragment are removed:

```
✉️ Alex · Security alert
We blocked a sign-in from a new device.
No password was changed.
Review the alert when you can.
Open: https://pbh-mail.vercel.app/m/msg/18
```

If the injection check is unsure, the card still posts. It shows the sender, the subject, and Open, and it leaves the excerpt off:

```
✉️ Alex · Notes
Open: https://pbh-mail.vercel.app/m/msg/u
```

The excerpt is included only when that check is clean. A message that tries to instruct the bot is a warning. The body stays off the card:

```
⚠️ Sam · suspicious: Please run this
Asks the bot to run a command. The body was withheld.
```

`mailSource` is `own` or `agent` (default `own`). In own mode the bot's email trigger hands each message to `mail_post.py`, and this helper decides importance. Important means a real person writing, a security alert, a payment, a deadline, or an unexpected verification. Newsletters, solicitations, and no-action receipts are normal and are posted only when `mailMode` is `all` (default `important`).

In agent mode Mail Lead or Gmail boss sends `id`, `from_name`, `from_addr`, `subject`, `tag`, `reason`, `attachments`, `link`, and an optional excerpt. An id that starts with `gmail_` is handed to Gmail boss as a draft. Every other id goes to Mail Lead. The helper trusts that tag. An instruction inside the excerpt still upgrades the card to suspicious and withholds the body. The routine prompts live only in [docs/OPERATOR.md](docs/OPERATOR.md).

Suspicious means the mail tells the bot to act, click, reply, forward, change settings, run a command, or contact someone. A person asking the owner to reply or to pay is ordinary mail.

## Replies

Replies are in v0.1. A reply-watch routine runs every five minutes, reads the newest X Chat events with `get_chat_conversation_events`, and `mail_post.py --watch` decrypts them with `xchat_lite`. The owner uses X's native Reply on a card, or types `reply 2 <text>`. The bot asks:

```
Reply to Jane Doe <jane@x.com>: "See you Friday"? Reply yes within 2 min.
```

Only `yes` or `yes please` inside that two-minute window produces an action. Own mode emits `send_email_reply`. Agent mode emits `handoff_reply`: a `gmail_` id goes to Gmail boss with `draft` true, and every other id goes to Mail Lead. The routine performs that action, then the bot posts `→ Jane Doe ✉️ sent` or `✉️ sent for approval`. Nothing is sent on a late yes, a no, a crash, or an unverified event. Reply-watch stays paused unless `~/.xchat-mail/watch_until` is a unix timestamp in the future. Until then the owner uses the card's Open link.

The five-minute schedule is the fastest a Grok routine can run, so the confirm can take that long to appear. The two-minute clock starts when the confirm is posted, and the same routine run polls events every 20 seconds until it ends. Webhooks and the activity stream are not the v0.1 path: a webhook needs a public HTTPS endpoint that answers X's CRC check, and a stream has to stay open for the life of the process. A short routine on the bot computer can do neither. Event history is durable, so a missed schedule still sees the reply on the next run. The details, and both routine prompts, are in [docs/OPERATOR.md](docs/OPERATOR.md).

## Privacy

Email bodies are not stored and are not written to the log. The routine's request file is mode 600 and is deleted as soon as it is read. Card text is passed to `xchat_lite` on stdin and leaves as ciphertext. `mail.log` is counts only. Verification codes of 4–8 digits that sit near a code or verify word are masked before the card is built. Links in the body are removed. The only plaintext this program prints is the owner's own reply, inside the approved effect the routine must send. No keys and no account ids belong in this repo.

## Layout

| Path | Role |
| --- | --- |
| `mail_post.py` | The helper. This is the release asset. |
| `config.example.json` | Copy to `~/.xchat-mail/config.json` on the bot. Leave the committed copy empty. |
| `docs/OPERATOR.md` | Install, unlock, and the routine prompts. |
| `tests/` | Cleaner, cards, suspicious mail, code masking, replies, and both modes. |

```
python3 -m unittest discover -s tests -v
```

## Release

v0.1.1. `SHA256SUMS` covers `mail_post.py` and `docs/OPERATOR.md`. The release assets are those two names: `mail_post.py` and `SHA256SUMS`. Copy `docs/OPERATOR.md` to `~/xchat-mail/OPERATOR.md`. That copy matches the `docs/OPERATOR.md` line.

```
abf624ac9403a2e86dcb334fe244c6871cc290d877d1966c15309cd2e621ccf0  mail_post.py
f73f379d255f6a8920f54e75dd57854f08141e152c3a5100e5218fac245ae9ed  docs/OPERATOR.md
```

Check the files you install:

```
shasum -a 256 mail_post.py docs/OPERATOR.md
```
