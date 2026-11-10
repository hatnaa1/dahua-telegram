"""Offline regression tests; no camera or Telegram credentials required."""
import asyncio
import contextlib
import http.client
import io
import json
import os
import threading
import unittest
from email.message import EmailMessage
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from aiosmtpd.smtp import LoginPassword

import app
import configure_dahua as cli


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            'TELEGRAM_TOKEN': 'fake', 'TELEGRAM_CHAT_ID': '-1002907522749',
            'COOLDOWN_S': '15'}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.bridge = app.Bridge()
        self.bridge.api = Mock()

    def test_required_configuration(self):
        for values in ({'TELEGRAM_TOKEN': ''}, {'TELEGRAM_CHAT_ID': ''},
                       {'SMTP_USER': 'camera'}, {'COOLDOWN_S': '-1'},
                       {'COOLDOWN_S': 'nan'}, {'COOLDOWN_S': 'inf'}):
            with self.subTest(values=values), patch.dict(os.environ, values):
                with self.assertRaises(ValueError):
                    app.Bridge()

    def test_authentication(self):
        self.bridge.user, self.bridge.password = 'camera', 'secret'
        for credentials, expected in ((LoginPassword(b'camera', b'secret'), True),
                                      (LoginPassword(b'camera', b'wrong'), False),
                                      (None, False)):
            self.assertEqual(self.bridge.authenticate(None, None, None, 'LOGIN', credentials).success, expected)

    def test_cooldown_is_per_camera_and_event(self):
        with patch('app.time.monotonic', return_value=100):
            self.assertTrue(self.bridge.deliver(' Motion ', 'alarm', device='Front'))
            self.assertFalse(self.bridge.deliver('motion', 'alarm', device='FRONT'))
            self.assertTrue(self.bridge.deliver('motion', 'alarm', device='Back'))
            self.assertTrue(self.bridge.deliver('tamper', 'alarm', device='Front'))
            self.assertTrue(self.bridge.deliver('motion', 'test', device='Front', test=True))
        with patch('app.time.monotonic', return_value=115):
            self.assertTrue(self.bridge.deliver('motion', 'alarm', device='Front'))

    def test_failed_delivery_does_not_consume_cooldown(self):
        self.bridge.api.side_effect = app.DeliveryError('failed')
        with self.assertRaises(app.DeliveryError):
            self.bridge.deliver('motion', 'alarm')
        self.assertEqual(self.bridge.last, {})

    def test_image_batching_and_caption_limits(self):
        photo = ('snapshot.jpg', b'jpeg', 'image/jpeg')
        self.bridge.deliver('motion', 'x' * 2000, [photo] * 21)
        calls = self.bridge.api.call_args_list
        self.assertEqual([c.args[0] for c in calls], ['sendMediaGroup', 'sendMediaGroup', 'sendPhoto'])
        media = json.loads(calls[0].args[1]['media'])
        self.assertEqual(len(media), 10)
        self.assertEqual(len(media[0]['caption']), 1024)
        self.assertNotIn('caption', media[1])
        self.assertEqual(set(calls[0].args[2]), {f'p{i}' for i in range(10)})

    def test_text_limit(self):
        self.bridge.deliver('motion', 'alarm', text='x' * 5000)
        self.assertEqual(len(self.bridge.api.call_args.args[1]['text']), 4096)

    def test_mail_identity_precedence(self):
        cases = [(['Camera Name: Gate'], 'Lobby <lobby@camera.local>', '[Front] Alarm', 'Gate'),
                 ([], 'Lobby <lobby@camera.local>', '[Front] Alarm', 'Lobby'),
                 ([], 'lobby@camera.local', '[Front] Alarm', 'Front'),
                 ([], 'lobby@camera.local', 'Alarm', 'lobby@camera.local'),
                 ([], '', 'Alarm', 'Alarm'), ([], '', '', 'Fallback')]
        for bodies, sender, subject, expected in cases:
            with self.subTest(expected=expected):
                message = EmailMessage()
                message['From'], message['Subject'] = sender, subject
                self.assertEqual(app.mail_identity(message, bodies, 'Fallback'), expected)

    def test_mime_mail_and_test_subject(self):
        message = EmailMessage()
        message['From'] = 'front@camera.local'
        message['Subject'] = '[Front] TEST snapshot'
        message.set_content('Device: Entrance\nMotion detected')
        message.add_attachment(b'jpeg', maintype='image', subtype='jpeg', filename='a.jpg')
        message.add_attachment(b'png', maintype='application', subtype='octet-stream', filename='B.PNG')
        message.add_attachment(b'ignore', maintype='application', subtype='pdf', filename='a.pdf')
        self.bridge.deliver = Mock(return_value=True)
        self.assertTrue(self.bridge.mail(message.as_bytes()))
        args, kwargs = self.bridge.deliver.call_args
        self.assertEqual(len(args[2]), 2)
        self.assertEqual(args[2][1], ('snapshot.png', b'png', 'image/png'))
        self.assertEqual(kwargs, {'test': True, 'text': None, 'device': 'Entrance'})

    def test_plain_mail_unknown_charset(self):
        self.bridge.deliver = Mock()
        self.bridge.mail(b'Subject: Motion\r\nContent-Type: text/plain; charset=unknown\r\n\r\nCamera: Gate')
        self.assertEqual(self.bridge.deliver.call_args.kwargs['device'], 'Gate')
        self.assertIn('Camera: Gate', self.bridge.deliver.call_args.kwargs['text'])

    def test_api_errors_are_sanitized(self):
        real_api = app.Bridge.api
        with patch('app.requests.post') as post:
            response = post.return_value.__enter__.return_value
            response.ok = True
            response.json.return_value = {'ok': True}
            real_api(self.bridge, 'sendMessage', {'text': 'hello'})
            self.assertEqual(post.call_args.kwargs['timeout'], (5, 30))
            for ok, payload in ((False, {'ok': False}), (True, {'ok': False})):
                response.ok, response.json.return_value = ok, payload
                with self.assertRaises(app.DeliveryError):
                    real_api(self.bridge, 'sendMessage', {})
            post.side_effect = requests.ConnectionError('SECRET URL')
            with self.assertRaises(app.DeliveryError) as caught:
                real_api(self.bridge, 'sendMessage', {})
            self.assertNotIn('SECRET', str(caught.exception))

    def test_smtp_statuses(self):
        handler = app.MailHandler(self.bridge)
        envelope = SimpleNamespace(original_content=b'email')
        self.bridge.mail = Mock(return_value=False)
        self.assertTrue(asyncio.run(handler.handle_DATA(None, None, envelope)).startswith('250'))
        self.bridge.mail.side_effect = app.DeliveryError('failed')
        with self.assertLogs('dahua', level='ERROR'):
            self.assertTrue(asyncio.run(handler.handle_DATA(None, None, envelope)).startswith('451'))


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.bridge = SimpleNamespace(webhook_token='secret', device='Default', deliver=Mock(return_value=True))
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), app.http_handler(self.bridge))
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join()

    def request(self, path, method='GET', body=None, headers=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_routes_and_auth(self):
        self.assertEqual(self.request('/health'), (200, {'status': 'ok'}))
        self.assertEqual(self.request('/missing')[0], 404)
        self.assertEqual(self.request('/event')[0], 403)
        self.assertEqual(self.request('/event?token=wrong')[0], 403)
        self.bridge.deliver.assert_not_called()

    def test_event_and_cooldown(self):
        self.assertEqual(self.request('/event?token=secret&camera=Gate&type=tamper&subject=Alarm&test=1'),
                         (200, {'status': 'sent'}))
        self.bridge.deliver.assert_called_once_with('tamper', 'Alarm', [], test=True, device='Gate')
        self.bridge.deliver.return_value = False
        self.assertEqual(self.request('/event?token=secret')[1]['status'], 'cooldown')

    def test_image_and_rejected_bodies(self):
        for data, mime in ((b'\xff\xd8\xffphoto', 'image/jpeg'), (b'\x89PNG\r\n\x1a\nphoto', 'image/png')):
            self.assertEqual(self.request('/event?token=secret', 'POST', data)[0], 200)
            self.assertEqual(self.bridge.deliver.call_args.args[2][0][2], mime)
        for headers, status in (({'Content-Length': 'bad'}, 400),
                                ({'Content-Length': '-1'}, 413),
                                ({'Content-Length': str(app.MAX_BYTES + 1)}, 413),
                                ({'Transfer-Encoding': 'chunked'}, 400)):
            self.assertEqual(self.request('/event?token=secret', 'POST', headers=headers)[0], status)
        self.assertEqual(self.request('/event?token=secret', 'POST', b'bad',
                                      {'Content-Type': 'application/octet-stream'})[0], 415)

    def test_delivery_failure(self):
        self.bridge.deliver.side_effect = app.DeliveryError('private')
        with self.assertLogs('dahua', level='ERROR'):
            self.assertEqual(self.request('/event?token=secret'),
                             (502, {'error': 'delivery failed; retry later'}))


class ConfigureTests(unittest.TestCase):
    def test_apply_alias_and_readback(self):
        values = cli.desired_config({})
        for flag in ('--configure', '--apply'):
            with self.subTest(flag=flag), patch.dict(os.environ, {'DAHUA_PASSWORD': 'fake'}, clear=True), \
                    patch.object(cli, 'load_dotenv'), patch.object(cli, 'Camera') as camera_class, \
                    contextlib.redirect_stdout(io.StringIO()):
                camera = camera_class.return_value
                camera.read.side_effect = lambda name: {k: v for k, v in values.items() if k.startswith(name)}
                self.assertEqual(cli.main([flag]), 0)
                camera.write.assert_called_once_with(values)
                camera.session.close.assert_called_once()

    def test_missing_fields_prevent_writes(self):
        camera = Mock()
        with self.assertRaises(cli.CameraError):
            cli.apply(camera, {'Email.Enable': 'true'}, {})
        camera.write.assert_not_called()

    def test_readback_mismatch_and_password_mask(self):
        camera = Mock()
        camera.read.return_value = {'Email.Enable': 'false'}
        with self.assertRaises(cli.CameraError):
            cli.apply(camera, {'Email.Enable': 'true'}, {'Email.Enable': 'false'})
        self.assertEqual(cli.masked({'Email.Password': 'secret', 'Email.Title': 'Gate'}),
                         {'Email.Password': '********', 'Email.Title': 'Gate'})

    def test_invalid_settings(self):
        for env in ({'SMTP_USER': 'user'}, {'DAHUA_SMTP_PORT': '0'}, {'DAHUA_MOTION_LEVEL': '7'}):
            with self.subTest(env=env), self.assertRaises(cli.CameraError):
                cli.desired_config(env)
        with self.assertRaises(cli.CameraError):
            cli.Camera('http://camera/secret', 'admin', 'fake')


if __name__ == '__main__':
    unittest.main()
