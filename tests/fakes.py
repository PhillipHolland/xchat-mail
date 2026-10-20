"""Test doubles. No network, and no real xchat_lite."""
import json
import os
import sys
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import mail_post as m

TOKEN = 'ZXQ-BODY-TOKEN'
REPLY_TOKEN = 'REPLY-TOKEN-77'


def keys():
    return {
        'conversation_key_events': [{'v': 1}],
        'signing_keys': [{'public_key': 'abc', 'signing_public_key': 'def'}],
    }


class Runner:
    def __init__(self, decrypt_messages=None, fail=False):
        self.calls = []
        self.decrypt_messages = decrypt_messages or []
        self.fail = fail
        self.n = 0

    def __call__(self, argv, input=b''):
        self.calls.append((list(argv), input))
        if self.fail:
            return SimpleNamespace(returncode=1, stdout=b'')
        self.n += 1
        cmd = argv[-1] if argv else ''
        if cmd == 'decrypt':
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({'messages': self.decrypt_messages}).encode('utf-8'))
        body = {
            'id': '111-222',
            'message_id': 'mid-%d' % self.n,
            'encoded_message_create_event': 'ENC%d' % self.n,
            'encoded_message_event_signature': 'SIG%d' % self.n,
        }
        return SimpleNamespace(returncode=0, stdout=json.dumps(body).encode('utf-8'))

    def argv_blob(self):
        return '\n'.join(' '.join(argv) for argv, _raw in self.calls)

    def stdins(self):
        out = []
        for argv, raw in self.calls:
            if argv and argv[-1] == 'session-encrypt':
                out.append(json.loads(raw.decode('utf-8')))
        return out

    def texts(self):
        return [row.get('text') or '' for row in self.stdins()]


def mail_req(state, **over):
    req = {
        'state_dir': state,
        'conversation_id': '111-222',
        'mailSource': 'own',
        'mailMode': 'important',
        'helper': {'python': '/usr/bin/python3', 'script': '/tmp/xchat_lite.py'},
        'xchat': keys(),
        'owner_user_id': 'owner-1',
        'sender_user_id': 'sender-1',
        'agent_id': 'mail-lead',
    }
    req.update(over)
    # Tests that call --watch directly opt in by writing watch_until.
    # A request field must not open the window, so this writes the state file.
    if req.pop('arm_watch', True) and req.get('op') in ('watch', 'reply'):
        path = os.path.join(state, 'watch_until')
        if not os.path.exists(path):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as fh:
                fh.write('9999999999')
    return req


def add_card(state, **fields):
    os.makedirs(state, exist_ok=True)
    data = m.load_cards(state)
    n = fields.pop('n', None)
    if n is None:
        n = int(data.get('next') or 1)
    card = {
        'n': n,
        'email_id': 'msg_%s' % n,
        'from_name': 'Jane Doe',
        'from_addr': 'jane@x.com',
        'subject': 'Hello',
        'reply_to': 'msg_%s' % n,
        'message_id': 'card-mid-%s' % n,
        'sequence_id': '',
        'text_sha256': 'abc',
        'suspicious': False,
        'source': 'own',
    }
    card.update(fields)
    card['n'] = n
    data['cards'][str(n)] = card
    data['next'] = max(int(data.get('next') or 1), n + 1)
    m.save_cards(state, data)
    return card


def owner_msg(text, seq='10', mid='e10', created=None, status='verified', reply_to='', sender='owner-1'):
    return {
        'sequence_id': seq,
        'id': mid,
        'sender_id': sender,
        'conversation_id': '111-222',
        'created_at_msec': created,
        'text': text,
        'status': status,
        'reply_to_sequence_id': reply_to,
    }


def tree_text(state):
    blobs = []
    for root, _dirs, files in os.walk(state):
        for name in files:
            path = os.path.join(root, name)
            with open(path, 'rb') as fh:
                blobs.append(fh.read().decode('utf-8', 'replace'))
    return '\n'.join(blobs)


def mode_of(path):
    return os.stat(path).st_mode & 0o777
