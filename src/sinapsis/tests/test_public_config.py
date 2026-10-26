"""Offline AST contract checks; no backend startup, secrets or database access."""
import ast
import copy
from pathlib import Path
import types
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'backend/server.py'
CONSTANTS = {'ADMIN_USER', 'BUS_HUMAN_READERS', 'BUS_HUMAN_PUBLISHERS',
             'CHAT_HUMAN_SENDERS', '_CHAT_THINKING_OK', '_CHAT_CMD_WRITE_AGENTS'}
FUNCTIONS = {'_is_bus_reader', '_is_bus_publisher', '_is_chat_sender', 'me'}


class Cursor:
    def __init__(self, row): self.row = row
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def execute(self, *args): pass
    def fetchone(self): return self.row
    def fetchall(self): return []


class Connection(Cursor):
    def cursor(self): return Cursor(self.row)


def candidate(env=None, name='admin', kind='human', role='admin'):
    row = {'id': 1, 'name': name, 'kind': kind, 'role': role, 'email': None,
           'team': None, 'is_lead': False, 'must_change_password': False}
    namespace = {'os': types.SimpleNamespace(environ=dict(env or {})),
                 '_check': lambda auth: row, '_pg': lambda: Connection(row)}
    nodes = []
    for original in ast.parse(SOURCE.read_text()).body:
        node = copy.deepcopy(original)
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in CONSTANTS for t in node.targets):
            nodes.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS:
            node.decorator_list = []
            node.args.defaults = [ast.Constant(None) for _ in node.args.defaults]
            nodes.append(node)
    exec(compile(ast.fix_missing_locations(ast.Module(nodes, type_ignores=[])), str(SOURCE), 'exec'), namespace)
    return namespace, row


class PublicConfig(unittest.TestCase):
    def test_default_admin_and_three_nominal_lists(self):
        ns, row = candidate()
        self.assertEqual(ns['ADMIN_USER'], 'admin')
        for name in ('BUS_HUMAN_READERS', 'BUS_HUMAN_PUBLISHERS', 'CHAT_HUMAN_SENDERS'):
            self.assertEqual(ns[name], ('admin',))
        self.assertTrue(ns['me']()['is_admin'])

    def test_custom_admin_drives_lists_and_me(self):
        ns, row = candidate({'ADMIN_USER': 'operator'}, name='operator')
        for name in ('BUS_HUMAN_READERS', 'BUS_HUMAN_PUBLISHERS', 'CHAT_HUMAN_SENDERS'):
            self.assertEqual(ns[name], ('operator',))
        me = ns['me']()
        self.assertTrue(me['is_admin'])
        self.assertEqual(me['admin_user'], 'operator')
        self.assertTrue(me['can_read_bus'] and me['can_publish_bus'] and me['can_use_chat'])

    def test_admin_role_on_agent_is_not_named_human_admin(self):
        ns, row = candidate(kind='agent')
        me = ns['me']()
        self.assertFalse(me['is_admin'])
        self.assertFalse(me['can_read_bus'] or me['can_publish_bus'] or me['can_use_chat'])

    def test_other_human_admin_does_not_inherit_owner_or_bus(self):
        ns, row = candidate(name='other-admin')
        me = ns['me']()
        self.assertFalse(me['is_admin'])
        self.assertFalse(me['can_read_bus'] or me['can_publish_bus'] or me['can_use_chat'])

    def test_explicit_reader_override_does_not_grant_publish_or_chat(self):
        ns, row = candidate({'BUS_HUMAN_READERS': 'other-admin'}, name='other-admin')
        me = ns['me']()
        self.assertTrue(me['can_read_bus'])
        self.assertFalse(me['can_publish_bus'] or me['can_use_chat'])

    def test_no_private_runtime_or_thinking_consent_defaults(self):
        ns, _ = candidate()
        self.assertEqual(ns['_CHAT_THINKING_OK'], set())
        self.assertEqual(ns['_CHAT_CMD_WRITE_AGENTS'], set())

    def test_migration_is_idempotent_and_contains_bff_columns(self):
        sql = (SOURCE.parent / 'migrations/001_sessions.sql').read_text().lower()
        self.assertIn('create table if not exists public.sessions', sql)
        self.assertIn('references public.agents(id)', sql)
        for column in ('id', 'principal_id', 'token_hash', 'method', 'expires_at', 'revoked_at'):
            self.assertIn(column, sql)
        self.assertIn('create index if not exists', sql)


if __name__ == '__main__':
    unittest.main()
