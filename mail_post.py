#!/usr/bin/env python3
"""Mail in X Chat v0.1.0 — routine helper (runs on the bot's Linux computer).

An email-trigger routine (own mode) or an agent message (agent mode) writes one
request file and runs this script. The file is deleted as soon as it is read.
Stdout is one JSON object: a pre-encrypted send plan for ``send_chat_message``,
or a reply effect the routine performs only after the owner says yes.

Email bodies never go into the log, the dedupe store, or stdout. The card text
is handed to xchat_lite on stdin and leaves only as ciphertext. A reply the
owner typed sits in pending.json (mode 600) until they say yes, they say no,
or two minutes pass, and is then deleted.

xchat_lite is not vendored. It is the xchat-grokbot-helper install the X Chat
skill describes. This file does not import the iMessage relay.
"""
from __future__ import annotations

import argparse
import email.policy
import email.utils
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from email.parser import BytesParser
from html.parser import HTMLParser

VERSION = '0.1.0'
STATE_DIR = '~/.xchat-mail'
SEP = ' \u00b7 '
CARD_MARK = '\u2709\ufe0f'
WARN_MARK = '\u26a0\ufe0f'
CLIP_MARK = '\U0001f4ce'
YES_WINDOW_S = 120
PROPOSAL_TTL_S = 600
KEY_CACHE_TTL_S = 24 * 3600
KEY_CACHE_MAX_BYTES = 512 * 1024
NEED_KEYS = ['conversation_key_events', 'signing_keys']
AGENT_TEXT_CAP = 600
OWN_TEXT_CAP = 200_000
REPLY_TEXT_CAP = 2000
CONFIRM_SHOW_CAP = 180
CODE_WINDOW = 48
SEEN_EVENT_CAP = 500
KNOWN_ID_CAP = 200

LOG_KEYS = (
    'important', 'normal', 'suspicious', 'duplicate', 'reply_approved',
    'reply_expired', 'reply_cancelled', 'error', 'fetch', 'empty',
)

# Bot-directed commands. A person asking the owner to reply or pay is not this.
_SUSPICIOUS = (
    (re.compile(r'(?i)ignore (?:all |any )?(?:previous|prior|above) (?:instructions|prompts|rules)'),
     'Tries to override the bot. The body was withheld.'),
    (re.compile(r'(?i)\b(?:system prompt|developer message|jailbreak)\b'),
     'Tries to instruct the bot. The body was withheld.'),
    (re.compile(r'(?i)<\s*/?\s*system\s*>'),
     'Tries to instruct the bot. The body was withheld.'),
    (re.compile(r'(?i)\b(?:run|execute|eval)\b.{0,48}\b(?:command|commands|script|shell|curl|wget|powershell)\b'),
     'Asks the bot to run a command. The body was withheld.'),
    (re.compile(r'(?i)\b(?:curl|wget)\b.{0,40}\b(?:sh|bash)\b'),
     'Asks the bot to run a command. The body was withheld.'),
    (re.compile(r'(?i)\b(?:curl|wget)\s+[^\s]*\|\s*(?:sh|bash)\b'),
     'Asks the bot to run a command. The body was withheld.'),
    (re.compile(r'(?i)\bchange (?:your|the) (?:settings|password|configuration|rules|instructions)\b'),
     'Asks the bot to change settings. The body was withheld.'),
    (re.compile(r'(?i)\bforward (?:this|the) (?:email|message|mail)\b'),
     'Asks the bot to forward or contact someone. The body was withheld.'),
    (re.compile(r'(?i)\bforward\b.{0,40}\b(?:everyone|everybody|all contacts)\b'),
     'Asks the bot to forward or contact someone. The body was withheld.'),
    (re.compile(r'(?i)\b(?:contact|email|message|call) (?:anyone|everybody|everyone|all (?:my )?contacts)\b'),
     'Asks the bot to forward or contact someone. The body was withheld.'),
    (re.compile(r"(?i)\b(?:do not|don't) (?:tell|inform|alert) (?:the )?(?:user|owner|human)\b"),
     'Tries to hide an instruction from the owner. The body was withheld.'),
    (re.compile(r'(?i)\b(?:click|open|tap) (?:here|this link|the link|below)\b.{0,80}\b(?:reply|forward|send|run|enter|password|code)\b'),
     'Asks the bot to click and then act. The body was withheld.'),
    (re.compile(r'(?i)\byou must\b.{0,48}\b(?:act|click|reply|forward|run|execute|contact)\b'),
     'Tries to instruct the bot. The body was withheld.'),
    (re.compile(r'(?i)\b(?:as an? )?(?:ai|assistant|bot|language model)\b.{0,60}\b(?:you must|ignore|run|forward|execute)\b'),
     'Tries to instruct the bot. The body was withheld.'),
)

_CODE_WORD = re.compile(
    r'(?i)\b(?:codes?|otp|passcodes?|verify|verification|verifying|2fa|mfa|one-time|onetime|pin)\b')
_CODE_NUM = re.compile(r'(?<!\d)(\d{4,8})(?!\d)')
_URL = re.compile(r'(?i)\b(?:https?://|mailto:|www\.)\S+')
_LONG_BARE = re.compile(r'(?i)\b[\w.-]+\.[a-z]{2,}/\S{8,}')
_ZW = re.compile(r'[\u200b-\u200d\ufeff\u2060]')
_REPLY_CMD = re.compile(r'(?i)^reply\s+(\d{1,6})\s+(\S(?:.*\S)?)\s*$')
_ON_WROTE = re.compile(r'(?is)^on\b.{0,240}?wrote:\s*$')
_WROTE_LINE = re.compile(r"(?i)^[\w .'@<>+\-]{0,80}\bwrote:\s*$")
_ORIG = re.compile(r'(?i)^(?:-{2,}\s*original message\s*-{2,}|begin forwarded message:)\s*$')
_FROM_HDR = re.compile(r'(?i)^from:\s+\S')
_SENT_HDR = re.compile(r'(?im)^sent:\s+')
_SUBJ_HDR = re.compile(r'(?im)^subject:\s+')
_SIG = re.compile(r'^(?:--\s?|—\s?|__{3,}|Sent from my |Get Outlook|Sent via )')
_AUTO_LOCAL = re.compile(
    r'^(?:no-?reply|noreply|newsletter|news|marketing|promo|notifications?|'
    r'mailer-daemon|bounce|donotreply|do-not-reply|updates)\b', re.I)
_HTMLISH = re.compile(r'(?i)<\s*(?:html|body|div|p|br|span|table|a|td|tr)\b')
_ABBR = ('Mr.', 'Mrs.', 'Ms.', 'Dr.', 'Jr.', 'Sr.', 'vs.', 'e.g.', 'i.e.', 'St.')

_SECURITY = re.compile(
    r'(?i)\b(?:security alert|new sign-in|new sign in|new device|unusual (?:sign-in|activity)|'
    r'password (?:reset|was changed|changed)|locked (?:your|the) account|suspicious (?:sign-in|login))\b')
_VERIFY = re.compile(
    r'(?i)\b(?:verification code|one-time code|one time code|security code|sign-in code|'
    r'sign in code|2fa|mfa|one-time passcode|otp)\b')
_PAYMENT = re.compile(
    r'(?i)\b(?:invoice|payment (?:due|failed|declined)|past due|amount due|'
    r'billing problem|card declined|refund failed)\b')
_RECEIPT = re.compile(
    r'(?i)\b(?:your receipt|payment received|order (?:confirmed|has shipped|shipped)|'
    r'no action (?:needed|required)|thanks for your (?:purchase|order))\b')
_DEADLINE = re.compile(
    r'(?i)\b(?:deadline|due by|due on|respond by|reply by|action required|'
    r'expires on|expires in|rsvp|by (?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b')
_BULK_PHRASE = re.compile(
    r'(?i)\b(?:view in (?:your )?browser|view this email|unsubscribe|email preferences|'
    r'manage (?:your )?preferences|%\s*off|limited time|special offer|you(?:\'re| are) receiving this)\b')


class MailError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


# ---------- HTML to text (stdlib parser; links are not kept) ----------
def _quote_attr(class_='', id_=''):
    blob = (class_ + ' ' + id_).lower()
    markers = ('gmail_quote', 'gmail_attr', 'gmail_signature', 'moz-cite-prefix',
               'yahoo_quoted', 'protonmail_quote')
    return any(m in blob for m in markers)


def _drop_anchor(text):
    t = ' '.join((text or '').split())
    if not t:
        return True
    low = t.lower()
    if _URL.search(t) or _LONG_BARE.search(t):
        return True
    if 'unsubscribe' in low or 'view in browser' in low or 'view this email' in low:
        return True
    if low in ('read more', 'view online', 'open in browser'):
        return True
    return False


