"""Offline contract tests: execute the exact BFF functions from the candidate AST."""
import ast
import asyncio
import base64
import binascii
from pathlib import Path
import types
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'backend/server.py'
NAMES = {'_chat_image_capability', 'api_chat_capabilities', 'api_chat_send'}

class HTTPException(Exception):
    def __init__(self, status_code, detail):
        self.status_code, self.detail = status_code, detail

class FakeResponse:
    def __init__(self, data=None, status_code=200):
        self.data, self.status_code = data, status_code
        self.content, self.headers = b'{}', {'content-type': 'application/json'}
    def json(self): return self.data

class Tests(unittest.TestCase):
    def setUp(self):
        funcs = [n for n in ast.parse(SOURCE.read_text()).body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in NAMES]
        for f in funcs:
            f.decorator_list = []
            f.args.defaults = [ast.Constant(value=None) for _ in f.args.defaults]
        self.calls = []
        def get(url, **kwargs):
            self.calls.append(('GET', url))
            return FakeResponse({'provider': self.provider, 'images': {'supported': self.bridge_support, 'max_b64': 8 * 1024 * 1024, 'mimes': ['image/png']}})
        def post(url, **kwargs):
            self.calls.append(('POST', url, kwargs.get('json')))
            return FakeResponse(status_code=202)
        self.provider, self.bridge_support = 'amazon-bedrock', True
        self.ns = {
            'httpx': types.SimpleNamespace(get=get, post=post, HTTPError=OSError, TimeoutException=TimeoutError),
            'base64': base64, 'binascii': binascii,
            '_CHAT_TIMEOUT': 5, '_CHAT_IMAGE_MIMES': {'image/png', 'image/jpeg', 'image/gif', 'image/webp'},
            '_CHAT_IMAGE_MAX_B64': 8 * 1024 * 1024, '_chat_agents_conf': lambda: {'assistant-g': {'url': 'http://bridge', 'token': 'test'}},
            '_require_chat_sender': lambda auth: {'name': 'pilot'}, '_pg': lambda: DummyContext(),
            '_audit': lambda *a, **k: None, 'ADMIN_USER': 'pilot', '_chat_health': lambda *a: {'status': 'respondió'},
            'CHAT_ST_OK': 'respondiendo', 'CHAT_ST_DONE': 'respondió', 'CHAT_ST_ASLEEP': 'dormido',
            'Response': lambda **kwargs: kwargs, 'HTTPException': HTTPException,
            'Header': lambda *a: None, 'Query': lambda *a, **k: None, 'Request': object,
        }
        exec(compile(ast.fix_missing_locations(ast.Module(body=funcs, type_ignores=[])), str(SOURCE), 'exec'), self.ns)
    def send(self, body):
        return asyncio.run(self.ns['api_chat_send'](types.SimpleNamespace(json=lambda: async_body(body)), None))
    def test_text_only_no_capabilities_dependency(self):
        self.send({'agent': 'assistant-g', 'message': 'hola'})
        self.assertEqual([c[0] for c in self.calls], ['POST'])
        self.assertEqual(self.calls[-1][2], {'message': 'hola'})
    def test_native_image_forwarded_only_after_capability(self):
        self.send({'agent': 'assistant-g', 'message': 'mira', 'images': [{'mimeType': 'image/png', 'data': 'AA=='}]})
        self.assertEqual([c[0] for c in self.calls], ['GET', 'POST'])
        self.assertEqual(self.calls[-1][2]['images'][0]['data'], 'AA==')
    def test_unsupported_rejected_without_post(self):
        self.provider = 'kiro-gateway'
        with self.assertRaises(HTTPException) as ctx:
            self.send({'agent': 'assistant-g', 'message': 'mira', 'images': [{'mimeType': 'image/png', 'data': 'AA=='}]})
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual([c[0] for c in self.calls], ['GET'])
    def test_range_count_size_mime_without_delegation(self):
        for images in ([{'mimeType':'image/png','data':'AA=='}] * 9,
                       [{'mimeType':'image/png','data':'A' * (8 * 1024 * 1024 + 1)}],
                       [{'mimeType':'image/svg+xml','data':'AA=='}]):
            self.calls.clear()
            with self.assertRaises(HTTPException) as ctx:
                self.send({'agent': 'assistant-g', 'message': 'mira', 'images': images})
            self.assertEqual(ctx.exception.status_code, 422)
            self.assertEqual(self.calls, [])
    def test_malformed_base64_never_delegated(self):
        for data in ('====', 'AA=A', 'A==='):
            with self.subTest(data=data):
                self.calls.clear()
                with self.assertRaises(HTTPException) as ctx:
                    self.send({'agent': 'assistant-g', 'message': 'mira', 'images': [{'mimeType': 'image/png', 'data': data}]})
                self.assertEqual(ctx.exception.status_code, 422)
                self.assertEqual(self.calls, [])
    def test_data_url_accepted_and_mime_mismatch_rejected(self):
        self.send({'agent': 'assistant-g', 'message': 'mira', 'images': [{'mimeType': 'image/png', 'data': 'data:image/png;base64,AA=='}]})
        self.assertEqual(self.calls[-1][2]['images'][0]['data'], 'data:image/png;base64,AA==')
        self.calls.clear()
        with self.assertRaises(HTTPException) as ctx:
            self.send({'agent': 'assistant-g', 'message': 'mira', 'images': [{'mimeType': 'image/png', 'data': 'data:image/jpeg;base64,AA=='}]})
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(self.calls, [])
    def test_image_only_uses_nonempty_bridge_prompt(self):
        self.send({'agent': 'assistant-g', 'message': '', 'images': [{'mimeType': 'image/png', 'data': 'AA=='}]})
        self.assertEqual(self.calls[-1][2]['message'], 'Adjunto imágenes')
    def test_missing_capabilities_fails_closed_but_text_remains_available(self):
        self.ns['httpx'].get = lambda *a, **k: FakeResponse(status_code=404)
        with self.assertRaises(HTTPException) as ctx:
            self.send({'agent': 'assistant-g', 'message': 'mira', 'images': [{'mimeType': 'image/png', 'data': 'AA=='}]})
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertFalse(any(c[0] == 'POST' for c in self.calls))
        self.send({'agent': 'assistant-g', 'message': 'hola'})
        self.assertEqual(self.calls[-1][2], {'message': 'hola'})
    def test_capability_is_gated(self):
        self.ns['_require_chat_sender'] = lambda auth: (_ for _ in ()).throw(HTTPException(403,'denied'))
        self.ns['_chat_denied_audit'] = lambda *a: None
        with self.assertRaises(HTTPException) as ctx:
            self.ns['api_chat_capabilities']('assistant-g', None)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(self.calls, [])

class DummyContext:
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def cursor(self): return self
    def commit(self): pass

async def async_body(value): return value

if __name__ == '__main__': unittest.main()
