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
            'COOLDOWN_S': '15', 'BURST_WINDOW_S': '0'}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.bridge = app.Bridge()
        self.bridge.api = Mock()

    def test_required_configuration(self):
        for values in ({'TELEGRAM_TOKEN': ''}, {'TELEGRAM_CHAT_ID': ''},
                       {'SMTP_USER': 'camera'}, {'COOLDOWN_S': '-1'},
                       {'COOLDOWN_S': 'nan'}, {'COOLDOWN_S': 'inf'},
                       {'BURST_WINDOW_S': '-1'}, {'BURST_WINDOW_S': 'nan'},
                       {'BURST_WINDOW_S': 'inf'}, {'MAX_BURST_PHOTOS': '0'}):
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
        self.bridge.mail(b'Subject: Test\r\nContent-Type: text/plain; charset=unknown\r\n\r\nCamera: Gate')
        self.assertEqual(self.bridge.deliver.call_args.kwargs['device'], 'Gate')
        self.assertIn('Camera: Gate', self.bridge.deliver.call_args.kwargs['text'])

    def snapshot_mail(self, photo=None, subject='Motion Detect', device='Gate'):
        message = EmailMessage()
        message['Subject'] = subject
        message.set_content(f'Camera: {device}')
        if photo is not None:
            message.add_attachment(photo, maintype='image', subtype='jpeg', filename='a.jpg')
        return message.as_bytes()

    def test_text_only_alarms_do_not_consume_cooldown(self):
        for subject in ('Alarm Event: Motion Detect', 'Alarm End'):
            self.assertFalse(self.bridge.mail(self.snapshot_mail(subject=subject)))
        self.bridge.api.assert_not_called()
        self.assertEqual(self.bridge.last, {})
        self.assertTrue(self.bridge.mail(self.snapshot_mail(b'actual snapshot')))
        self.bridge.api.assert_called_once()
        self.assertEqual(self.bridge.api.call_args.args[0], 'sendPhoto')
        self.assertFalse(self.bridge.mail(self.snapshot_mail(b'late snapshot')))

    def test_text_only_test_email_sends_without_cooldown(self):
        self.assertTrue(self.bridge.mail(self.snapshot_mail(subject='SMTP TEST')))
        self.assertEqual(self.bridge.api.call_args.args[0], 'sendMessage')
        self.assertEqual(self.bridge.last, {})

    def test_burst_combines_snapshots_and_caps_album(self):
        self.bridge.burst_window = 2.5
        with patch('app.threading.Timer') as timer:
            for photo in (b'one', b'two', b'three', b'excess'):
                self.assertTrue(self.bridge.mail(self.snapshot_mail(photo)))
            timer.assert_called_once()
            self.bridge.api.assert_not_called()
            self.assertEqual(self.bridge.last, {})
            delay, callback = timer.call_args.args
            self.assertEqual(delay, 2.5)
            callback(*timer.call_args.kwargs['args'])
        self.bridge.api.assert_called_once()
        method, data, files = self.bridge.api.call_args.args
        self.assertEqual(method, 'sendMediaGroup')
        self.assertEqual(len(json.loads(data['media'])), 3)
        self.assertEqual([photo[1] for photo in files.values()], [b'one', b'two', b'three'])
        self.assertEqual(self.bridge.bursts, {})
        self.assertIn(('gate', 'motion'), self.bridge.last)

    def test_bursts_are_separate_per_device_and_tests_bypass_them(self):
        self.bridge.burst_window = 2.5
        with patch('app.threading.Timer') as timer:
            self.bridge.mail(self.snapshot_mail(b'gate', device='Gate'))
            self.bridge.mail(self.snapshot_mail(b'back', device='Back'))
            self.assertEqual(timer.call_count, 2)
            self.assertTrue(self.bridge.mail(self.snapshot_mail(subject='Test')))
            self.assertEqual(self.bridge.api.call_args.args[0], 'sendMessage')
            for call in timer.call_args_list:
                call.args[1](*call.kwargs['args'])
        self.assertEqual([call.args[0] for call in self.bridge.api.call_args_list],
                         ['sendMessage', 'sendPhoto', 'sendPhoto'])

    def test_burst_failure_does_not_consume_cooldown(self):
        self.bridge.burst_window = 2.5
        self.bridge.api.side_effect = app.DeliveryError('failed')
        with patch('app.threading.Timer') as timer:
            self.bridge.mail(self.snapshot_mail(b'photo'))
            with self.assertLogs('dahua', level='ERROR'):
                timer.call_args.args[1](*timer.call_args.kwargs['args'])
        self.assertEqual(self.bridge.last, {})
        self.assertEqual(self.bridge.bursts, {})

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

    def test_omitted_receiver_is_written_and_verified(self):
        values = cli.desired_config({})
        current = {k: v for k, v in values.items() if k != 'Email.Receivers[0]'}
        for receiver in (values['Email.Receivers[0]'], 'wrong@camera.local', None):
            with self.subTest(receiver=receiver):
                camera = Mock()
                actual = dict(current)
                if receiver is not None:
                    actual['Email.Receivers[0]'] = receiver
                camera.read.side_effect = lambda name: {
                    k: v for k, v in actual.items() if k.startswith(name)}
                if receiver == values['Email.Receivers[0]']:
                    cli.apply(camera, values, current)
                else:
                    with self.assertRaisesRegex(cli.CameraError, r'Read-back mismatch: Email\.Receivers\[0\]'):
                        cli.apply(camera, values, current)
                camera.initialize_receiver.assert_called_once_with(values['Email.Receivers[0]'])
                camera.write.assert_called_once_with(values)
                self.assertEqual(camera.read.call_args_list,
                                 [unittest.mock.call('Email'), unittest.mock.call('MotionDetect')])

    def test_readback_mismatch_and_password_mask(self):
        camera = Mock()
        camera.read.return_value = {'Email.Enable': 'false'}
        with self.assertRaises(cli.CameraError):
            cli.apply(camera, {'Email.Enable': 'true'}, {'Email.Enable': 'false'})
        self.assertEqual(cli.masked({'Email.Password': 'secret', 'Email.Title': 'Gate'}),
                         {'Email.Password': '********', 'Email.Title': 'Gate'})

    def test_smtp_defaults_and_overrides(self):
        for env, expected in (({}, ('dahua', 'dahua123')),
                              ({'SMTP_USER': '', 'SMTP_PASS': ''}, ('dahua', 'dahua123')),
                              ({'SMTP_USER': 'bridge', 'SMTP_PASS': 'secret'}, ('bridge', 'secret')),
                              ({'DAHUA_SMTP_USER': 'camera', 'DAHUA_SMTP_PASS': 'other'}, ('camera', 'other'))):
            with self.subTest(env=env):
                values = cli.desired_config(env)
                self.assertEqual((values['Email.UserName'], values['Email.Password']), expected)
                self.assertEqual(values['Email.Anonymous'], 'false')
                self.assertEqual(values['Email.Authentication'], 'true')

    def test_rpc_md5_login_and_test_mail(self):
        import hashlib
        camera = cli.Camera('192.168.1.50', 'admin', 'secret')
        self.addCleanup(camera.session.close)
        responses = [
            {'session': 42, 'params': {'realm': 'Login', 'random': 'nonce'}},
            {'result': True}, {'result': True}]
        camera.session.post = Mock(side_effect=[SimpleNamespace(status_code=200, json=lambda r=r: r)
                                               for r in responses])
        camera.test_mail()
        calls = camera.session.post.call_args_list
        md5 = lambda s: hashlib.md5(s.encode()).hexdigest().upper()
        self.assertEqual(calls[1].kwargs['json']['params']['password'],
                         md5('admin:nonce:' + md5('admin:Login:secret')))
        self.assertTrue(calls[0].args[0].endswith('/RPC2_Login'))
        self.assertTrue(calls[1].args[0].endswith('/RPC2_Login'))
        self.assertTrue(calls[2].args[0].endswith('/RPC2'))
        self.assertEqual(calls[2].kwargs['json']['method'], 'netApp.sendTestMail')
        self.assertEqual(calls[2].kwargs['json']['session'], 42)

    def test_receiver_rpc_preserves_email_table(self):
        camera = cli.Camera('192.168.1.50', 'admin', 'secret')
        self.addCleanup(camera.session.close)
        camera.rpc = Mock(return_value={'params': {'table': {'Enable': True, 'Receivers': []}}})
        camera.initialize_receiver('alerts@camera.local')
        camera.rpc.assert_called_with('configManager.setConfig', {
            'name': 'Email', 'table': {'Enable': True, 'Receivers': ['alerts@camera.local']}})

    def test_rpc_failures_are_sanitized(self):
        camera = cli.Camera('192.168.1.50', 'admin', 'secret')
        self.addCleanup(camera.session.close)
        camera.session.post = Mock(side_effect=requests.ConnectionError('secret'))
        with self.assertRaises(cli.CameraError) as caught:
            camera.test_mail()
        self.assertNotIn('secret', str(caught.exception))
        camera.rpc_request = Mock(return_value={'result': False})
        with self.assertRaisesRegex(cli.CameraError, 'challenge missing'):
            camera.test_mail()
        camera.rpc_request.side_effect = [
            {'session': 42, 'params': {'realm': 'Login', 'random': 'nonce'}},
            {'result': False}]
        with self.assertRaisesRegex(cli.CameraError, 'login rejected'):
            camera.test_mail()
        self.assertIsNone(camera.rpc_session)
        camera.rpc_session = 42
        camera.rpc_request.side_effect = None
        with self.assertRaisesRegex(cli.CameraError, 'request rejected'):
            camera.test_mail()

    def test_receiver_initialization_failure_prevents_cgi_write(self):
        values = cli.desired_config({})
        camera = Mock()
        camera.initialize_receiver.side_effect = cli.CameraError('RPC rejected')
        current = {k: v for k, v in values.items() if k != 'Email.Receivers[0]'}
        with self.assertRaises(cli.CameraError):
            cli.apply(camera, values, current)
        camera.write.assert_not_called()

    def test_test_mail_cli(self):
        with patch.dict(os.environ, {'DAHUA_PASSWORD': 'fake'}, clear=True), \
                patch.object(cli, 'load_dotenv'), patch.object(cli, 'Camera') as factory, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['--test-mail']), 0)
            factory.return_value.test_mail.assert_called_once_with()
            factory.return_value.read.assert_not_called()
            factory.return_value.session.close.assert_called_once_with()

    def test_invalid_settings(self):
        for env in ({'SMTP_USER': 'user'}, {'DAHUA_SMTP_PORT': '0'}, {'DAHUA_MOTION_LEVEL': '7'}):
            with self.subTest(env=env), self.assertRaises(cli.CameraError):
                cli.desired_config(env)
        with self.assertRaises(cli.CameraError):
            cli.Camera('http://camera/secret', 'admin', 'fake')


if __name__ == '__main__':
    unittest.main()