class HTMLToText(HTMLParser):
    """Turn an HTML part into plain lines. Quote blocks, scripts, and hrefs stay out."""

    SKIP = {'script', 'style', 'head', 'title', 'noscript', 'svg', 'iframe'}
    BLOCK = {'p', 'div', 'tr', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
             'table', 'section', 'article', 'pre', 'ul', 'ol', 'blockquote', 'br'}
    VOID = {'br', 'img', 'hr', 'meta', 'link', 'input', 'source', 'wbr', 'area'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip_depth = 0
        self.quote_depth = 0
        self.a_depth = 0
        self.anchor = []
        self.hidden = []

    def handle_starttag(self, tag, attrs):
        self._start(tag, attrs)

    def handle_startendtag(self, tag, attrs):
        self._start(tag, attrs, void=True)

    def _start(self, tag, attrs, void=False):
        tag = (tag or '').lower()
        attr = {str(k).lower(): (v or '') for k, v in attrs}
        is_void = void or tag in self.VOID
        if is_void:
            if self.skip_depth or self.quote_depth:
                return
            if tag == 'br' and not self.a_depth:
                self.parts.append('\n')
            return
        if self.skip_depth:
            self.skip_depth += 1
            return
        if self.quote_depth:
            self.quote_depth += 1
            return
        if tag in self.SKIP:
            self.skip_depth = 1
            return
        if tag == 'blockquote' or _quote_attr(attr.get('class', ''), attr.get('id', '')):
            self.quote_depth = 1
            return
        if tag == 'a':
            self.a_depth = 1
            self.anchor = []
            return
        if self.a_depth:
            self.a_depth += 1
            return
        if tag in self.BLOCK:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        tag = (tag or '').lower()
        if tag in self.VOID:
            return
        if self.skip_depth:
            self.skip_depth -= 1
            return
        if self.quote_depth:
            self.quote_depth -= 1
            if self.quote_depth == 0:
                self.parts.append('\n')
            return
        if self.a_depth:
            self.a_depth -= 1
            if self.a_depth <= 0:
                self.a_depth = 0
                text = ''.join(self.anchor).strip()
                self.anchor = []
                if text and not _drop_anchor(text):
                    if self.parts and self.parts[-1] and not self.parts[-1][-1].isspace():
                        self.parts.append(' ')
                    self.parts.append(text)
            return
        if tag in self.BLOCK:
            self.parts.append('\n')

    def handle_data(self, data):
        if not data:
            return
        if self.quote_depth:
            return
        # Scripts, styles, and comments are not shown, but they are still scanned.
        if self.skip_depth:
            self.hidden.append(data)
            return
        if self.a_depth:
            self.anchor.append(data)
        else:
            self.parts.append(data)

    def handle_comment(self, data):
        if data and not self.quote_depth:
            self.hidden.append(data)

    def text(self):
        return ''.join(self.parts)

    def hidden_text(self):
        return '\n'.join(self.hidden)


def html_to_text(raw):
    """Return (visible text, hidden text). Hidden is script, style, head, and comments."""
    parser = HTMLToText()
    try:
        parser.feed(raw or '')
        parser.close()
    except Exception:
        # A broken part still yields whatever the parser kept. Never raise the source.
        pass
    return parser.text(), parser.hidden_text()


def looks_like_html(fragment):
    return bool(_HTMLISH.search(fragment or ''))


def fragment_to_text(fragment):
    fragment = fragment or ''
    if looks_like_html(fragment):
        return html_to_text(fragment)
    return fragment, ''


# ---------- cleaning (talon-style line machine on the parser's text) ----------
def _squash_ws(text):
    text = _ZW.sub('', text or '')
    text = text.replace('\u00a0', ' ').replace('\r\n', '\n').replace('\r', '\n')
    return text


def _is_on_wrote(lines, i):
    s = lines[i].strip()
    if _ON_WROTE.match(s):
        return True
    if not re.match(r'(?i)^on\b', s) or re.search(r'[.!?]\s*$', s):
        return False
    blob = ' '.join(x.strip() for x in lines[i:i + 3])
    return bool(_ON_WROTE.match(blob))


def _is_outlook(lines, i):
    if not _FROM_HDR.match(lines[i].strip()):
        return False
    blob = '\n'.join(x.strip() for x in lines[i:i + 6])
    return bool(_SENT_HDR.search(blob) and _SUBJ_HDR.search(blob))


def _quote_cut(lines):
    """Index where a quoted chain starts, or None. Signature is not a quote cut."""
    for i, line in enumerate(lines):
        s = line.strip()
        if not s:
            continue
        if _ORIG.match(s) or _is_on_wrote(lines, i) or _is_outlook(lines, i):
            return i
        if _WROTE_LINE.match(s) and 'wrote:' in s.lower() and len(s) <= 80:
            return i
    return None


def _footer_line(s):
    low = s.lower().strip()
    if not low:
        return False
    if low in ('unsubscribe', 'view in browser', 'view in your browser', 'view online'):
        return True
    if 'view in browser' in low or 'view this email' in low or 'email preferences' in low:
        return True
    if 'manage your preferences' in low or 'manage preferences' in low:
        return True
    if 'unsubscribe' in low and len(low) < 140:
        return True
    if 'intended recipient' in low or 'intended solely' in low or 'privileged and confidential' in low:
        return True
    if low.startswith('this message') and 'confidential' in low:
        return True
    if low.startswith("you're receiving") or low.startswith('you are receiving'):
        return True
    return False


def _image_only(s):
    t = s.strip()
    if not t:
        return False
    return bool(re.fullmatch(
        r'(?i)(?:\[image[^\]]*\]|\[cid:[^\]]+\]|\[photo\]|\[pict\]|\(image\)|\U0001f4f7|\U0001f5bc\ufe0f)+',
        t))


def _strip_urls(text):
    text = _URL.sub('', text or '')
    text = _LONG_BARE.sub('', text)
    text = re.sub(r'[ \t]{2,}', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return text


def new_part(text):
    """The text the sender wrote, without a quoted chain. Signature stays, for the scanner."""
    text = _squash_ws(text)
    text = re.sub(r'(?i)\[image[^\]]*\]|\[cid:[^\]]+\]', '', text)
    lines = text.split('\n')
    cut = _quote_cut(lines)
    if cut is not None:
        lines = lines[:cut]
    kept = []
    for line in lines:
        if line.strip().startswith('>'):
            continue
        kept.append(line)
    return '\n'.join(kept)


def clean_display(text):
    """Display text: no signature, footer, image line, or URL."""
    lines = _squash_ws(text).split('\n')
    out = []
    for line in lines:
        s = line.strip()
        if not s:
            out.append('')
            continue
        if _SIG.match(s):
            break
        if s.startswith('>') or _footer_line(s) or _image_only(s):
            continue
        if _URL.fullmatch(s) or _LONG_BARE.fullmatch(s):
            continue
        out.append(_strip_urls(line).strip())
    text = '\n'.join(out)
    text = _strip_urls(text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def mask_codes(text):
    """Mask a 4–8 digit code that sits near a code or verify word. Other numbers stay."""
    if not text or not _CODE_WORD.search(text):
        return text or ''
    spans = [(m.start(), m.end()) for m in _CODE_WORD.finditer(text)]

    def near(start, end):
        for a, b in spans:
            if start <= b + CODE_WINDOW and end >= a - CODE_WINDOW:
                return True
        return False

    def repl(m):
        if near(m.start(), m.end()):
            return '\u2022' * len(m.group(1))
        return m.group(0)

    return _CODE_NUM.sub(repl, text)


def split_sentences(text):
    text = re.sub(r'\s+', ' ', (text or '')).strip()
    if not text:
        return []
    for i, abbr in enumerate(_ABBR):
        text = text.replace(abbr, abbr[:-1] + '<%d>' % i)
    parts = re.split(r'(?<=[.!?])\s+', text)
    out = []
    for part in parts:
        for i, abbr in enumerate(_ABBR):
            part = part.replace('<%d>' % i, '.')
        part = part.strip()
        if part:
            out.append(part)
    return out


def fit_sentence(sentence):
    if len(sentence) <= 320:
        return sentence
    cut = sentence[:320]
    for sep in ('. ', '; ', ', '):
        idx = cut.rfind(sep)
        if idx >= 80:
            return cut[:idx + (1 if sep == '. ' else 0)].rstrip()
    sp = cut.rfind(' ')
    if sp >= 80:
        return cut[:sp].rstrip() + '\u2026'
    return cut.rstrip() + '\u2026'


def body_lines(text):
    """Up to four sentences, each cut on a sentence boundary. Fewer when the mail is shorter."""
    lines = []
    for sentence in split_sentences(text):
        if len(lines) >= 4:
            break
        fitted = fit_sentence(sentence).strip()
        if fitted:
            lines.append(fitted)
    return lines


def one_line(text, limit):
    t = _squash_ws(text or '').replace('\n', ' ')
    t = re.sub(r'[\x00-\x1f\x7f\u2028\u2029]', '', t)
    t = re.sub(r'\s+', ' ', t).strip()
    t = t.replace('"', "'")
    if len(t) <= limit:
        return t
    chunk = t[:limit]
    if ' ' in chunk:
        chunk = chunk.rsplit(' ', 1)[0]
    return chunk.rstrip() + '\u2026'


def clean_addr(addr):
    m = re.search(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', addr or '')
    return m.group(0).lower() if m else ''


def attachment_names(raw):
    names = []
    seen = set()
    for item in raw or []:
        if isinstance(item, dict):
            item = item.get('name') or item.get('filename') or ''
        if not isinstance(item, str):
            continue
        if _URL.search(item) or _LONG_BARE.search(item):
            continue
        base = os.path.basename(item.replace('\\', '/')).strip()
        base = re.sub(r'[\x00-\x1f\u2028\u2029]', '', base)
        base = base.replace('"', '').replace("'", '')
        if not base or base in ('.', '..') or '/' in base or '\\' in base:
            continue
        if _URL.search(base) or len(base) > 80:
            continue
        key = base.lower()
        if key in seen:
            continue
        seen.add(key)
        names.append(base)
        if len(names) >= 8:
            break
    return names


# ---------- importance (own mode) ----------
def suspicious_reason(text):
    blob = _squash_ws(text or '')
    blob = re.sub(r'\s+', ' ', blob)
    if not blob.strip():
        return None
    for pattern, reason in _SUSPICIOUS:
        if pattern.search(blob):
            return reason
    return None


def _headers(payload):
    raw = payload.get('headers') or {}
    if not isinstance(raw, dict):
        return {}
    return {str(k).lower(): str(v) for k, v in raw.items() if v}


def _bulk(payload, text):
    headers = _headers(payload)
    if headers.get('list-unsubscribe') or headers.get('list-id'):
        return True
    prec = (headers.get('precedence') or '').lower()
    if prec in ('bulk', 'list', 'junk'):
        return True
    auto = (headers.get('auto-submitted') or '').lower()
    if auto and auto != 'no':
        return True
    addr = clean_addr(payload.get('from_addr') or '')
    local = addr.split('@', 1)[0] if addr else ''
    if local and _AUTO_LOCAL.match(local):
        return True
    if _BULK_PHRASE.search(text or ''):
        return True
    return False


def _importance(payload, text):
    """Return (tag, reason_or_empty). tag is important, normal, or suspicious."""
    subject = payload.get('subject') or ''
    scan = subject + '\n' + (text or '')
    reason = suspicious_reason(scan)
    if reason:
        return 'suspicious', reason
    low = scan.lower()
    if _SECURITY.search(low) or _VERIFY.search(low):
        return 'important', ''
    receipt = bool(_RECEIPT.search(low))
    payment = bool(_PAYMENT.search(low))
    deadline = bool(_DEADLINE.search(low))
    if payment and not (receipt and not re.search(r'(?i)failed|declined|past due|problem', low)):
        return 'important', ''
    if deadline and not receipt:
        return 'important', ''
    if _bulk(payload, scan) or receipt:
        return 'normal', ''
    # A named person with no bulk markers is the important case.
    if '_given_name' in payload:
        named = payload.get('_given_name') or ''
    else:
        named = one_line(payload.get('from_name') or '', 80)
    # A display fallback such as "someone" is not a person who wrote.
    if clean_addr(payload.get('from_addr') or '') or named:
        return 'important', ''
    return 'normal', ''


def safe_public_reason(text):
    """An agent-supplied reason may be shown. A reason that carries a link or the body may not."""
    if not isinstance(text, str):
        return None
    t = one_line(text, 160)
    if not t or _URL.search(t) or _LONG_BARE.search(t) or '<' in t:
        return None
    return mask_codes(t)


# ---------- RFC822 (own mode) ----------
def parse_raw(raw):
    if isinstance(raw, str):
        blob = raw.encode('utf-8', 'replace')
    elif isinstance(raw, (bytes, bytearray)):
        blob = bytes(raw)
    else:
        return {}
    if len(blob) > OWN_TEXT_CAP:
        blob = blob[:OWN_TEXT_CAP]
    try:
        msg = BytesParser(policy=email.policy.default).parsebytes(blob)
    except Exception:
        return {}
    name, addr = email.utils.parseaddr(str(msg.get('from') or ''))
    texts, htmls, files = [], [], []

    def take(part):
        if part.get_content_disposition() == 'attachment':
            fn = part.get_filename()
            if fn:
                files.append(str(fn))
            return
        ctype = part.get_content_type()
        try:
            content = part.get_content()
        except Exception:
            return
        if not isinstance(content, str):
            return
        if ctype == 'text/plain':
            texts.append(content)
        elif ctype == 'text/html':
            htmls.append(content)

    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_maintype() == 'multipart':
                continue
            take(part)
    else:
        take(msg)
    headers = {}
    for key in ('list-unsubscribe', 'list-id', 'precedence', 'auto-submitted'):
        val = msg.get(key)
        if val:
            headers[key] = str(val)
    text = '\n'.join(texts)
    html = '\n'.join(htmls)
    if len(text) > OWN_TEXT_CAP:
        text = text[:OWN_TEXT_CAP]
    if len(html) > OWN_TEXT_CAP:
        html = html[:OWN_TEXT_CAP]
    return {
        'from_name': name,
        'from_addr': addr,
        'subject': str(msg.get('subject') or ''),
        'text': text,
        'html': html,
        'attachments': files,
        'headers': headers,
        'message_id': str(msg.get('message-id') or '').strip(),
    }


def merge_payload(payload, source):
    data = dict(payload or {})
    raw = data.get('raw') if data.get('raw') not in (None, '') else data.get('rfc822')
    if raw:
        parsed = parse_raw(raw)
        for key, val in parsed.items():
            if data.get(key) in (None, '', [], {}):
                data[key] = val
    data.pop('raw', None)
    data.pop('rfc822', None)
    text = data.get('text') if isinstance(data.get('text'), str) else ''
    if not text and isinstance(data.get('body'), str):
        text = data.get('body') or ''
    html = data.get('html') if isinstance(data.get('html'), str) else ''
    if source == 'agent':
        html = ''
        if len(text) > AGENT_TEXT_CAP:
            text = text[:AGENT_TEXT_CAP]
    else:
        if len(text) > OWN_TEXT_CAP:
            text = text[:OWN_TEXT_CAP]
        if len(html) > OWN_TEXT_CAP:
            html = html[:OWN_TEXT_CAP]
    plain_ex, plain_hidden = fragment_to_text(text)
    html_ex, html_hidden = html_to_text(html) if html else ('', '')
    scan = '\n'.join((new_part(plain_ex), plain_hidden, new_part(html_ex), html_hidden))
    d_plain = clean_display(new_part(plain_ex))
    d_html = clean_display(new_part(html_ex)) if html_ex else ''
    display = d_plain if _letter_count(d_plain) >= _letter_count(d_html) else d_html
    display = mask_codes(display)
    data['from_addr'] = clean_addr(data.get('from_addr') or '')
    name = one_line(data.get('from_name') or '', 80).replace('<', '').replace('>', '')
    data['_given_name'] = name
    data['from_name'] = name or (data['from_addr'].split('@', 1)[0] if data['from_addr'] else 'someone')
    subject = mask_codes(_strip_urls(one_line(data.get('subject') or '', 120))).replace('<', '').replace('>', '')
    data['subject'] = subject or '(no subject)'
    data['_scan'] = _squash_ws(scan)
    data['_display'] = display
    data['_files'] = attachment_names(data.get('attachments'))
    reply_to = str(data.get('message_id') or data.get('reply_to') or data.get('id') or '').strip()
    reply_to = one_line(reply_to, 200)
    data['_reply_to'] = reply_to
    return data


def _letter_count(text):
    return len(re.findall(r'[A-Za-z]', text or ''))


# ---------- cards ----------
def render_card(name, subject, lines, files, open_link, suspicious, reason):
    if suspicious:
        head = WARN_MARK + ' ' + name + SEP + 'suspicious: ' + subject
        return head + '\n' + (reason or 'Flagged as suspicious. The body was withheld.')
    parts = [CARD_MARK + ' ' + name + SEP + subject]
    parts.extend(lines or [])
    if files:
        parts.append(CLIP_MARK + ' ' + ', '.join(files))
    if open_link:
        parts.append('Open: ' + open_link)
    return '\n'.join(parts)


def safe_open_link(url, source):
    if source != 'agent':
        return None
    if not isinstance(url, str):
        return None
    url = url.strip()
    if len(url) > 500 or not re.match(r'https?://[^\s]+$', url):
        return None
    return url


def confirm_line(name, addr, text):
    shown = one_line(text, CONFIRM_SHOW_CAP)
    return 'Reply to %s <%s>: "%s"? Reply yes within 2 min.' % (name, addr, shown)


def sent_line(name, source):
    if source == 'agent':
        return '\u2709\ufe0f sent for approval'
    return '\u2192 %s \u2709\ufe0f sent' % name


def build_effect(card, text, source, agent_id):
    text = one_line(text, REPLY_TEXT_CAP)
    if source == 'agent':
        return {
            'action': 'handoff_reply',
            'agent': agent_id,
            'message': {
                'id': card['email_id'],
                'reply_to': card['reply_to'],
                'to': card['from_addr'],
                'from_name': card['from_name'],
                'text': text,
            },
        }
    return {
        'action': 'send_email_reply',
        'reply_to': card['reply_to'],
        'to': card['from_addr'],
        'text': text,
    }


# ---------- state files (mode 600, no bodies) ----------
def ensure_dir(path):
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def atomic_write(path, text):
    folder = os.path.dirname(path) or '.'
    ensure_dir(folder)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix='.tmp-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            fh.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def read_json(path, default):
    try:
        with open(path, encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return default
    return data


def log_counts(state_dir, **counts):
    bits = []
    for key in LOG_KEYS:
        if key not in counts:
            continue
        try:
            n = int(counts[key])
        except (TypeError, ValueError):
            continue
        if n:
            bits.append('%s=%s' % (key, n))
    if not bits:
        return
    line = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()) + ' ' + ' '.join(bits) + '\n'
    path = os.path.join(state_dir, 'mail.log')
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode('utf-8'))
        finally:
            os.close(fd)
        os.chmod(path, 0o600)
    except OSError:
        pass


class StateLock:
    def __init__(self, directory):
        ensure_dir(directory)
        self.fh = open(os.path.join(directory, '.lock'), 'a')

    def __enter__(self):
        fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        try:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
        finally:
            self.fh.close()


class Dedupe:
    def __init__(self, path, ttl=7 * 86400, now=None):
        self.path = path
        self.ttl = ttl
        self.now = now or time.time

    def _read(self):
        data = read_json(self.path, {})
        return data if isinstance(data, dict) else {}

    def seen(self, ident):
        if not ident:
            return False
        got = self._read().get(ident)
        if got is None:
            return False
        try:
            return self.now() - float(got) <= self.ttl
        except (TypeError, ValueError):
            return False

    def commit(self, ids):
        ids = [str(i) for i in ids if i]
        if not ids:
            return
        folder = os.path.dirname(self.path) or '.'
        ensure_dir(folder)
        lock_path = self.path + '.lock'
        with open(lock_path, 'a') as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            data = self._read()
            now = self.now()
            for ident in ids:
                data[ident] = now
            cut = now - self.ttl
            data = {k: v for k, v in data.items() if isinstance(v, (int, float)) and v >= cut}
            atomic_write(self.path, json.dumps(data))


class KeyCache:
    """Conversation key events and public signing keys only. Never a message body. Mode 600, 24 h."""

    def __init__(self, path, ttl=KEY_CACHE_TTL_S, now=None):
        self.path = path
        self.ttl = ttl
        self.now = now or time.time

    def load(self, conv):
        try:
            if os.path.getsize(self.path) > KEY_CACHE_MAX_BYTES:
                return None
        except OSError:
            return None
        data = read_json(self.path, None)
        if not isinstance(data, dict) or data.get('conversation_id') != conv:
            return None
        try:
            age = self.now() - float(data.get('savedAt') or 0)
        except (TypeError, ValueError):
            return None
        if not (0 <= age <= self.ttl):
            return None
        cke, sk = data.get('conversation_key_events'), data.get('signing_keys')
        if not cke or not sk or not isinstance(cke, list) or not isinstance(sk, list):
            return None
        return {'conversation_key_events': cke, 'signing_keys': sk}

    def save(self, conv, cke, sk):
        data = json.dumps({
            'v': 1, 'conversation_id': conv, 'savedAt': self.now(),
            'conversation_key_events': cke, 'signing_keys': sk,
        })
        if len(data) > KEY_CACHE_MAX_BYTES:
            return False
        atomic_write(self.path, data)
        return True

    def clear(self):
        try:
            os.remove(self.path)
        except OSError:
            pass


def _cards_path(state):
    return os.path.join(state, 'cards.json')


def _pending_path(state):
    return os.path.join(state, 'pending.json')


def _events_path(state):
    return os.path.join(state, 'seen_events.json')


def load_cards(state):
    data = read_json(_cards_path(state), {'next': 1, 'cards': {}})
    if not isinstance(data, dict):
        data = {'next': 1, 'cards': {}}
    if not isinstance(data.get('cards'), dict):
        data['cards'] = {}
    try:
        data['next'] = int(data.get('next') or 1)
    except (TypeError, ValueError):
        data['next'] = 1
    return data


def save_cards(state, data):
    atomic_write(_cards_path(state), json.dumps(data))


def load_pending(state):
    data = read_json(_pending_path(state), None)
    return data if isinstance(data, dict) else None


def save_pending(state, data):
    atomic_write(_pending_path(state), json.dumps(data))


def clear_pending(state):
    try:
        os.remove(_pending_path(state))
    except OSError:
        pass


def load_seen_events(state):
    data = read_json(_events_path(state), {'ids': []})
    if not isinstance(data, dict) or not isinstance(data.get('ids'), list):
        return {'ids': []}
    data['ids'] = [str(i) for i in data['ids'] if i][-SEEN_EVENT_CAP:]
    return data


def save_seen_events(state, data):
    data['ids'] = [str(i) for i in data.get('ids') or [] if i][-SEEN_EVENT_CAP:]
    atomic_write(_events_path(state), json.dumps({'ids': data['ids']}))


def mark_seen_events(store, keys):
    cur = list(store.get('ids') or [])
    have = set(cur)
    for key in keys:
        key = str(key or '')
        if key and key not in have:
            cur.append(key)
            have.add(key)
    store['ids'] = cur[-SEEN_EVENT_CAP:]


def unsee_event(store, key):
    key = str(key or '')
    store['ids'] = [i for i in store.get('ids') or [] if i != key]


# ---------- xchat_lite (copied flow: stdin only, key cache, send plan) ----------
def locate_helper(helper):
    py = (helper or {}).get('python')
    script = (helper or {}).get('script')
    if py and script:
        return os.path.expanduser(str(py)), os.path.expanduser(str(script))
    home = os.path.expanduser('~')
    roots = (
        os.path.join(home, 'xchat-grokbot-helper'),
        os.path.join(os.getcwd(), 'xchat-grokbot-helper'),
        os.path.join(home, 'xchat-lite'),
        os.path.join(os.getcwd(), 'xchat-lite'),
    )
    for root in roots:
        sp = os.path.join(root, 'xchat_lite.py')
        venv = os.path.join(root, '.venv', 'bin', 'python')
        if os.path.exists(sp):
            return (venv if os.path.exists(venv) else sys.executable), sp
    return None, None


def _helper_base(helper):
    py, script = locate_helper(helper)
    if not py or not script:
        raise MailError('xchat_lite_missing')
    base = [py, script]
    h = helper or {}
    for flag, key in (('--user-id', 'user_id'), ('--key-version', 'key_version'), ('--juicebox', 'juicebox')):
        if h.get(key):
            val = os.path.expanduser(str(h[key])) if key == 'juicebox' else str(h[key])
            base += [flag, val]
    return base


def _default_runner(helper):
    env = dict(os.environ)
    h = helper or {}
    if h.get('xdg_config_home'):
        env['XDG_CONFIG_HOME'] = os.path.expanduser(str(h['xdg_config_home']))
    for key, val in (h.get('env') or {}).items():
        text = str(val)
        if str(key).upper() == 'XDG_CONFIG_HOME' or text.startswith('~') or '/' in text:
            text = os.path.expanduser(text)
        env[str(key)] = text

    def run(argv, input=b''):
        return subprocess.run(argv, input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)

    return run


def _run(argv, stdin_obj, runner):
    # The card or a decrypted row is in stdin_obj. Never put it on argv, and never
    # return helper stderr (it can echo the text). Callers only see a fixed code.
    proc = runner(argv, input=json.dumps(stdin_obj).encode('utf-8'))
    out = proc.stdout.decode('utf-8') if isinstance(proc.stdout, (bytes, bytearray)) else (proc.stdout or '')
    if proc.returncode != 0:
        raise MailError('xchat_lite_failed')
    try:
        data = json.loads(out)
    except ValueError:
        raise MailError('xchat_lite_failed')
    if not isinstance(data, dict):
        raise MailError('xchat_lite_failed')
    return data


def session_encrypt_stdin(conversation_id, text, xchat, reply_to=None, should_notify=False):
    body = {
        'conversation_id': conversation_id,
        'text': text,
        'events': (xchat or {}).get('events') or [],
        'conversation_key_events': (xchat or {}).get('conversation_key_events') or [],
        'signing_keys': (xchat or {}).get('signing_keys') or [],
        'should_notify': bool(should_notify),
    }
    prep = (xchat or {}).get('prepare')
    if prep:
        body['prepare'] = prep
    if reply_to:
        body['reply_to'] = str(reply_to)
    return body


def encrypt_line(conversation_id, text, xchat, helper, runner, reply_to=None, should_notify=False):
    base = _helper_base(helper)
    res = _run(base + ['session-encrypt'],
               session_encrypt_stdin(conversation_id, text, xchat, reply_to, should_notify), runner)
    return {
        'conversation_id': str(res.get('id') or conversation_id),
        'message_id': res.get('message_id') or str(uuid.uuid4()),
        'encoded_message_create_event': res.get('encoded_message_create_event'),
        'encoded_message_event_signature': res.get('encoded_message_event_signature'),
        'add_conversation_keys': res.get('add_conversation_keys') if res.get('needs_add_conversation_keys_before_send') else None,
        'should_notify': bool(should_notify),
        'reply_to': str(reply_to) if reply_to else None,
    }


def decrypt_events(events, xchat, helper, runner):
    if not events:
        return []
    base = _helper_base(helper)
    res = _run(base + ['decrypt'], {
        'events': list(events),
        'conversation_key_events': (xchat or {}).get('conversation_key_events') or [],
        'signing_keys': (xchat or {}).get('signing_keys') or [],
    }, runner)
    rows = res.get('messages') or []
    return rows if isinstance(rows, list) else []


def resolve_xchat(req, conv, cache):
    """Return (xchat, from_cache). Raise a fetch result via MailError? Callers check the dict."""
    xchat = dict(req.get('xchat') or {})
    supplied = bool(xchat.get('conversation_key_events') and xchat.get('signing_keys'))
    if supplied or xchat.get('prepare'):
        return xchat, False, True
    if req.get('key_cache') is False:
        return xchat, False, False
    cached = cache.load(conv) if cache and conv else None
    if not cached:
        return xchat, False, False
    xchat.update(cached)
    return xchat, True, True


def _crypto_failure(cache, from_cache, kind):
    if from_cache and cache:
        cache.clear()
        return {'action': 'fetch', 'need': list(NEED_KEYS), 'posted_kind': kind,
                'reason': 'no_cached_keys'}
    return {'action': 'error', 'reason': 'xchat_lite_failed', 'encrypted': False, 'posted_kind': kind}


def _save_keys(cache, conv, xchat, supplied):
    if supplied and cache and conv:
        try:
            cache.save(conv, xchat['conversation_key_events'], xchat['signing_keys'])
        except OSError:
            pass


# ---------- config ----------
def load_config(state_dir):
    path = os.path.join(state_dir, 'config.json')
    data = read_json(path, {})
    return data if isinstance(data, dict) else {}


def _merge(base, over):
    out = dict(base or {})
    for key, val in (over or {}).items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], val)
        elif val not in (None, ''):
            out[key] = val
    return out


def apply_config(req):
    req = dict(req or {})
    state = os.path.expanduser(str(req.get('state_dir') or STATE_DIR))
    # config lives in the state dir. A state_dir inside the request wins over the file's copy.
    file_cfg = load_config(state)
    merged = _merge(file_cfg, req)
    merged['state_dir'] = os.path.expanduser(str(merged.get('state_dir') or state))
    source = str(merged.get('mailSource') or 'own').strip().lower()
    mode = str(merged.get('mailMode') or 'important').strip().lower()
    merged['mailSource'] = source
    merged['mailMode'] = mode
    return merged


def _now(req, now):
    if now is not None:
        return float(now)
    if req.get('now') is not None:
        try:
            return float(req.get('now'))
        except (TypeError, ValueError):
            pass
    return time.time()


def _sha(text):
    return hashlib.sha256((text or '').encode('utf-8')).hexdigest()


def same_conv(a, b):
    def parts(value):
        value = str(value or '').replace(':', '-').strip()
        bits = [p for p in value.split('-') if p]
        return set(bits) if len(bits) == 2 else {value}
    if not a or not b:
        return True
    return parts(a) == parts(b)


# ---------- inbound ----------
def _reserve_card(state, fields):
    data = load_cards(state)
    for card in data['cards'].values():
        if card.get('email_id') == fields['email_id']:
            card.update(fields)
            save_cards(state, data)
            return card
    n = int(data.get('next') or 1)
    fields = dict(fields)
    fields['n'] = n
    data['cards'][str(n)] = fields
    data['next'] = n + 1
    save_cards(state, data)
    return fields


def build_plaintext(data, source, tag, reason):
    open_link = safe_open_link(data.get('link'), source)
    if tag == 'suspicious':
        # `reason` is already the line to show. Do not fall back to the raw payload:
        # an instruction upgrade must replace a mild agent reason.
        public = safe_public_reason(reason) or 'Flagged as suspicious. The body was withheld.'
        text = render_card(data['from_name'], data['subject'], [], [], None, True, public)
        return text, public
    lines = body_lines(data.get('_display') or '')
    # Final pass: a URL must not survive inside a body line.
    lines = [_strip_urls(mask_codes(line)).strip() for line in lines]
    lines = [line for line in lines if line]
    text = render_card(data['from_name'], data['subject'], lines, data.get('_files') or [], open_link, False, '')
    return text, ''


def handle_inbound(req, runner, now):
    source = req.get('mailSource') or 'own'
    mode = req.get('mailMode') or 'important'
    if source not in ('own', 'agent'):
        raise MailError('bad_source')
    if mode not in ('important', 'all'):
        raise MailError('bad_mode')
    payload = req.get('payload') or {}
    if not isinstance(payload, dict):
        raise MailError('bad_payload')
    email_id = str(payload.get('id') or '').strip()
    if not email_id or len(email_id) > 200 or re.search(r'\s', email_id):
        raise MailError('missing_id')
    state = req['state_dir']
    dedupe = Dedupe(req.get('dedupe_path') or os.path.join(state, 'seen_ids.json'),
                    req.get('ttl_seconds', 7 * 86400), now=lambda: now)
    if dedupe.seen(email_id):
        log_counts(state, duplicate=1)
        return {'action': 'skip', 'reason': 'duplicate', 'posted_kind': 'duplicate'}

    data = merge_payload(payload, source)
    forced = suspicious_reason((data.get('subject') or '') + '\n' + (data.get('_scan') or ''))
    if source == 'agent':
        tag = str(payload.get('tag') or '').strip().lower()
        if tag not in ('important', 'suspicious', 'normal'):
            raise MailError('bad_tag')
        # The excerpt is still untrusted mail. An instruction upgrades any tag.
        if forced:
            tag = 'suspicious'
            reason = forced
        elif tag == 'suspicious':
            reason = safe_public_reason(payload.get('reason')) or (
                'Flagged as suspicious. The body was withheld.')
        else:
            reason = ''
    else:
        # Own mode classifies. A tag on the payload is the mail trying to label itself.
        tag, reason = _importance(data, data.get('_scan') or '')
        if forced:
            tag, reason = 'suspicious', forced
    if tag == 'normal' and mode != 'all':
        dedupe.commit([email_id])
        log_counts(state, normal=1)
        return {'action': 'skip', 'reason': 'normal', 'tag': 'normal', 'posted_kind': 'normal'}
    if tag == 'normal':
        tag = 'important'  # mailMode all still posts a normal card in the important shape
        posted_tag = 'normal'
    else:
        posted_tag = tag

    conv = str(req.get('conversation_id') or '').strip()
    if not conv:
        raise MailError('missing_conversation')
    cache = None if req.get('key_cache') is False else KeyCache(
        req.get('key_cache_path') or os.path.join(state, 'xchat_keys.json'), now=lambda: now)
    xchat, from_cache, ready = resolve_xchat(req, conv, cache)
    if not ready:
        log_counts(state, fetch=1)
        return {'action': 'fetch', 'need': list(NEED_KEYS), 'posted_kind': posted_tag,
                'reason': 'no_cached_keys'}

    text, _public = build_plaintext(data, source, tag if posted_tag != 'normal' else 'important', reason)
    if posted_tag == 'normal':
        # Posted because mailMode is all. Still the normal card shape, not a warning.
        text, _public = build_plaintext(data, source, 'important', '')
    fields = {
        'email_id': email_id,
        'from_name': data['from_name'],
        'from_addr': data['from_addr'],
        'subject': data['subject'],
        'reply_to': data.get('_reply_to') or email_id,
        'message_id': '',
        'sequence_id': '',
        'text_sha256': _sha(text),
        'suspicious': posted_tag == 'suspicious',
        'source': source,
    }
    try:
        send = encrypt_line(conv, text, xchat, req.get('helper'), runner, should_notify=True)
    except MailError as err:
        if err.code in ('xchat_lite_failed', 'xchat_lite_missing') and from_cache:
            return _crypto_failure(cache, True, posted_tag)
        if err.code == 'xchat_lite_missing':
            raise
        return _crypto_failure(cache, from_cache, posted_tag)
    # The card map is written only after encrypt succeeds, so a failed send leaves no card.
    fields['message_id'] = send.get('message_id') or ''
    card = _reserve_card(state, fields)
    supplied = bool((req.get('xchat') or {}).get('conversation_key_events') and
                    (req.get('xchat') or {}).get('signing_keys'))
    _save_keys(cache, conv, xchat, supplied)
    if not req.get('defer_commit'):
        dedupe.commit([email_id])
    log_key = 'suspicious' if posted_tag == 'suspicious' else ('normal' if posted_tag == 'normal' else 'important')
    log_counts(state, **{log_key: 1})
    result = {
        'action': 'send',
        'phase': 'card',
        'sends': [send],
        'encrypted': True,
        'posted_kind': posted_tag,
        'tag': posted_tag,
        'card_n': card['n'],
        'ack_ids': [email_id],
    }
    if posted_tag == 'suspicious':
        result['suspicious'] = True
    return result


# ---------- replies ----------
def _actionable(content):
    """Reactions, edits, and read receipts are not the owner composing a reply."""
    if not isinstance(content, dict):
        return True
    kind = content.get('content_type')
    if kind and kind != 'Text':
        return False
    return True


def normalize_watch_message(row):
    if not isinstance(row, dict):
        return None
    if isinstance(row.get('event'), dict):
        ev = row['event']
        if ev.get('type') not in (None, 'Message'):
            return None
        content = ev.get('content') or {}
        preview = content.get('replying_to_preview') or {}
        seq = preview.get('replying_to_message_sequence_id')
        return {
            'sequence_id': '' if ev.get('sequence_id') is None else str(ev.get('sequence_id')),
            'id': '' if ev.get('id') is None else str(ev.get('id')),
            'sender_id': '' if ev.get('sender_id') is None else str(ev.get('sender_id')),
            'conversation_id': '' if ev.get('conversation_id') is None else str(ev.get('conversation_id')),
            'created_at_msec': ev.get('created_at_msec'),
            'text': row.get('text') if isinstance(row.get('text'), str) else '',
            'status': row.get('status') or '',
            'reply_to_sequence_id': '' if not seq else str(seq),
            'actionable': _actionable(content),
        }
    if 'text' not in row and 'sender_id' not in row:
        return None
    seq = row.get('reply_to_sequence_id')
    return {
        'sequence_id': '' if row.get('sequence_id') is None else str(row.get('sequence_id')),
        'id': '' if row.get('id') is None else str(row.get('id')),
        'sender_id': '' if row.get('sender_id') is None else str(row.get('sender_id')),
        'conversation_id': '' if row.get('conversation_id') is None else str(row.get('conversation_id')),
        'created_at_msec': row.get('created_at_msec'),
        'text': row.get('text') if isinstance(row.get('text'), str) else '',
        'status': row.get('status') or '',
        'reply_to_sequence_id': '' if not seq else str(seq),
        'actionable': row.get('actionable', True) is not False,
    }


def event_keys(message):
    keys = []
    for field in ('sequence_id', 'id'):
        val = str(message.get(field) or '')
        if val and val not in keys:
            keys.append(val)
    return keys


def _trusted(message):
    return message.get('status') in ('verified', 'unencrypted')


def _chatter(text):
    """System lines from xchat_lite. These are not the owner typing a reply."""
    t = (text or '').strip().lower()
    return t.startswith((
        'reacted ', 'removed reaction ', 'edited ', 'pinned ', 'unpinned ',
        '[photo]', '[file:', '[call', '[added', '[deleted', '[could not',
        '[empty message]', '[forwarded]', '[draft]', '[missed ',
        '[recorded', '[took a screenshot]', '[unsupported',
        '[message failed', '[conversation key',
    ))


def is_yes(text):
    t = (text or '').strip().lower().strip('!. ')
    return t in ('yes', 'yes please')


def is_no(text):
    t = (text or '').strip().lower().strip('!. ')
    return t in ('no', 'nope', 'cancel', 'stop')


def parse_intent(message):
    text = (message.get('text') or '').strip()
    if not text or _chatter(text):
        return None
    cmd = _REPLY_CMD.match(text)
    if cmd:
        return {
            'kind': 'command',
            'n': int(cmd.group(1)),
            'text': cmd.group(2).strip(),
            'keys': event_keys(message),
            'sequence_id': message.get('sequence_id') or '',
        }
    seq = message.get('reply_to_sequence_id') or ''
    if seq:
        return {
            'kind': 'native',
            'sequence': str(seq),
            'text': text,
            'keys': event_keys(message),
            'sequence_id': message.get('sequence_id') or '',
        }
    return None


def resolve_card(intent, cards):
    if intent['kind'] == 'command':
        card = cards.get(str(intent['n']))
        return card, (None if card else 'unknown')
    for card in cards.values():
        if card.get('sequence_id') and str(card['sequence_id']) == str(intent['sequence']):
            return card, None
    return None, 'not_a_card'


def fresh_yes(message, pending):
    """A yes counts only when it is a new event from after the confirm was shown."""
    keys = set(event_keys(message))
    if not keys:
        return False
    known = set(str(k) for k in (pending.get('known_ids') or []))
    if keys & known:
        return False
    confirm_at = pending.get('confirm_at')
    if not confirm_at:
        return False
    ts = message.get('created_at_msec')
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return False
    return ts >= int(float(confirm_at) * 1000) - 2000


def _cap_ids(ids, cap=KNOWN_ID_CAP):
    out = []
    seen = set()
    for item in ids or []:
        item = str(item or '')
        if item and item not in seen:
            out.append(item)
            seen.add(item)
    return out[-cap:]


def bind_cards(cards, messages, sender_id):
    changed = False
    for message in messages:
        if not _trusted(message):
            continue
        if sender_id and message.get('sender_id') != sender_id:
            continue
        for card in cards.values():
            if card.get('sequence_id'):
                continue
            mid = str(message.get('id') or '')
            if card.get('message_id') and mid and card['message_id'] == mid:
                card['sequence_id'] = str(message.get('sequence_id') or '')
                changed = True
                continue
            text = message.get('text') or ''
            if text and card.get('text_sha256') and _sha(text) == card['text_sha256']:
                card['sequence_id'] = str(message.get('sequence_id') or '')
                if mid and not card.get('message_id'):
                    card['message_id'] = mid
                changed = True
    return changed


def _fixed_send(req, runner, text, xchat, from_cache, cache, conv, reply_to, kind):
    try:
        send = encrypt_line(conv, text, xchat, req.get('helper'), runner,
                            reply_to=reply_to, should_notify=True)
    except MailError as err:
        if err.code == 'xchat_lite_missing':
            raise
        return None, _crypto_failure(cache, from_cache, kind)
    return send, None


def handle_watch(req, runner, now):
    state = req['state_dir']
    owner = str(req.get('owner_user_id') or '').strip()
    sender = str(req.get('sender_user_id') or '').strip()
    conv = str(req.get('conversation_id') or '').strip()
    if not owner:
        raise MailError('missing_owner')
    if not sender:
        raise MailError('missing_sender')
    if owner == sender:
        raise MailError('owner_is_sender')
    if not conv:
        raise MailError('missing_conversation')
    source = req.get('mailSource') or 'own'
    if source not in ('own', 'agent'):
        raise MailError('bad_source')
    agent_id = str(req.get('agent_id') or '').strip()
    if source == 'agent' and not agent_id:
        raise MailError('missing_agent')

    cache = None if req.get('key_cache') is False else KeyCache(
        req.get('key_cache_path') or os.path.join(state, 'xchat_keys.json'), now=lambda: now)
    supplied = bool((req.get('xchat') or {}).get('conversation_key_events') and
                    (req.get('xchat') or {}).get('signing_keys'))
    xchat, from_cache, ready = resolve_xchat(req, conv, cache)
    messages = []
    # A present events key wins. An empty list must not fall through to plaintext messages.
    if 'events' in req:
        events = req.get('events') if isinstance(req.get('events'), list) else []
        if events and not ready:
            log_counts(state, fetch=1)
            return {'action': 'fetch', 'need': list(NEED_KEYS), 'reason': 'no_cached_keys',
                    'posted_kind': 'watch'}
        if events:
            xchat.setdefault('events', list(events))
            try:
                messages = decrypt_events(events, xchat, req.get('helper'), runner)
            except MailError as err:
                if err.code == 'xchat_lite_missing':
                    raise
                return _crypto_failure(cache, from_cache, 'watch')
        if supplied:
            _save_keys(cache, conv, xchat, True)
    else:
        messages = req.get('messages') if isinstance(req.get('messages'), list) else []
        if supplied:
            _save_keys(cache, conv, xchat, True)

    rows = []
    for row in messages:
        norm = normalize_watch_message(row)
        if not norm:
            continue
        if norm.get('conversation_id') and not same_conv(norm['conversation_id'], conv):
            continue
        rows.append(norm)

    book = load_cards(state)
    if bind_cards(book['cards'], rows, sender):
        save_cards(state, book)

    def need_keys():
        if ready:
            return None
        log_counts(state, fetch=1)
        return {'action': 'fetch', 'need': list(NEED_KEYS), 'reason': 'no_cached_keys',
                'posted_kind': 'watch'}

    def thread_of(seq):
        # xchat_lite rejects a reply_to that is not in this request's events.
        if 'events' not in req or not seq:
            return None
        seq = str(seq)
        for row in rows:
            if seq == row.get('sequence_id') or seq == row.get('id'):
                return seq
        return None

    def send_fixed(text, seq):
        err = need_keys()
        if err:
            return None, err
        return _fixed_send(req, runner, text, xchat, from_cache, cache, conv, thread_of(seq), 'watch')

    pending = load_pending(state)
    if pending and pending.get('phase') == 'proposed':
        if now - float(pending.get('proposed_at') or 0) > PROPOSAL_TTL_S:
            clear_pending(state)
            pending = None
            log_counts(state, reply_expired=1)

    seen = load_seen_events(state)
    seen_set = set(seen.get('ids') or [])
    batch_keys = []
    for row in rows:
        batch_keys.extend(event_keys(row))

    def unseen(row):
        keys = event_keys(row)
        return bool(keys) and not any(k in seen_set for k in keys)

    def finish_seen(extra=()):
        mark_seen_events(seen, list(batch_keys) + list(extra))
        save_seen_events(state, seen)

    def stamp(row):
        ts = row.get('created_at_msec')
        if isinstance(ts, bool) or not isinstance(ts, (int, float)):
            return 0
        return ts

    if pending and pending.get('phase') == 'awaiting_ack':
        confirm_at = float(pending.get('confirm_at') or 0)
        finish_seen()
        if confirm_at and now > confirm_at + YES_WINDOW_S:
            clear_pending(state)
            log_counts(state, reply_expired=1)
            return {'action': 'skip', 'reason': 'expired', 'posted_kind': 'watch'}
        # The effect was already returned once. Do not include the reply text again.
        return {'action': 'skip', 'reason': 'awaiting_ack', 'effect_id': pending.get('effect_id'),
                'posted_kind': 'watch'}

    owner_rows = [r for r in rows if r.get('sender_id') == owner and _trusted(r)]
    owner_rows.sort(key=lambda r: (stamp(r), r.get('sequence_id') or ''))
    new_rows = [r for r in owner_rows if unseen(r)]

    def speakable(row):
        if row.get('actionable') is False:
            return False
        if _chatter(row.get('text') or ''):
            return False
        return True

    if pending and pending.get('phase') == 'proposed':
        err = need_keys()
        if err:
            return err
        # Ids seen while the confirm is still only proposed can never approve it.
        pending['known_ids'] = _cap_ids(list(pending.get('known_ids') or []) + batch_keys)
        save_pending(state, pending)
        card = book['cards'].get(str(pending.get('card_n')))
        if not card or not card.get('from_addr'):
            clear_pending(state)
            finish_seen()
            return {'action': 'skip', 'reason': 'unknown_card', 'posted_kind': 'watch'}
        line = confirm_line(card['from_name'], card['from_addr'], pending.get('text') or '')
        send, err = _fixed_send(
            req, runner, line, xchat, from_cache, cache, conv,
            thread_of(pending.get('owner_sequence_id')), 'watch')
        if err:
            return err
        finish_seen()
        return {'action': 'send', 'phase': 'confirm', 'sends': [send],
                'confirm_id': pending.get('confirm_id'), 'card_n': card['n'],
                'encrypted': True, 'posted_kind': 'watch'}

    if pending and pending.get('phase') == 'posted':
        confirm_at = float(pending.get('confirm_at') or 0)
        if not confirm_at or now > confirm_at + YES_WINDOW_S:
            late = None
            for row in new_rows:
                if not speakable(row):
                    continue
                text = row.get('text') or ''
                if (is_yes(text) or is_no(text)) and fresh_yes(row, pending):
                    late = row
                    break
            if late is not None:
                # Keys are required to say the window closed. Leave the pending in place until then.
                if is_no(late.get('text') or ''):
                    line, phase, log_key = 'Not sent.', 'cancelled', 'reply_cancelled'
                else:
                    line, phase, log_key = 'Too late to send.', 'expired', 'reply_expired'
                send, err = send_fixed(line, late.get('sequence_id'))
                if err:
                    return err
                clear_pending(state)
                finish_seen()
                log_counts(state, **{log_key: 1})
                return {'action': 'send', 'phase': phase, 'sends': [send], 'encrypted': True,
                        'posted_kind': 'watch'}
            clear_pending(state)
            pending = None
            log_counts(state, reply_expired=1)

    for row in new_rows:
        if not speakable(row):
            continue
        text = row.get('text') or ''
        if pending and pending.get('phase') == 'posted':
            if not (is_yes(text) or is_no(text)) or not fresh_yes(row, pending):
                continue
            if is_no(text):
                send, err = send_fixed('Not sent.', row.get('sequence_id'))
                if err:
                    return err
                clear_pending(state)
                finish_seen()
                log_counts(state, reply_cancelled=1)
                return {'action': 'send', 'phase': 'cancelled', 'sends': [send], 'encrypted': True,
                        'posted_kind': 'watch'}
            card = book['cards'].get(str(pending.get('card_n')))
            if not card:
                finish_seen()
                clear_pending(state)
                return {'action': 'skip', 'reason': 'unknown_card', 'posted_kind': 'watch'}
            effect = build_effect(card, pending.get('text') or '', source, agent_id)
            effect_id = 'e_' + uuid.uuid4().hex[:12]
            effect['effect_id'] = effect_id
            # Encrypt first. A failure must leave the yes unseen and the confirm posted.
            send, err = send_fixed(sent_line(card['from_name'], source), row.get('sequence_id'))
            if err:
                return err
            pending['phase'] = 'awaiting_ack'
            pending['effect_id'] = effect_id
            pending['yes_keys'] = event_keys(row)
            save_pending(state, pending)
            finish_seen()
            log_counts(state, reply_approved=1)
            return {'action': 'send', 'phase': 'approved', 'sends': [send], 'effect': effect,
                    'effect_id': effect_id, 'encrypted': True, 'posted_kind': 'watch'}
        if pending:
            continue
        intent = parse_intent(row)
        if not intent:
            continue
        if not one_line(intent.get('text') or '', REPLY_TEXT_CAP):
            continue
        card, problem = resolve_card(intent, book['cards'])
        seq = row.get('sequence_id')
        if problem == 'unknown':
            send, err = send_fixed(WARN_MARK + ' No message %s to reply to.' % intent['n'], seq)
            if err:
                return err
            finish_seen()
            return {'action': 'send', 'phase': 'unknown_card', 'sends': [send], 'encrypted': True,
                    'posted_kind': 'watch'}
        if problem == 'not_a_card' or card is None:
            continue
        if card.get('suspicious'):
            send, err = send_fixed(WARN_MARK + ' Not replying to a suspicious message.', seq)
            if err:
                return err
            finish_seen()
            return {'action': 'send', 'phase': 'refused', 'sends': [send], 'encrypted': True,
                    'posted_kind': 'watch'}
        if not card.get('from_addr'):
            send, err = send_fixed(WARN_MARK + ' That message has no reply address.', seq)
            if err:
                return err
            finish_seen()
            return {'action': 'send', 'phase': 'no_address', 'sends': [send], 'encrypted': True,
                    'posted_kind': 'watch'}
        reply_text = one_line(intent['text'], REPLY_TEXT_CAP)
        line = confirm_line(card['from_name'], card['from_addr'], reply_text)
        send, err = send_fixed(line, seq)
        if err:
            return err
        confirm_id = 'c_' + uuid.uuid4().hex[:12]
        save_pending(state, {
            'confirm_id': confirm_id,
            'card_n': card['n'],
            'from_name': card['from_name'],
            'to': card['from_addr'],
            'reply_to': card['reply_to'],
            'text': reply_text,
            'proposed_at': now,
            'confirm_at': None,
            'phase': 'proposed',
            'known_ids': _cap_ids(batch_keys),
            'owner_sequence_id': seq or '',
            'source': source,
        })
        finish_seen()
        return {'action': 'send', 'phase': 'confirm', 'sends': [send], 'confirm_id': confirm_id,
                'card_n': card['n'], 'encrypted': True, 'posted_kind': 'watch'}

    finish_seen()
    return {'action': 'skip', 'reason': 'quiet', 'posted_kind': 'watch'}


def confirm_posted(req, now):
    state = req['state_dir']
    pending = load_pending(state)
    if not pending or pending.get('phase') not in ('proposed', 'posted'):
        return {'action': 'error', 'reason': 'no_pending'}
    want = str(req.get('confirm_id') or '')
    if want and want != str(pending.get('confirm_id') or ''):
        return {'action': 'error', 'reason': 'no_pending'}
    if not pending.get('confirm_at'):
        pending['confirm_at'] = now
    pending['phase'] = 'posted'
    save_pending(state, pending)
    return {'action': 'confirm_posted', 'confirm_id': pending.get('confirm_id')}


def ack_reply(req):
    clear_pending(req['state_dir'])
    return {'action': 'ack_reply'}


def release_reply(req, now):
    state = req['state_dir']
    pending = load_pending(state)
    if not pending:
        return {'action': 'skip', 'reason': 'no_pending'}
    confirm_at = float(pending.get('confirm_at') or 0)
    if pending.get('phase') == 'awaiting_ack' and confirm_at and now <= confirm_at + YES_WINDOW_S:
        seen = load_seen_events(state)
        for key in pending.get('yes_keys') or []:
            unsee_event(seen, key)
        save_seen_events(state, seen)
        pending['phase'] = 'posted'
        pending.pop('effect_id', None)
        pending.pop('yes_keys', None)
        save_pending(state, pending)
        return {'action': 'released'}
    clear_pending(state)
    return {'action': 'skip', 'reason': 'expired'}


def expire_pending(req, now):
    state = req['state_dir']
    pending = load_pending(state)
    if not pending:
        return {'action': 'skip', 'reason': 'no_pending'}
    confirm_at = pending.get('confirm_at')
    proposed_at = float(pending.get('proposed_at') or 0)
    expired = False
    if pending.get('phase') == 'proposed' and now - proposed_at > PROPOSAL_TTL_S:
        expired = True
    if confirm_at and now > float(confirm_at) + YES_WINDOW_S:
        expired = True
    if req.get('force_expire'):
        expired = True
    if not expired and not req.get('force'):
        # The routine's loop ended. Drop the unsent reply either way: the owner did not say yes.
        expired = True
    clear_pending(state)
    if expired:
        log_counts(state, reply_expired=1)
    return {'action': 'expired'}


def drop_card(req, n):
    state = req['state_dir']
    book = load_cards(state)
    key = str(int(n))
    if key not in book['cards']:
        return {'action': 'skip', 'reason': 'unknown_card'}
    email_id = book['cards'][key].get('email_id')
    del book['cards'][key]
    save_cards(state, book)
    return {'action': 'dropped', 'n': int(n), 'email_id': email_id}


def list_cards(req):
    book = load_cards(req['state_dir'])
    rows = []
    for key in sorted(book['cards'], key=lambda k: int(k) if str(k).isdigit() else 0):
        card = book['cards'][key]
        rows.append({
            'n': card.get('n'),
            'from_name': card.get('from_name') or '',
            'subject': card.get('subject') or '',
            'suspicious': bool(card.get('suspicious')),
        })
    return {'action': 'cards', 'cards': rows}


def ack(req, now):
    state = req.get('state_dir') or os.path.expanduser(STATE_DIR)
    path = req.get('dedupe_path') or os.path.join(state, 'seen_ids.json')
    dedupe = Dedupe(path, req.get('ttl_seconds', 7 * 86400), now=lambda: now)
    ids = [str(i) for i in (req.get('ids') or []) if i]
    dedupe.commit(ids)
    return {'action': 'ack', 'remembered': len(ids)}


def process(req, runner=None, now=None):
    req = apply_config(req or {})
    now = _now(req, now)
    op = str(req.get('op') or 'inbound').strip().lower()
    state = req['state_dir']
    ensure_dir(state)
    runner = runner or _default_runner(req.get('helper'))
    with StateLock(state):
        if op in ('watch', 'reply'):
            return handle_watch(req, runner, now)
        if op == 'confirm_posted':
            return confirm_posted(req, now)
        if op == 'ack_reply':
            return ack_reply(req)
        if op == 'release_reply':
            return release_reply(req, now)
        if op == 'expire':
            return expire_pending(req, now)
        if op == 'drop_card':
            return drop_card(req, req.get('n'))
        if op == 'list_cards':
            return list_cards(req)
        if op == 'ack':
            return ack(req, now)
        return handle_inbound(req, runner, now)


# ---------- CLI ----------
def _read_request(path):
    """Read a request file and delete it immediately. The body is in this file."""
    if not path:
        raw = sys.stdin.read()
        data = json.loads(raw or '{}')
        if not isinstance(data, dict):
            raise MailError('bad_payload')
        return data
    try:
        with open(path, encoding='utf-8') as fh:
            raw = fh.read()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    try:
        data = json.loads(raw or '{}')
    except ValueError:
        raise MailError('bad_payload')
    if not isinstance(data, dict):
        raise MailError('bad_payload')
    return data


def _read_payload_file(path):
    try:
        with open(path, encoding='utf-8') as fh:
            raw = fh.read()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    try:
        data = json.loads(raw or 'null')
    except ValueError:
        raise MailError('bad_payload')
    if not isinstance(data, dict):
        raise MailError('bad_payload')
    return data


def _apply_state(req, state_dir):
    if state_dir:
        req['state_dir'] = state_dir
    return req


def main(argv=None):
    parser = argparse.ArgumentParser(description='Post one email into X Chat, or watch for a reply.')
    parser.add_argument('--in', dest='infile', help='request JSON; deleted right after it is read')
    parser.add_argument('--payload-file', help='email JSON; deleted right after it is read')
    parser.add_argument('--state-dir')
    parser.add_argument('--ack', action='store_true')
    parser.add_argument('--dedupe-path')
    parser.add_argument('--forget-keys', action='store_true')
    parser.add_argument('--watch', action='store_true')
    parser.add_argument('--confirm-posted', nargs='?', const='', dest='confirm_posted')
    parser.add_argument('--ack-reply', action='store_true')
    parser.add_argument('--release-reply', action='store_true')
    parser.add_argument('--expire', action='store_true')
    parser.add_argument('--list-cards', action='store_true')
    parser.add_argument('--drop-card', type=int)
    parser.add_argument('ids', nargs='*')
    args = parser.parse_args(argv)

    if args.forget_keys:
        state = os.path.expanduser(args.state_dir or STATE_DIR)
        KeyCache(os.path.join(state, 'xchat_keys.json')).clear()
        print(json.dumps({'action': 'forgot_keys'}))
        return 0

    try:
        if args.ack and args.ids and not args.infile:
            req = {'op': 'ack', 'ids': args.ids}
        elif args.confirm_posted is not None and not args.infile:
            req = {'op': 'confirm_posted', 'confirm_id': args.confirm_posted}
        elif args.ack_reply and not args.infile:
            req = {'op': 'ack_reply'}
        elif args.release_reply and not args.infile:
            req = {'op': 'release_reply'}
        elif args.expire and not args.infile:
            req = {'op': 'expire', 'force': True}
        elif args.list_cards and not args.infile:
            req = {'op': 'list_cards'}
        elif args.drop_card is not None and not args.infile:
            req = {'op': 'drop_card', 'n': args.drop_card}
        else:
            req = _read_request(args.infile)
            if args.payload_file:
                req['payload'] = _read_payload_file(args.payload_file)
            if args.watch:
                req['op'] = 'watch'
            if args.ack:
                req['op'] = 'ack'
                if args.ids:
                    req['ids'] = list(req.get('ids') or []) + list(args.ids)
            if args.confirm_posted is not None:
                req['op'] = 'confirm_posted'
                if args.confirm_posted:
                    req['confirm_id'] = args.confirm_posted
            if args.ack_reply:
                req['op'] = 'ack_reply'
            if args.release_reply:
                req['op'] = 'release_reply'
            if args.expire:
                req['op'] = 'expire'
                req['force'] = True
            if args.list_cards:
                req['op'] = 'list_cards'
            if args.drop_card is not None:
                req['op'] = 'drop_card'
                req['n'] = args.drop_card
        if args.dedupe_path:
            req['dedupe_path'] = args.dedupe_path
        req = _apply_state(req, args.state_dir)
        res = process(req)
    except MailError as err:
        res = {'action': 'error', 'reason': err.code}
    except SystemExit:
        raise
    except Exception:
        res = {'action': 'error', 'reason': 'failed'}
    print(json.dumps(res, ensure_ascii=False))
    return 0 if res.get('action') != 'error' else 2


if __name__ == '__main__':
    sys.exit(main())
