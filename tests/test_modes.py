"""Own mode, agent mode, files, the key cache, and the command line."""
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from email.message import EmailMessage

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fakes
import mail_post as m


def person(text, **extra):
    payload = {
        'id': extra.pop('id', 'msg_1'),
        'from_name': 'Jane Doe',
        'from_addr': 'jane@x.com',
        'subject': "Friday's invoice",
        'text': text,
    }
    payload.update(extra)
    return payload


class ModeTests(unittest.TestCase):
    def test_own_mode_ignores_a_supplied_tag(self):
        with tempfile.TemporaryDirectory() as state:
            runner = fakes.Runner()
            res = m.process(fakes.mail_req(state, payload=person(
                'Are you free for lunch on Friday?',
                subject='Lunch',
                tag='suspicious',
                reason='Tries to instruct the bot. The body was withheld.',
            )), runner=runner, now=1000)
            self.assertEqual(res['tag'], 'important')
            card = runner.texts()[0]
            self.assertTrue(card.startswith('✉️ Jane Doe · Lunch'))
            self.assertIn('lunch', card.lower())
            self.assertNotIn('withheld', card)

            runner = fakes.Runner()
            res = m.process(fakes.mail_req(state, payload=person(
                'Unsubscribe from this newsletter. View in browser.',
                id='msg_2',
                from_addr='noreply@news.example',
                from_name='News',
                subject='Weekly notes',
                tag='important',
            )), runner=runner, now=1000)
            self.assertEqual(res['action'], 'skip')
            self.assertEqual(res['reason'], 'normal')
            self.assertEqual(runner.calls, [])

    def test_mail_mode_all_posts_a_normal_letter_in_the_plain_shape(self):
        with tempfile.TemporaryDirectory() as state:
            runner = fakes.Runner()
            res = m.process(fakes.mail_req(state, mailMode='all', payload=person(
                'Your receipt is ready. The total was twenty dollars.',
                subject='Receipt',
                from_addr='noreply@shop.example',
                from_name='Shop',
            )), runner=runner, now=1000)
            self.assertEqual(res['action'], 'send')
            self.assertEqual(res['tag'], 'normal')
            card = runner.texts()[0]
            self.assertTrue(card.startswith('✉️ Shop · Receipt'))
            self.assertIn('Your receipt is ready.', card)
            self.assertNotIn('⚠️', card)

    def test_important_mode_posts_a_bank_failure_from_noreply(self):
        with tempfile.TemporaryDirectory() as state:
            runner = fakes.Runner()
            res = m.process(fakes.mail_req(state, payload=person(
                'Your payment failed for the March invoice.',
                subject='Payment failed',
                from_addr='noreply@bank.example',
                from_name='Bank',
            )), runner=runner, now=1000)
            self.assertEqual(res['tag'], 'important')
            self.assertIn('payment failed', runner.texts()[0].lower())

    def test_agent_trusts_a_safe_suspicious_reason_and_drops_an_unsafe_one(self):
        with tempfile.TemporaryDirectory() as state:
            runner = fakes.Runner()
            res = m.process(fakes.mail_req(state, mailSource='agent', payload={
                'id': 'msg_7',
                'from_name': 'Sam',
                'from_addr': 'sam@x.com',
                'subject': 'Odd note',
                'tag': 'suspicious',
                'reason': 'The sender asked for a password.',
                'text': 'ZXQ-BODY-TOKEN please read this excerpt',
                'attachments': ['invoice.pdf'],
                'link': 'https://mail.example/msg/7',
            }), runner=runner, now=1000)
            card = runner.texts()[0]
            self.assertEqual(
                card,
                '⚠️ Sam · suspicious: Odd note\nThe sender asked for a password.')
            self.assertNotIn('ZXQ-BODY-TOKEN', card)
            self.assertNotIn('Open:', card)
            self.assertTrue(res.get('suspicious'))

            runner = fakes.Runner()
            m.process(fakes.mail_req(state, mailSource='agent', payload={
                'id': 'msg_8',
                'from_name': 'Sam',
                'from_addr': 'sam@x.com',
                'subject': 'Odd note',
                'tag': 'suspicious',
                'reason': 'See https://evil.example/secret ZXQ-BODY-TOKEN',
                'text': 'ZXQ-BODY-TOKEN',
            }), runner=runner, now=1000)
            card = runner.texts()[0]
            self.assertIn('Flagged as suspicious. The body was withheld.', card)
            self.assertNotIn('evil.example', card)
            self.assertNotIn('ZXQ-BODY-TOKEN', card)
            self.assertNotIn('ZXQ-BODY-TOKEN', fakes.tree_text(state))

    def test_agent_excerpt_is_capped_and_own_mode_is_not(self):
        body = 'Hello. ' + ('n' * 700) + ' ZEBRAWORD'
        agent = m.merge_payload({
            'id': 'msg_1', 'from_name': 'Alex', 'from_addr': 'alex@x.com',
            'subject': 'Note', 'text': body,
        }, 'agent')
        own = m.merge_payload({
            'id': 'msg_1', 'from_name': 'Alex', 'from_addr': 'alex@x.com',
            'subject': 'Note', 'text': body,
        }, 'own')
        self.assertNotIn('ZEBRAWORD', agent['_scan'])
        self.assertIn('ZEBRAWORD', own['_scan'])

    def test_duplicate_defer_commit_and_normal_skip_commits(self):
        with tempfile.TemporaryDirectory() as state:
            runner = fakes.Runner()
            first = m.process(fakes.mail_req(state, defer_commit=True, payload=person(
                'The March invoice is attached. Please pay it by Friday. ZXQ-BODY-TOKEN',
            )), runner=runner, now=1000)
            self.assertEqual(first['action'], 'send')
            dedupe = m.Dedupe(os.path.join(state, 'seen_ids.json'), now=lambda: 1000)
            self.assertFalse(dedupe.seen('msg_1'))
            m.process({'op': 'ack', 'state_dir': state, 'ids': ['msg_1']}, now=1000)
            self.assertTrue(dedupe.seen('msg_1'))
            second = m.process(fakes.mail_req(state, payload=person(
                'The March invoice is attached again. ZXQ-BODY-TOKEN',
            )), runner=fakes.Runner(), now=1000)
            self.assertEqual(second['reason'], 'duplicate')
            self.assertNotIn('ZXQ-BODY-TOKEN', json.dumps(second))

            runner = fakes.Runner()
            skipped = m.process(fakes.mail_req(state, defer_commit=True, payload=person(
                'Unsubscribe from this newsletter.',
                id='msg_9',
                from_addr='noreply@news.example',
                subject='Weekly',
            )), runner=runner, now=1000)
            self.assertEqual(skipped['reason'], 'normal')
            again = m.process(fakes.mail_req(state, payload=person(
                'Unsubscribe from this newsletter.',
                id='msg_9',
                from_addr='noreply@news.example',
                subject='Weekly',
            )), runner=fakes.Runner(), now=1000)
            self.assertEqual(again['reason'], 'duplicate')

    def test_missing_keys_fetch_and_failed_encrypt_leave_no_card(self):
        with tempfile.TemporaryDirectory() as state:
            req = fakes.mail_req(state, payload=person(
                'The March invoice is attached. ZXQ-BODY-TOKEN'))
            req.pop('xchat')
            runner = fakes.Runner()
            res = m.process(req, runner=runner, now=1000)
            self.assertEqual(res['action'], 'fetch')
            self.assertEqual(res['need'], ['conversation_key_events', 'signing_keys'])
            self.assertEqual(runner.calls, [])
            self.assertFalse(os.path.exists(os.path.join(state, 'cards.json')))
            self.assertNotIn('ZXQ-BODY-TOKEN', fakes.tree_text(state))

            runner = fakes.Runner(fail=True)
            res = m.process(fakes.mail_req(state, payload=person(
                'The March invoice is attached. ZXQ-BODY-TOKEN')), runner=runner, now=1000)
            self.assertEqual(res['reason'], 'xchat_lite_failed')
            self.assertNotIn('ZXQ-BODY-TOKEN', json.dumps(res))
            self.assertFalse(os.path.exists(os.path.join(state, 'cards.json')))
            runner = fakes.Runner()
            res = m.process(fakes.mail_req(state, payload=person(
                'The March invoice is attached. ZXQ-BODY-TOKEN')), runner=runner, now=1000)
            self.assertEqual(res['action'], 'send')
            self.assertIn('ZXQ-BODY-TOKEN', runner.texts()[0])
            self.assertNotIn('ZXQ-BODY-TOKEN', fakes.tree_text(state))

    def test_key_cache_roundtrip_and_clear_on_failure(self):
        with tempfile.TemporaryDirectory() as state:
            m.process(fakes.mail_req(state, payload=person(
                'The March invoice is attached.')), runner=fakes.Runner(), now=1000)
            cache = os.path.join(state, 'xchat_keys.json')
            self.assertEqual(fakes.mode_of(cache), 0o600)
            with open(cache, encoding='utf-8') as fh:
                self.assertNotIn('ZXQ-BODY-TOKEN', fh.read())
            req = fakes.mail_req(state, payload=person(
                'A second note from Jane.', id='msg_2', subject='Hello'))
            req.pop('xchat')
            res = m.process(req, runner=fakes.Runner(), now=1000)
            self.assertEqual(res['action'], 'send')
            req = fakes.mail_req(state, payload=person(
                'A third note.', id='msg_3', subject='Hello again'))
            req.pop('xchat')
            res = m.process(req, runner=fakes.Runner(fail=True), now=1000)
            self.assertEqual(res['action'], 'fetch')
            self.assertFalse(os.path.exists(cache))
            dedupe = m.Dedupe(os.path.join(state, 'seen_ids.json'), now=lambda: 1000)
            self.assertFalse(dedupe.seen('msg_3'))

    def test_state_files_are_private_and_logs_are_counts(self):
        with tempfile.TemporaryDirectory() as state:
            m.process(fakes.mail_req(state, payload=person(
                'The March invoice is attached. Please pay it by Friday. ZXQ-BODY-TOKEN',
            )), runner=fakes.Runner(), now=1000)
            self.assertEqual(stat.S_IMODE(os.stat(state).st_mode), 0o700)
            for name in ('cards.json', 'seen_ids.json', 'xchat_keys.json', 'mail.log'):
                path = os.path.join(state, name)
                self.assertEqual(fakes.mode_of(path), 0o600, name)
            with open(os.path.join(state, 'mail.log'), encoding='utf-8') as fh:
                log = fh.read()
            self.assertNotIn('ZXQ-BODY-TOKEN', log)
            self.assertRegex(log.strip(), r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z(?: [a-z_]+=\d+)+$')
            listed = m.process({'op': 'list_cards', 'state_dir': state})
            blob = json.dumps(listed)
            self.assertNotIn('jane@x.com', blob)
            self.assertNotIn('ZXQ-BODY-TOKEN', blob)
            self.assertEqual(listed['cards'][0]['from_name'], 'Jane Doe')
            self.assertIn('invoice', listed['cards'][0]['subject'])

    def test_raw_rfc822_extracts_headers_and_not_the_attachment_bytes(self):
        msg = EmailMessage()
        msg['From'] = 'Jane Doe <jane@x.com>'
        msg['Subject'] = "Friday's invoice"
        msg['Message-ID'] = '<m1@x.com>'
        msg.set_content('The March invoice is attached. Please pay it by Friday.\n')
        msg.add_attachment(b'%PDF-1.4 ZXQ-BODY-TOKEN', maintype='application', subtype='pdf',
                           filename='invoice.pdf')
        with tempfile.TemporaryDirectory() as state:
            runner = fakes.Runner()
            res = m.process(fakes.mail_req(state, payload={
                'id': 'msg_1',
                'raw': msg.as_string(),
                'link': 'https://mail.example/should-not-show',
            }), runner=runner, now=1000)
            card = runner.texts()[0]
            self.assertIn('Jane Doe', card)
            self.assertIn("Friday's invoice", card)
            self.assertIn('📎 invoice.pdf', card)
            self.assertNotIn('%PDF', card)
            self.assertNotIn('ZXQ-BODY-TOKEN', card)
            self.assertNotIn('Open:', card)
            self.assertNotIn('ZXQ-BODY-TOKEN', json.dumps(res))
            self.assertNotIn('ZXQ-BODY-TOKEN', fakes.tree_text(state))
            stored = m.load_cards(state)['cards']['1']['reply_to']
            self.assertIn('m1@x.com', stored)

    def test_drop_card_allows_a_retry_when_the_send_was_not_acked(self):
        with tempfile.TemporaryDirectory() as state:
            res = m.process(fakes.mail_req(state, defer_commit=True, payload=person(
                'The March invoice is attached.')), runner=fakes.Runner(), now=1000)
            dropped = m.process({'op': 'drop_card', 'state_dir': state, 'n': res['card_n']})
            self.assertEqual(dropped['email_id'], 'msg_1')
            again = m.process(fakes.mail_req(state, defer_commit=True, payload=person(
                'The March invoice is attached.')), runner=fakes.Runner(), now=1000)
            self.assertEqual(again['action'], 'send')
            self.assertEqual(again['card_n'], 2)

    def test_empty_string_does_not_wipe_config(self):
        with tempfile.TemporaryDirectory() as state:
            m.atomic_write(os.path.join(state, 'config.json'), json.dumps({
                'mailMode': 'all',
                'mailSource': 'own',
            }))
            runner = fakes.Runner()
            res = m.process(fakes.mail_req(state, mailMode='', payload=person(
                'Your receipt is ready. The total was twenty dollars.',
                subject='Receipt',
                from_addr='noreply@shop.example',
                from_name='Shop',
            )), runner=runner, now=1000)
            self.assertEqual(res['tag'], 'normal')
            self.assertEqual(res['action'], 'send')
            res = m.process(fakes.mail_req(state, mailMode='important', payload=person(
                'Your receipt is ready.',
                id='msg_2',
                subject='Receipt',
                from_addr='noreply@shop.example',
                from_name='Shop',
            )), runner=fakes.Runner(), now=1000)
            self.assertEqual(res['reason'], 'normal')

    def test_command_line_deletes_the_request_and_hides_the_body(self):
        with tempfile.TemporaryDirectory() as state:
            bad = os.path.join(state, 'bad.json')
            with open(bad, 'w', encoding='utf-8') as fh:
                fh.write('not json')
            os.chmod(bad, 0o600)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = m.main(['--in', bad, '--state-dir', state])
            self.assertEqual(rc, 2)
            self.assertFalse(os.path.exists(bad))
            self.assertIn('bad_payload', buf.getvalue())
            self.assertNotIn('Traceback', buf.getvalue())

            infile = os.path.join(state, 'req.json')
            payload = os.path.join(state, 'mail.json')
            with open(infile, 'w', encoding='utf-8') as fh:
                json.dump({'conversation_id': '111-222'}, fh)
            os.chmod(infile, 0o600)
            with open(payload, 'w', encoding='utf-8') as fh:
                json.dump({
                    'id': 'msg_4',
                    'from_name': 'News',
                    'from_addr': 'noreply@news.example',
                    'subject': 'Weekly',
                    'text': 'Unsubscribe from this newsletter. ZXQ-BODY-TOKEN',
                }, fh)
            os.chmod(payload, 0o600)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = m.main(['--in', infile, '--payload-file', payload, '--state-dir', state])
            self.assertEqual(rc, 0)
            self.assertFalse(os.path.exists(infile))
            self.assertFalse(os.path.exists(payload))
            self.assertNotIn('ZXQ-BODY-TOKEN', buf.getvalue())
            self.assertNotIn('ZXQ-BODY-TOKEN', fakes.tree_text(state))
            self.assertIn('normal', buf.getvalue())

    def test_cached_watch_failure_does_not_consume_a_yes(self):
        with tempfile.TemporaryDirectory() as state:
            fakes.add_card(state)
            m.process(fakes.mail_req(state, op='watch', messages=[
                fakes.owner_msg('reply 1 See you Friday', seq='10', mid='e10'),
            ]), runner=fakes.Runner(), now=1000)
            m.process({'op': 'confirm_posted', 'state_dir': state, 'confirm_id': m.load_pending(state)['confirm_id']},
                      now=1000)
            req = fakes.mail_req(state, op='watch', messages=[
                fakes.owner_msg('yes', seq='11', mid='yes-1', created=1050 * 1000),
            ])
            req.pop('xchat')
            res = m.process(req, runner=fakes.Runner(fail=True), now=1050)
            self.assertEqual(res['action'], 'fetch')
            self.assertNotIn(fakes.REPLY_TOKEN, json.dumps(res))
            self.assertEqual(m.load_pending(state)['phase'], 'posted')
            self.assertFalse(os.path.exists(os.path.join(state, 'xchat_keys.json')))
            seen = m.load_seen_events(state)
            self.assertNotIn('yes-1', seen['ids'])


if __name__ == '__main__':
    unittest.main()
