"""Reply confirm: yes window, native reply, reply N, and no auto-send."""
import json
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fakes
import mail_post as m


class ReplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = self.tmp.name
        fakes.add_card(self.state)

    def tearDown(self):
        self.tmp.cleanup()

    def watch(self, messages, now=1000, runner=None, **kw):
        runner = runner or fakes.Runner()
        req = fakes.mail_req(self.state, op='watch', messages=messages, **kw)
        res = m.process(req, runner=runner, now=now)
        return res, runner

    def propose(self, text='See you Friday', now=1000, n=1):
        res, runner = self.watch([
            fakes.owner_msg('reply %d %s' % (n, text), seq='10', mid='e10', created=now * 1000),
        ], now=now)
        self.assertEqual(res['phase'], 'confirm')
        self.assertNotIn('effect', res)
        posted = m.process(
            {'op': 'confirm_posted', 'state_dir': self.state, 'confirm_id': res['confirm_id']},
            now=now)
        self.assertEqual(posted['action'], 'confirm_posted')
        return res, runner

    def answer(self, proposed, text='yes', seq='11', mid='yes-1', created=1050 * 1000):
        return fakes.owner_msg(
            text, seq=seq, mid=mid, created=created,
            reply_to=proposed['sends'][0]['message_id'])

    def test_yes_and_no_words(self):
        for text in ('yes', 'YES', 'yes please', 'Yes!', 'yes.'):
            self.assertTrue(m.is_yes(text), text)
        for text in ('y', 'yes, send', 'yeah', 'yes please send'):
            self.assertFalse(m.is_yes(text), text)
        for text in ('no', 'nope', 'cancel', 'stop', 'No!'):
            self.assertTrue(m.is_no(text), text)

    def test_confirm_quotes_the_reply_and_does_not_send(self):
        res, runner = self.watch([
            fakes.owner_msg('reply 1 See you Friday', seq='10', mid='e10'),
        ])
        self.assertEqual(res['phase'], 'confirm')
        self.assertEqual(res['card_n'], 1)
        self.assertNotIn('effect', res)
        self.assertEqual(
            runner.texts()[0],
            'Reply to Jane Doe <jane@x.com>: "See you Friday"? Reply yes within 2 min.')
        self.assertNotIn('reply_to', runner.stdins()[0])
        pending = m.load_pending(self.state)
        self.assertEqual(pending['phase'], 'proposed')
        self.assertEqual(fakes.mode_of(os.path.join(self.state, 'pending.json')), 0o600)
        self.assertNotIn('to', pending)
        self.assertNotIn('subject', pending)
        self.assertEqual(pending['to_sha256'], m._sha('jane@x.com'))
        with open(os.path.join(self.state, 'pending.json'), encoding='utf-8') as fh:
            pending_text = fh.read()
        self.assertNotIn('jane@x.com', pending_text)
        self.assertNotIn('Hello', pending_text)
        self.assertNotIn(fakes.REPLY_TOKEN, json.dumps(res))

    def test_yes_inside_the_window_emits_one_effect(self):
        proposed, _runner = self.propose(fakes.REPLY_TOKEN)
        res, runner = self.watch([
            self.answer(proposed, seq='11', mid='yes-1', created=1050 * 1000),
        ], now=1050)
        self.assertEqual(res['phase'], 'approved')
        self.assertEqual(res['effect']['action'], 'send_email_reply')
        self.assertEqual(res['effect']['to'], 'jane@x.com')
        self.assertEqual(res['effect']['reply_to'], 'msg_1')
        self.assertEqual(res['effect']['text'], fakes.REPLY_TOKEN)
        self.assertTrue(res['effect']['effect_id'])
        self.assertEqual(runner.texts()[0], '→ Jane Doe ✉️ sent')
        self.assertNotIn(fakes.REPLY_TOKEN, runner.texts()[0])
        self.assertEqual(m.load_pending(self.state)['phase'], 'awaiting_ack')
        again, _runner = self.watch([
            self.answer(proposed, seq='11', mid='yes-1', created=1050 * 1000),
        ], now=1060)
        self.assertEqual(again['reason'], 'awaiting_ack')
        self.assertNotIn('effect', again)
        self.assertNotIn(fakes.REPLY_TOKEN, json.dumps(again))
        with open(os.path.join(self.state, 'pending.json'), encoding='utf-8') as fh:
            pending_text = fh.read()
        self.assertIn(fakes.REPLY_TOKEN, pending_text)
        self.assertNotIn(fakes.REPLY_TOKEN, fakes.tree_text(self.state).replace(pending_text, ''))

    def test_rewritten_address_does_not_replace_the_frozen_hash(self):
        proposed, _runner = self.propose(fakes.REPLY_TOKEN)
        book = m.load_cards(self.state)
        book['cards']['1']['from_addr'] = 'casey@example.com'
        book['cards']['1']['from_name'] = 'Casey Boone'
        m.save_cards(self.state, book)
        res, runner = self.watch([
            self.answer(proposed, seq='11', mid='yes-freeze', created=1050 * 1000),
        ], now=1050)
        self.assertNotEqual(res.get('phase'), 'approved')
        self.assertNotIn('effect', res)
        self.assertEqual(res['reason'], 'address_mismatch')
        self.assertNotIn('casey@example.com', json.dumps(res))
        self.assertNotIn(fakes.REPLY_TOKEN, json.dumps(res))
        self.assertEqual(runner.calls, [])
        pending = m.load_pending(self.state)
        self.assertEqual(pending['to_sha256'], m._sha('jane@x.com'))
        self.assertNotIn('to', pending)
        with open(os.path.join(self.state, 'pending.json'), encoding='utf-8') as fh:
            self.assertNotIn('jane@x.com', fh.read())
            self.assertNotIn('casey@example.com', fh.read())

    def test_yes_at_120_seconds_counts(self):
        proposed, _runner = self.propose()
        res, runner = self.watch([
            self.answer(proposed, text='yes please', seq='11', mid='yes-120', created=1120 * 1000),
        ], now=1120)
        self.assertEqual(res['phase'], 'approved')
        self.assertIn('effect', res)
        self.assertEqual(runner.texts()[0], '→ Jane Doe ✉️ sent')

    def test_yes_inside_the_window_counts_when_the_poll_is_late(self):
        proposed, _runner = self.propose()
        res, runner = self.watch([
            self.answer(proposed, seq='11', mid='yes-edge', created=1120 * 1000),
        ], now=1121)
        self.assertEqual(res['phase'], 'approved')
        self.assertIn('effect', res)
        self.assertEqual(runner.texts()[0], '→ Jane Doe ✉️ sent')

    def test_late_yes_posts_too_late_and_does_not_emit(self):
        proposed, _runner = self.propose()
        runner = fakes.Runner()
        res, runner = self.watch([
            self.answer(proposed, seq='12', mid='yes-121', created=1121 * 1000),
        ], now=1121, runner=runner)
        self.assertEqual(res['phase'], 'expired')
        self.assertNotIn('effect', res)
        self.assertEqual(runner.texts()[0], 'Too late to send.')
        self.assertIsNone(m.load_pending(self.state))

    def test_no_cancels_and_a_bare_yes_does_nothing(self):
        proposed, _runner = self.propose()
        res, runner = self.watch([
            fakes.owner_msg('yes', seq='13', mid='yes-bare', created=1040 * 1000),
        ], now=1040)
        self.assertEqual(res['reason'], 'quiet')
        self.assertEqual(runner.calls, [])
        self.assertEqual(m.load_pending(self.state)['phase'], 'posted')
        res, runner = self.watch([
            fakes.owner_msg('nope', seq='14', mid='no-bare', created=1050 * 1000),
        ], now=1050)
        self.assertEqual(res['reason'], 'quiet')
        self.assertEqual(runner.calls, [])
        self.assertEqual(m.load_pending(self.state)['phase'], 'posted')
        res, runner = self.watch([
            self.answer(proposed, text='nope', seq='12', mid='no-1', created=1060 * 1000),
        ], now=1060)
        self.assertEqual(res['phase'], 'cancelled')
        self.assertNotIn('effect', res)
        self.assertEqual(runner.texts()[0], 'Not sent.')
        self.assertIsNone(m.load_pending(self.state))

        res, runner = self.watch([
            fakes.owner_msg('yes', seq='15', mid='yes-after', created=1070 * 1000),
        ], now=1070)
        self.assertEqual(res['reason'], 'quiet')
        self.assertEqual(runner.calls, [])

    def test_yes_in_the_proposal_batch_cannot_approve(self):
        runner = fakes.Runner()
        res, _runner = self.watch([
            fakes.owner_msg('reply 1 See you Friday', seq='10', mid='e10', created=1000 * 1000),
            fakes.owner_msg('yes', seq='11', mid='yes-early', created=1000 * 1000, reply_to='mid-1'),
        ], now=1000, runner=runner)
        self.assertEqual(res['phase'], 'confirm')
        self.assertEqual(res['sends'][0]['message_id'], 'mid-1')
        self.assertNotIn('effect', res)
        m.process({'op': 'confirm_posted', 'state_dir': self.state, 'confirm_id': res['confirm_id']},
                  now=1000)
        seen = m.load_seen_events(self.state)
        m.unsee_event(seen, '11')
        m.unsee_event(seen, 'yes-early')
        m.save_seen_events(self.state, seen)
        res, runner = self.watch([
            fakes.owner_msg('yes', seq='11', mid='yes-early', created=1000 * 1000, reply_to='mid-1'),
        ], now=1020)
        self.assertNotIn('effect', res)
        self.assertNotEqual(res.get('phase'), 'approved')
        fresh, fresh_runner = self.watch([
            fakes.owner_msg('yes', seq='12', mid='yes-new', created=1030 * 1000, reply_to='mid-1'),
        ], now=1030)
        self.assertEqual(fresh['phase'], 'approved')
        self.assertEqual(fresh_runner.texts()[0], '→ Jane Doe ✉️ sent')

    def test_missing_timestamp_and_yes_before_confirm_posted(self):
        proposed, _runner = self.watch([
            fakes.owner_msg('reply 1 See you Friday', seq='10', mid='e10'),
        ])
        confirm_id = proposed['confirm_id']
        res, runner = self.watch([
            fakes.owner_msg('yes', seq='11', mid='yes-pre', created=1002 * 1000),
        ], now=1002)
        self.assertEqual(res['phase'], 'confirm')
        self.assertEqual(res['confirm_id'], confirm_id)
        self.assertNotIn('effect', res)
        self.assertEqual(runner.texts()[0],
                         'Reply to Jane Doe <jane@x.com>: "See you Friday"? Reply yes within 2 min.')
        m.process({'op': 'confirm_posted', 'state_dir': self.state, 'confirm_id': confirm_id}, now=1010)
        res, _runner = self.watch([
            fakes.owner_msg('yes', seq='11', mid='yes-pre', created=1002 * 1000),
        ], now=1020)
        self.assertNotIn('effect', res)
        res, _runner = self.watch([
            fakes.owner_msg('yes', seq='13', mid='yes-notime', created=None),
        ], now=1030)
        self.assertNotIn('effect', res)
        self.assertEqual(m.load_pending(self.state)['phase'], 'posted')
        pending = m.load_pending(self.state)
        opened = int(pending['confirm_at']) * 1000
        self.assertFalse(m.fresh_yes(
            {'sequence_id': '14', 'id': 'edge0', 'created_at_msec': opened - 2000}, pending))
        self.assertFalse(m.fresh_yes(
            {'sequence_id': '15', 'id': 'edge1', 'created_at_msec': opened - 1}, pending))
        self.assertTrue(m.fresh_yes(
            {'sequence_id': '16', 'id': 'edge2', 'created_at_msec': opened}, pending))
        self.assertFalse(m.in_yes_window(
            {'sequence_id': '18', 'id': 'edge4', 'created_at_msec': opened - 1}, pending))
        self.assertTrue(m.in_yes_window(
            {'sequence_id': '19', 'id': 'edge5', 'created_at_msec': opened}, pending))
        self.assertTrue(m.in_yes_window(
            {'sequence_id': '20', 'id': 'edge6',
             'created_at_msec': opened + m.YES_WINDOW_S * 1000}, pending))
        self.assertFalse(m.in_yes_window(
            {'sequence_id': '21', 'id': 'edge7',
             'created_at_msec': opened + m.YES_WINDOW_S * 1000 + 1}, pending))

    def test_bare_early_and_unrelated_yes_do_not_approve(self):
        proposed, _runner = self.propose(fakes.REPLY_TOKEN)
        mid = proposed['sends'][0]['message_id']
        self.assertIn(mid, m.load_pending(self.state)['confirm_message_ids'])
        res, runner = self.watch([
            fakes.owner_msg('yes', seq='11', mid='bare', created=1050 * 1000),
        ], now=1050)
        self.assertNotIn('effect', res)
        self.assertEqual(runner.calls, [])
        self.assertEqual(m.load_pending(self.state)['phase'], 'posted')
        res, runner = self.watch([
            fakes.owner_msg('yes please', seq='12', mid='early', created=1000 * 1000 - 2000, reply_to=mid),
        ], now=1000)
        self.assertNotIn('effect', res)
        self.assertEqual(runner.calls, [])
        self.assertEqual(m.load_pending(self.state)['phase'], 'posted')
        res, runner = self.watch([
            fakes.owner_msg('yes', seq='13', mid='card-yes', created=1060 * 1000, reply_to='card-mid-1'),
        ], now=1060)
        self.assertNotIn('effect', res)
        res, runner = self.watch([
            fakes.owner_msg('yes', seq='14', mid='other', created=1070 * 1000, reply_to='relay-mid'),
        ], now=1070)
        self.assertNotIn('effect', res)
        self.assertEqual(m.load_pending(self.state)['phase'], 'posted')
        res, runner = self.watch([
            fakes.owner_msg('yes', seq='15', mid='edge', created=1000 * 1000, reply_to=mid),
        ], now=1000)
        self.assertEqual(res['phase'], 'approved')
        self.assertEqual(res['effect']['text'], fakes.REPLY_TOKEN)
        self.assertEqual(runner.texts()[0], '→ Jane Doe ✉️ sent')

    def test_native_reply_to_the_confirm_sequence_approves(self):
        proposed, _runner = self.propose()
        mid = proposed['sends'][0]['message_id']
        res, runner = self.watch([
            {
                'text': 'confirm',
                'status': 'verified',
                'event': {
                    'type': 'Message', 'sequence_id': '70', 'id': mid,
                    'sender_id': 'sender-1', 'conversation_id': '111:222',
                    'created_at_msec': 1010 * 1000,
                    'content': {'content_type': 'Text'},
                },
            },
            {
                'text': 'yes',
                'status': 'verified',
                'event': {
                    'type': 'Message', 'sequence_id': '71', 'id': 'yes-seq',
                    'sender_id': 'owner-1', 'conversation_id': '111:222',
                    'created_at_msec': 1080 * 1000,
                    'content': {
                        'content_type': 'Text',
                        'replying_to_preview': {'replying_to_message_sequence_id': '70'},
                    },
                },
            },
        ], now=1080)
        self.assertEqual(res['phase'], 'approved')
        self.assertEqual(runner.texts()[0], '→ Jane Doe ✉️ sent')
        self.assertIn('70', m.load_pending(self.state)['confirm_sequence_ids'])

    def test_reply_to_the_first_confirm_message_id_after_reencrypt(self):
        runner = fakes.Runner()
        res, runner = self.watch([
            fakes.owner_msg('reply 1 See you Friday', seq='10', mid='e10', created=1000 * 1000),
        ], now=1000, runner=runner)
        first = res['sends'][0]['message_id']
        again, runner = self.watch([], now=1005, runner=runner)
        second = again['sends'][0]['message_id']
        self.assertNotEqual(first, second)
        self.assertEqual(again['confirm_id'], res['confirm_id'])
        self.assertEqual(m.load_pending(self.state)['confirm_message_ids'], [first, second])
        m.process(
            {'op': 'confirm_posted', 'state_dir': self.state, 'confirm_id': res['confirm_id']},
            now=1005)
        approved, _runner = self.watch([
            fakes.owner_msg('yes', seq='11', mid='yes-first', created=1060 * 1000, reply_to=first),
        ], now=1060)
        self.assertEqual(approved['phase'], 'approved')
        self.assertIn('effect', approved)

    def test_encrypt_failure_does_not_consume_the_yes(self):
        proposed, _runner = self.propose(fakes.REPLY_TOKEN)
        runner = fakes.Runner(fail=True)
        res, _runner = self.watch([
            self.answer(proposed, seq='11', mid='yes-1', created=1050 * 1000),
        ], now=1050, runner=runner)
        self.assertEqual(res['action'], 'error')
        self.assertEqual(res['reason'], 'xchat_lite_failed')
        self.assertNotIn('effect', res)
        self.assertNotIn(fakes.REPLY_TOKEN, json.dumps(res))
        self.assertEqual(m.load_pending(self.state)['phase'], 'posted')
        seen = m.load_seen_events(self.state)
        self.assertNotIn('yes-1', seen['ids'])
        res, runner = self.watch([
            self.answer(proposed, seq='11', mid='yes-1', created=1050 * 1000),
        ], now=1060)
        self.assertEqual(res['phase'], 'approved')
        self.assertEqual(res['effect']['text'], fakes.REPLY_TOKEN)

    def test_release_records_a_spent_effect_and_does_not_emit_again(self):
        proposed, _runner = self.propose(fakes.REPLY_TOKEN)
        approved, _runner = self.watch([
            self.answer(proposed, seq='11', mid='yes-1', created=1050 * 1000),
        ], now=1050)
        effect_id = approved['effect']['effect_id']
        released = m.process({'op': 'release_reply', 'state_dir': self.state}, now=1060)
        self.assertEqual(released['action'], 'released')
        self.assertEqual(m.load_pending(self.state)['phase'], 'posted')
        self.assertNotIn('effect', released)
        self.assertNotIn(fakes.REPLY_TOKEN, json.dumps(released))
        ledger_path = os.path.join(self.state, 'spent_effects.json')
        self.assertEqual(fakes.mode_of(ledger_path), 0o600)
        with open(ledger_path, encoding='utf-8') as fh:
            ledger = fh.read()
        self.assertIn(effect_id, ledger)
        self.assertNotIn(fakes.REPLY_TOKEN, ledger)
        self.assertNotIn('jane@x.com', ledger)
        res, runner = self.watch([
            self.answer(proposed, seq='11', mid='yes-1', created=1050 * 1000),
        ], now=1070)
        self.assertEqual(res['reason'], 'spent')
        self.assertNotIn('effect', res)
        self.assertNotEqual(res.get('phase'), 'approved')
        self.assertNotIn(fakes.REPLY_TOKEN, json.dumps(res))
        self.assertEqual(runner.calls, [])
        fresh, fresh_runner = self.watch([
            self.answer(proposed, text='yes please', seq='12', mid='yes-2', created=1080 * 1000),
        ], now=1080)
        self.assertEqual(fresh['phase'], 'approved')
        self.assertEqual(fresh['effect']['text'], fakes.REPLY_TOKEN)
        self.assertNotEqual(fresh['effect']['effect_id'], effect_id)
        self.assertEqual(fresh_runner.texts()[0], '→ Jane Doe ✉️ sent')
        m.process({'op': 'ack_reply', 'state_dir': self.state})
        self.assertIsNone(m.load_pending(self.state))
        self.assertNotIn(fakes.REPLY_TOKEN, fakes.tree_text(self.state))

    def test_reply_number_native_reply_and_binding(self):
        fakes.add_card(self.state, n=2, from_name='Alex', from_addr='alex@x.com',
                       email_id='msg_2', reply_to='msg_2', subject='Security alert')
        res, runner = self.watch([
            fakes.owner_msg('reply 2 The alert was mine', seq='20', mid='e20'),
        ])
        self.assertEqual(res['card_n'], 2)
        self.assertIn('Reply to Alex <alex@x.com>:', runner.texts()[0])
        m.process({'op': 'expire', 'state_dir': self.state, 'force': True}, now=1000)

        book = m.load_cards(self.state)
        book['cards']['1']['message_id'] = 'card-mid-1'
        book['cards']['1']['sequence_id'] = ''
        m.save_cards(self.state, book)
        res, runner = self.watch([
            fakes.owner_msg('the card itself', seq='40', mid='card-mid-1', sender='sender-1'),
            fakes.owner_msg('Thanks for the note', seq='41', mid='e41', reply_to='40'),
        ])
        self.assertEqual(res['phase'], 'confirm')
        self.assertEqual(res['card_n'], 1)
        self.assertIn('"Thanks for the note"', runner.texts()[0])
        self.assertEqual(m.load_cards(self.state)['cards']['1']['sequence_id'], '40')

    def test_hash_binding_and_events_thread_the_confirm(self):
        text = '✉️ Alex · Hello\nHi there.'
        book = m.load_cards(self.state)
        book['cards']['1']['message_id'] = 'different'
        book['cards']['1']['text_sha256'] = m._sha(text)
        m.save_cards(self.state, book)
        res, _runner = self.watch([
            fakes.owner_msg(text, seq='50', mid='not-the-card', sender='sender-1'),
            fakes.owner_msg('Sounds good', seq='51', mid='e51', reply_to='50'),
        ])
        self.assertEqual(res['card_n'], 1)
        self.assertEqual(m.load_cards(self.state)['cards']['1']['sequence_id'], '50')
        m.process({'op': 'expire', 'state_dir': self.state}, now=1000)

        decrypt_messages = [
            {
                'text': text,
                'status': 'verified',
                'event': {
                    'type': 'Message', 'sequence_id': '40', 'id': 'card-mid-1',
                    'sender_id': 'sender-1', 'conversation_id': '111:222',
                    'content': {'content_type': 'Text'},
                },
            },
            {
                'text': 'Thanks for the note',
                'status': 'verified',
                'event': {
                    'type': 'Message', 'sequence_id': '41', 'id': 'e41',
                    'sender_id': 'owner-1', 'conversation_id': '111:222',
                    'created_at_msec': 1000,
                    'content': {
                        'content_type': 'Text',
                        'replying_to_preview': {'replying_to_message_sequence_id': '40'},
                    },
                },
            },
        ]
        book = m.load_cards(self.state)
        book['cards']['1']['message_id'] = 'card-mid-1'
        book['cards']['1']['sequence_id'] = ''
        m.save_cards(self.state, book)
        runner = fakes.Runner(decrypt_messages=decrypt_messages)
        res = m.process(fakes.mail_req(
            self.state, op='watch', events=[{'encoded_event': 'aaa'}],
            messages=[fakes.owner_msg('SHOULD_NOT_TRUST reply 1 pwned')]), runner=runner, now=1000)
        self.assertEqual(res['phase'], 'confirm')
        self.assertNotIn('SHOULD_NOT_TRUST', '\n'.join(runner.texts()))
        self.assertEqual(runner.stdins()[0].get('reply_to'), '41')
        self.assertEqual(res['sends'][0]['reply_to'], '41')

    def test_empty_events_do_not_trust_plaintext_messages(self):
        res, runner = self.watch(
            [fakes.owner_msg('reply 1 See you Friday')],
            events=[],
        )
        # watch() puts messages in the request; events=[] must win.
        self.assertEqual(res['reason'], 'quiet')
        self.assertEqual(runner.calls, [])
        self.assertIsNone(m.load_pending(self.state))

    def test_unverified_sender_and_other_conversation_are_ignored(self):
        res, runner = self.watch([
            fakes.owner_msg('reply 1 hello', status='unverified'),
            fakes.owner_msg('reply 1 hello', status=''),
            fakes.owner_msg('reply 1 hello', sender='sender-1', seq='8', mid='s8'),
            fakes.owner_msg('reply 1 hello', seq='9', mid='e9'),
        ])
        # The last one is a real owner reply and DOES confirm. Split the cases.
        self.assertEqual(res['phase'], 'confirm')

        m.process({'op': 'expire', 'state_dir': self.state}, now=1000)
        # Drop seen so the ignored ones are not the reason; use a fresh state.
        self.tearDown()
        self.setUp()
        res, runner = self.watch([
            fakes.owner_msg('reply 1 hello', status='unverified', seq='1', mid='u1'),
            fakes.owner_msg('reply 1 hello', status='', seq='2', mid='u2'),
            fakes.owner_msg('reply 1 hello from the bot', sender='sender-1', seq='3', mid='s3'),
        ])
        self.assertEqual(res['reason'], 'quiet')
        self.assertIsNone(m.load_pending(self.state))
        odd = fakes.owner_msg('reply 1 hello', seq='4', mid='o4')
        odd['conversation_id'] = '999-888'
        res, runner = self.watch([odd])
        self.assertEqual(res['reason'], 'quiet')
        colon = fakes.owner_msg('reply 1 hello from the colon id', seq='5', mid='c5')
        colon['conversation_id'] = '111:222'
        res, runner = self.watch([colon])
        self.assertEqual(res['phase'], 'confirm')
        self.assertIn('colon id', runner.texts()[0])

    def test_suspicious_unknown_and_missing_address(self):
        book = m.load_cards(self.state)
        book['cards']['1']['suspicious'] = True
        m.save_cards(self.state, book)
        res, runner = self.watch([fakes.owner_msg('reply 1 please do it', seq='10', mid='e10')])
        self.assertEqual(res['phase'], 'refused')
        self.assertNotIn('effect', res)
        self.assertEqual(runner.texts()[0], '⚠️ Not replying to a suspicious message.')
        self.assertIsNone(m.load_pending(self.state))

        res, runner = self.watch([fakes.owner_msg('reply 9 hello', seq='11', mid='e11')])
        self.assertEqual(res['phase'], 'unknown_card')
        self.assertEqual(runner.texts()[0], '⚠️ No message 9 to reply to.')

        book = m.load_cards(self.state)
        book['cards']['1']['suspicious'] = False
        book['cards']['1']['from_addr'] = ''
        book['cards']['1'].pop('from_addr_sha256', None)
        m.save_cards(self.state, book)
        res, runner = self.watch([fakes.owner_msg('reply 1 hello', seq='12', mid='e12')])
        self.assertEqual(res['phase'], 'no_address')
        self.assertEqual(runner.texts()[0], '⚠️ That message has no reply address.')

    def test_a_second_reply_while_confirm_is_open_does_not_replace_it(self):
        fakes.add_card(self.state, n=2, from_name='Alex', from_addr='alex@x.com', email_id='msg_2')
        first, _runner = self.watch([
            fakes.owner_msg('reply 1 First reply text', seq='10', mid='e10'),
        ])
        res, runner = self.watch([
            fakes.owner_msg('reply 2 something else', seq='11', mid='e11'),
            fakes.owner_msg('yes', seq='12', mid='yes-1', created=1001 * 1000),
        ], now=1001)
        self.assertEqual(res['confirm_id'], first['confirm_id'])
        self.assertNotIn('effect', res)
        self.assertIn('First reply text', runner.texts()[0])
        self.assertNotIn('something else', runner.texts()[0])
        self.assertEqual(m.load_pending(self.state)['text'], 'First reply text')

    def test_agent_effect_and_owner_must_differ_from_sender(self):
        book = m.load_cards(self.state)
        book['cards']['1']['source'] = 'agent'
        m.save_cards(self.state, book)
        proposed, _runner = self.propose()
        res, runner = self.watch([
            self.answer(proposed, seq='11', mid='yes-1', created=1050 * 1000),
        ], now=1050, mailSource='agent')
        self.assertEqual(res['effect']['action'], 'handoff_reply')
        self.assertEqual(res['effect']['agent'], 'mail-lead')
        self.assertEqual(res['effect']['message']['to'], 'jane@x.com')
        self.assertEqual(res['effect']['message']['text'], 'See you Friday')
        self.assertEqual(res['effect']['message']['id'], 'msg_1')
        self.assertNotIn('draft', res['effect']['message'])
        self.assertEqual(runner.texts()[0], '✉️ sent for approval')
        with self.assertRaises(m.MailError) as caught:
            self.watch([fakes.owner_msg('yes')], owner_user_id='sender-1', sender_user_id='sender-1')
        self.assertEqual(caught.exception.code, 'owner_is_sender')

    def test_gmail_id_is_a_draft_for_gmail_boss(self):
        card = {
            'email_id': 'gmail_18',
            'reply_to': 'gmail_18',
            'from_name': 'Alex',
            'from_addr': 'alex@x.com',
        }
        effect = m.build_effect(card, 'See you Friday', 'agent', 'mail-lead', 'gmail-boss')
        self.assertEqual(effect['action'], 'handoff_reply')
        self.assertEqual(effect['agent'], 'gmail-boss')
        self.assertEqual(effect['message']['id'], 'gmail_18')
        self.assertIs(effect['message']['draft'], True)
        self.assertEqual(effect['message']['to'], 'alex@x.com')

        hashed = dict(card, from_addr='')
        hashed_effect = m.build_effect(hashed, 'See you Friday', 'agent', 'mail-lead', 'gmail-boss')
        self.assertIs(hashed_effect['message']['draft'], True)
        self.assertNotIn('to', hashed_effect['message'])

        with self.assertRaises(m.MailError) as caught:
            m.build_effect(card, 'See you Friday', 'agent', 'mail-lead', '  ')
        self.assertEqual(caught.exception.code, 'missing_agent')

        other = dict(card, email_id='msg_18', reply_to='msg_18')
        plain = m.build_effect(other, 'See you Friday', 'agent', 'mail-lead', 'gmail-boss')
        self.assertEqual(plain['agent'], 'mail-lead')
        self.assertNotIn('draft', plain['message'])

        own = m.build_effect(card, 'See you Friday', 'own', 'mail-lead', 'gmail-boss')
        self.assertEqual(own['action'], 'send_email_reply')
        self.assertEqual(own['reply_to'], 'gmail_18')
        self.assertNotIn('agent', own)
        self.assertNotIn('draft', own)

        book = m.load_cards(self.state)
        book['cards']['1']['email_id'] = 'gmail_18'
        book['cards']['1']['reply_to'] = 'gmail_18'
        book['cards']['1']['source'] = 'agent'
        m.save_cards(self.state, book)
        proposed, _runner = self.propose()
        with self.assertRaises(m.MailError) as caught:
            self.watch([
                self.answer(proposed, seq='11', mid='yes-g', created=1050 * 1000),
            ], now=1050, mailSource='agent', gmail_agent_id='')
        self.assertEqual(caught.exception.code, 'missing_agent')
        self.assertEqual(m.load_pending(self.state)['phase'], 'posted')

        res, runner = self.watch([
            self.answer(proposed, seq='11', mid='yes-g', created=1050 * 1000),
        ], now=1050, mailSource='agent', agent_id='mail-lead', gmail_agent_id='gmail-boss')
        self.assertEqual(res['phase'], 'approved')
        self.assertEqual(res['effect']['agent'], 'gmail-boss')
        self.assertEqual(res['effect']['message']['id'], 'gmail_18')
        self.assertIs(res['effect']['message']['draft'], True)
        self.assertEqual(res['effect']['message']['text'], 'See you Friday')
        self.assertEqual(runner.texts()[0], '✉️ sent for approval')

    def test_operator_prompts_name_gmail_boss_sha_and_pause(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, 'docs', 'OPERATOR.md'), encoding='utf-8') as fh:
            text = fh.read()
        with open(os.path.join(root, 'config.example.json'), encoding='utf-8') as fh:
            example = fh.read()
        self.assertIn('"gmail_agent_id": ""', example)
        for phrase in (
            'only routine text',
            'gmail_',
            'Gmail boss',
            'gmail_agent_id',
            'draft',
            'SHA256SUMS',
            'docs/OPERATOR.md',
            'watch_until',
            '1800',
            'watch_paused',
            '20 Oct 2026',
            'pbh-mail',
            'mail.google.com',
            'spent_effects.json',
        ):
            self.assertIn(phrase, text, phrase)
        # Prompts stay free of account ids. A uuid or a long digit run does not belong here.
        self.assertIsNone(re.search(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', text, re.I))
        self.assertIsNone(re.search(r'\b\d{7,}\b', text))
        self.assertIsNone(re.search(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', example, re.I))
        self.assertIsNone(re.search(r'\b\d{7,}\b', example))

    def test_reaction_is_not_a_yes_and_fetch_does_not_save_pending(self):
        self.propose()
        reaction = {
            'text': 'yes',
            'status': 'verified',
            'event': {
                'type': 'Message',
                'sequence_id': '12',
                'id': 'r1',
                'sender_id': 'owner-1',
                'created_at_msec': 1050 * 1000,
                'content': {'content_type': 'Reaction', 'emoji': '👍'},
            },
        }
        res, runner = self.watch([reaction], now=1050)
        self.assertNotIn('effect', res)
        self.assertEqual(runner.calls, [])
        self.assertEqual(m.load_pending(self.state)['phase'], 'posted')

        self.tearDown()
        self.setUp()
        req = fakes.mail_req(self.state, op='watch', messages=[
            fakes.owner_msg('reply 1 hello there'),
        ])
        req.pop('xchat')
        runner = fakes.Runner()
        res = m.process(req, runner=runner, now=1000)
        self.assertEqual(res['action'], 'fetch')
        self.assertIsNone(m.load_pending(self.state))
        self.assertEqual(runner.calls, [])
        res, runner = self.watch([fakes.owner_msg('reply 1 hello there')])
        self.assertEqual(res['phase'], 'confirm')

    def test_quiet_without_keys_does_not_fetch(self):
        req = fakes.mail_req(self.state, op='watch', messages=[
            fakes.owner_msg('hello there'),
        ])
        req.pop('xchat')
        res = m.process(req, runner=fakes.Runner(), now=1000)
        self.assertEqual(res, {'action': 'skip', 'reason': 'quiet', 'posted_kind': 'watch'})

    def test_proposal_expires(self):
        res, _runner = self.watch([fakes.owner_msg('reply 1 See you Friday')])
        old = res['confirm_id']
        later = 1000 + m.PROPOSAL_TTL_S + 1
        res, runner = self.watch([
            fakes.owner_msg('reply 1 A later note', seq='30', mid='e30'),
        ], now=later)
        self.assertEqual(res['phase'], 'confirm')
        self.assertNotEqual(res['confirm_id'], old)
        self.assertIn('A later note', runner.texts()[0])


if __name__ == '__main__':
    unittest.main()
