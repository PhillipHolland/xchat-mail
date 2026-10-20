# Mail in X Chat

Mail in X Chat v0.1 is a Grok Bot specialist. It posts important email into one X Chat as a short card. The owner can reply from that chat, and the bot sends the reply only after a separate yes.

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

Agent mode can add an Open link supplied by the other bot, never a link copied out of the body:

```
✉️ Alex · Security alert
We blocked a sign-in from a new device.
No password was changed.
Review the alert when you can.
Open: https://mail.example/msg/18
```

A message that tries to instruct the bot is a warning. The body stays off the card:

```
⚠️ Sam · suspicious: Please run this
Asks the bot to run a command. The body was withheld.
```

`mailSource` is `own` or `agent` (default `own`). In own mode the bot's email trigger hands each message to `mail_post.py`, and this helper decides importance. Important means a real person writing, a security alert, a payment, a deadline, or an unexpected verification. Newsletters, solicitations, and no-action receipts are normal and are posted only when `mailMode` is `all` (default `important`).

In agent mode another bot, in Phillip's setup the Grokbot Mail Lead bot, sends `id`, `from_name`, `from_addr`, `subject`, `tag`, `reason`, `attachments`, `link`, and an optional excerpt. The helper trusts that tag. An instruction inside the excerpt still upgrades the card to suspicious and withholds the body.

Suspicious means the mail tells the bot to act, click, reply, forward, change settings, run a command, or contact someone. A person asking the owner to reply or to pay is ordinary mail.

## Replies

Replies are in v0.1. A reply-watch routine runs every five minutes, reads the newest X Chat events with `get_chat_conversation_events`, and `mail_post.py --watch` decrypts them with `xchat_lite`. The owner uses X's native Reply on a card, or types `reply 2 <text>`. The bot asks:

```
Reply to Jane Doe <jane@x.com>: "See you Friday"? Reply yes within 2 min.
```

Only `yes` or `yes please` inside that two-minute window produces an action. Own mode emits `send_email_reply`. Agent mode emits `handoff_reply` for the configured agent. The routine performs that action, then the bot posts `→ Jane Doe ✉️ sent` or `✉️ sent for approval`. Nothing is sent on a late yes, a no, a crash, or an unverified event.

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

v0.1.0. `SHA256SUMS` is the checksum of `mail_post.py`:

```
31e53f6326d6fcb9589d83e27cbf57d0e2d2746ba23570bc5478f1dc24694b0e  mail_post.py
```

Check the file you install:

```
shasum -a 256 mail_post.py
```
