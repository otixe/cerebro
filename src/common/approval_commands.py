"""Approval DB adapter. INTERNAL: auth boundaries alone call trusted_principal; no HTTP/MCP model instantiates ApprovalPrincipal.
No authentication takes place here. Caller supplies a fresh non-autocommit connection factory per DB role.
No replay of a failed operation; reconcile is a distinct explicit read via a SERVICE pool (not approver).
"""
from __future__ import annotations
import base64
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
import re
import unicodedata
from uuid import UUID

import psycopg

_ENABLED = os.environ.get('APPROVAL_API_ENABLED') == 'true'
HUMAN_APPROVER = os.environ.get('APPROVAL_HUMAN_NAME', 'admin').strip().lower()
def approval_enabled():
    return _ENABLED

class ApprovalError(Exception):
    def __init__(self, code, status):
        self.code, self.status = code, status
        super().__init__(code)
class OutcomeUnknown(ApprovalError):
    def __init__(self):
        super().__init__('outcome_unknown', 503)

def map_error(exc):
    state = getattr(exc, 'sqlstate', None)
    if state in ('PZ101', 'PZ102'):
        return ApprovalError('approval_request_immutable' if state == 'PZ101' else 'approval_state_conflict', 409)
    if state == '42501':
        return ApprovalError('approval_forbidden', 403)
    if state == '23505':
        constraint = getattr(getattr(exc, 'diag', None), 'constraint_name', None)
        if constraint == 'tasks_pkey': return ApprovalError('request_id_conflict', 409)
        if constraint == 'tasks_approval_replaces_unique': return ApprovalError('approval_state_conflict', 409)
        return ApprovalError('approval_integrity_error', 500)
    if isinstance(exc, psycopg.OperationalError):
        return ApprovalError('approval_unavailable', 503)
    return ApprovalError('approval_error', 500)

_PRINCIPAL_TOKEN = object()
@dataclass(frozen=True, init=False)
class ApprovalPrincipal:
    name: str
    kind: str
    channel: str
    def __init__(self, name, kind, channel, *, _token=None):
        if _token is not _PRINCIPAL_TOKEN or not isinstance(name, str) or not name.strip() or kind not in ('human', 'agent') or channel not in ('bff','rest','mcp','mcp_sse'):
            raise ValueError('trusted auth boundary required')
        object.__setattr__(self, 'name', name)
        object.__setattr__(self, 'kind', kind)
        object.__setattr__(self, 'channel', channel)

def trusted_principal(name, kind, channel):
    """Internal factory: call ONLY after session/key verification; never from request or tool args."""
    return ApprovalPrincipal(name, kind, channel, _token=_PRINCIPAL_TOKEN)

def _text(v):
    if not isinstance(v,str) or not v or v != v.strip() or any(ord(c)<32 or ord(c)==127 for c in v): raise ValueError('invalid text')
    return unicodedata.normalize('NFC',v)

def _time(v):
    if not isinstance(v,str) or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|[+-]\d\d:\d\d)',v): raise ValueError('invalid time')
    d=datetime.fromisoformat(v.replace('Z','+00:00'))
    if d.utcoffset() is None: raise ValueError('invalid offset')
    return d

@dataclass(frozen=True)
class Scope:
    action_type: str
    resource_kind: str
    resource_id: str
    constraints: dict
    scope_version: int = 1
    def __post_init__(self):
        a,r=self.action_type,self.resource_kind
        if a not in ('maintenance_window','destructive_fs_action','other_explicit') or r not in ('folder','file','service','other') or (a=='destructive_fs_action' and r not in ('folder','file')) or (a=='maintenance_window' and r=='other'): raise ValueError('invalid scope enum')
        rid=_text(self.resource_id)
        if r in ('folder','file') and (rid.startswith('/') or any(x in rid for x in ('\\','%',':')) or any(p in ('','.','..') for p in rid.split('/'))): raise ValueError('ambiguous path')
        if type(self.scope_version) is not int or self.scope_version<1: raise ValueError('invalid scope_version')
        c=self.constraints
        if type(c) is not dict or not c or set(c)-{'backup_refs','executor','window_start','window_end','expiry'} or 'executor' not in c: raise ValueError('invalid constraints')
        norm={k: _text(v) if k!='backup_refs' else v for k,v in c.items()}
        if a=='destructive_fs_action' and 'backup_refs' not in norm: raise ValueError('backup_refs required')
        if 'backup_refs' in norm:
            refs=norm['backup_refs']
            if type(refs) is not list or not refs: raise ValueError('invalid backup_refs')
            norm['backup_refs']=[_text(x) for x in refs]
            if len(set(norm['backup_refs']))!=len(refs): raise ValueError('duplicate backup_ref')
        if a=='maintenance_window' and (not {'window_start','window_end'} <= norm.keys() or _time(norm['window_start'])>=_time(norm['window_end'])): raise ValueError('invalid window')
        object.__setattr__(self,'resource_id',rid)
        object.__setattr__(self,'constraints',norm)
    @property
    def digest(self): return sha256(canonical_scope(self)).hexdigest()

def canonical_scope(scope):
    if not isinstance(scope,Scope): raise ValueError('scope required')
    return json.dumps(dict(action_type=scope.action_type,resource_kind=scope.resource_kind,resource_id=scope.resource_id,constraints=scope.constraints,scope_version=scope.scope_version),sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8')

def _uuid(value, v4=False):
    if not isinstance(value,str): raise ValueError('UUID required')
    try: parsed=UUID(value)
    except (ValueError,AttributeError) as e: raise ValueError('invalid UUID') from e
    if str(parsed)!=value or (v4 and parsed.version!=4): raise ValueError('noncanonical UUID')
    return parsed

def _limit(v):
    if type(v) is not int or not 1<=v<=200: raise ValueError('limit must be integer 1..200')
    return v

def encode_cursor(ts, id):
    uid=_uuid(id)
    if ts is not None:
        if not isinstance(ts,datetime) or ts.utcoffset() is None: raise ValueError('aware timestamp required')
        ts=ts.astimezone(timezone.utc).isoformat(timespec='microseconds')
    raw=json.dumps({'v':1,'created_at':ts,'id':str(uid)},separators=(',',':')).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip('=')

def decode_cursor(cursor):
    if not isinstance(cursor,str) or not 1<=len(cursor)<=2048 or not re.fullmatch('[A-Za-z0-9_-]+={0,2}',cursor): raise ValueError('invalid cursor')
    try:
        raw=base64.b64decode(cursor+'='*((-len(cursor))%4),altchars=b'-_',validate=True)
        if base64.urlsafe_b64encode(raw).decode().rstrip('=')!=cursor.rstrip('='): raise ValueError('noncanonical cursor')
        def pairs(ps):
            d={}
            for k,v in ps:
                if k in d: raise ValueError('duplicate cursor key')
                d[k]=v
            return d
        data=json.loads(raw.decode('utf8'),object_pairs_hook=pairs,parse_constant=lambda _: (_ for _ in ()).throw(ValueError('NaN')))
        if type(data) is not dict or set(data)!={'v','created_at','id'} or type(data['v']) is not int or data['v']!=1: raise ValueError('cursor shape')
        uid=_uuid(data['id']); ts=data['created_at']
        if ts is not None:
            if not isinstance(ts, str): raise ValueError('invalid cursor timestamp')
            parsed = _time(ts).astimezone(timezone.utc)
            if parsed.isoformat(timespec='microseconds') != ts: raise ValueError('noncanonical cursor timestamp')
            return (parsed, uid)
        return (None, uid)
    except (ValueError,UnicodeError,TypeError) as e: raise ValueError('invalid cursor') from e

@dataclass(frozen=True)
class Reconciliation:
    classification: str  # equivalent_row | transition_compatible | not_applied | manual_review
    detail: str

_NO_OLD = object()
def reconcile_request(detail, principal, scope_hash, title, description, *, old=_NO_OLD):
    if detail is None: return Reconciliation('not_applied','no visible row; foreign rows remain hidden')
    if (detail['creator'],detail['creator_kind'],detail['scope_hash'],detail['title'],detail['description']) != (principal.name,principal.kind,scope_hash,title,description):
        return Reconciliation('manual_review','UUID has a different visible payload')
    if old is not _NO_OLD and (old is None or old['status']!='superseded_request'):
        return Reconciliation('manual_review','old request not superseded')
    return Reconciliation('equivalent_row','equivalent logical request; not invocation attribution; old→new link NOT verified' if old is not _NO_OLD else 'equivalent logical request; not invocation attribution')

def reconcile_transition(pre, post, operation):
    target={'approve':'aprobada','revoke':'revocada','request_changes':'changes_requested','finish':'done','withdraw':'withdrawn','record_effect':pre['status']}
    if operation not in target: raise ValueError('not a transition command')
    keys=('version','status','scope_hash','event_count','effect_count')
    if post is None: return Reconciliation('manual_review','detail not visible')
    if all(post[k]==pre[k] for k in keys): return Reconciliation('not_applied','PRE unchanged')
    if (post['version']==pre['version']+1 and post['status']==target[operation] and post['scope_hash']==pre['scope_hash'] and post['event_count']==pre['event_count']+1 and post['effect_count']==pre['effect_count']+(operation=='record_effect')):
        return Reconciliation('transition_compatible','transition applied; attribution to this invocation NOT proven')
    return Reconciliation('manual_review','concurrent or incompatible change')

_LIST_COLUMNS='id,title,status,creator,creator_kind,version,scope_hash,created_at,updated_at'
_DETAIL_COLUMNS='title,description,created_at'
_STATUSES=('pending_approval','changes_requested','superseded_request','withdrawn','aprobada','revocada','done')
class ApprovalCommands:
    def __init__(self, connect, *, test_after_commit=None):
        self._connect=connect
        self._test_after_commit=test_after_commit  # test-only: may raise psycopg.OperationalError after committed ack
    def _execute(self, sql, args, *, write=False, owner=None):
        conn=None; phase='before'
        try:
            conn=self._connect()
            if conn.autocommit: raise ValueError('autocommit connection forbidden')
            with conn.cursor() as cur:
                if owner is not None:
                    p, uid = owner
                    cur.execute("SELECT creator,creator_kind FROM public.tasks WHERE id=%s AND request_kind=%s FOR SHARE",(uid,"approval_request"))
                    seen=cur.fetchone()
                    if seen is None: raise ApprovalError("approval_not_found",404)
                    if seen != (p.name,p.kind): raise ApprovalError("approval_forbidden",403)
                cur.execute(sql,args)
                row=cur.fetchone() if cur.description else None
                if row is not None:
                    columns=[d.name for d in cur.description]
                    row=dict(row) if isinstance(row,dict) else dict(zip(columns,row))
            phase='commit'
            conn.commit()
            phase='after'
            if self._test_after_commit is not None: self._test_after_commit()
            return row
        except Exception as exc:
            if phase in ('commit','after') and isinstance(exc,psycopg.OperationalError): raise OutcomeUnknown() from exc
            if conn is not None and phase=='before':
                try: conn.rollback()
                except psycopg.OperationalError: pass
            if isinstance(exc,(ApprovalError,ValueError)): raise
            raise map_error(exc) from exc
        finally:
            if conn is not None:
                try: conn.close()
                except psycopg.OperationalError: pass
    @staticmethod
    def _actor(p):
        if not isinstance(p,ApprovalPrincipal): raise ValueError('trusted principal required')
        return p.name
    @staticmethod
    def _owner(p, row):
        if row is None or row['creator']!=p.name or row['creator_kind']!=p.kind: raise ApprovalError('approval_not_found',404)
    def request(self,p,request_id,title,description,scope):
        self._actor(p); uid=_uuid(request_id,True); title=_text(title)
        if description is not None and not isinstance(description,str): raise ValueError('invalid description')
        return self._execute('SELECT * FROM approval_api.approval_request(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',(uid,p.name,title,description,scope.action_type,scope.resource_kind,scope.resource_id,scope.scope_version,canonical_scope(scope),scope.digest),write=True)
    def approve(self,p,id,version,hash):
        self._actor(p)
        return self._execute('SELECT * FROM approval_api.approval_approve(%s,%s,%s)',(_uuid(id),version,hash),write=True)
    def revoke(self,p,id,version,hash,reason):
        self._actor(p)
        return self._execute('SELECT * FROM approval_api.approval_revoke(%s,%s,%s,%s)',(_uuid(id),version,hash,_text(reason)),write=True)
    def request_changes(self,p,id,version,hash,reason):
        self._actor(p)
        return self._execute('SELECT * FROM approval_api.approval_request_changes(%s,%s,%s,%s)',(_uuid(id),version,hash,_text(reason)),write=True)
    def finish(self,p,id,version,hash,evidence):
        self._actor(p)
        return self._execute('SELECT * FROM approval_api.approval_finish(%s,%s,%s,%s,%s)',(_uuid(id),version,hash,p.name,_text(evidence)),write=True,owner=(p,_uuid(id)))
    def withdraw(self,p,id,version,hash):
        self._actor(p)
        return self._execute('SELECT * FROM approval_api.approval_withdraw(%s,%s,%s,%s)',(_uuid(id),version,hash,p.name),write=True,owner=(p,_uuid(id)))
    def supersede(self,p,id,version,hash,new_request_id,title,description,scope):
        self._actor(p); uid=_uuid(new_request_id,True)
        title=_text(title)
        if description is not None and not isinstance(description,str): raise ValueError('invalid description')
        return self._execute('SELECT * FROM approval_api.approval_supersede(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',(_uuid(id),version,hash,uid,p.name,title,description,scope.action_type,scope.resource_kind,scope.resource_id,scope.scope_version,canonical_scope(scope),scope.digest),write=True,owner=(p,_uuid(id)))
    def record_effect(self,p,id,version,hash,occurred_at,evidence):
        self._actor(p)
        if not isinstance(occurred_at,datetime) or occurred_at.utcoffset() is None: raise ValueError('aware occurred_at required')
        return self._execute('SELECT * FROM approval_api.approval_record_effect(%s,%s,%s,%s,%s,%s)',(_uuid(id),version,hash,p.name,occurred_at,_text(evidence)),write=True,owner=(p,_uuid(id)))
    def get(self,p,id):
        self._actor(p)
        # approver login has no SELECT: caller must inject service pool; identity filter never trusts a cursor.
        row=self._execute('SELECT a.*, t.title,t.description,t.created_at FROM public.tasks t CROSS JOIN LATERAL approval_api.approval_get(t.id) a WHERE t.id=%s AND t.request_kind=%s AND ((%s=%s AND t.creator=%s AND t.creator_kind=%s) OR (%s=%s AND %s=%s))',(_uuid(id),'approval_request',p.kind,'agent',p.name,'agent',p.kind,'human',p.name,HUMAN_APPROVER))
        if row is None: raise ApprovalError('approval_not_found',404)
        return row
    def get_for_review(self,p,id):
        self._actor(p)
        # Same service-pool authorization as get; never use the approver login for reads.
        row=self._execute('SELECT a.*, t.title,t.description,t.created_at,t.action_type,t.resource_kind,t.resource_id,t.scope_version,t.scope_canonical FROM public.tasks t CROSS JOIN LATERAL approval_api.approval_get(t.id) a WHERE t.id=%s AND t.request_kind=%s AND ((%s=%s AND t.creator=%s AND t.creator_kind=%s) OR (%s=%s AND %s=%s))',(_uuid(id),'approval_request',p.kind,'agent',p.name,'agent',p.kind,'human',p.name,HUMAN_APPROVER))
        if row is None: raise ApprovalError('approval_not_found',404)
        try:
            row['scope_canonical_text']=bytes(row.pop('scope_canonical')).decode('utf-8',errors='strict')
        except (UnicodeError,TypeError,ValueError) as exc:
            raise ApprovalError('approval_integrity_error',500) from exc
        return row
    def _list(self,p,limit,cursor,status,inbox):
        self._actor(p); limit=_limit(limit)
        if status is not None and status not in _STATUSES: raise ValueError('invalid status')
        ts,uid=decode_cursor(cursor) if cursor is not None else (None,None)
        if p.kind=='human' and p.name!=HUMAN_APPROVER: raise ApprovalError('approval_forbidden',403)
        if p.kind=='human' and not inbox: return {'items':[],'next_cursor':None}
        identity='TRUE' if p.kind=='human' else 'creator=%s AND creator_kind=%s'
        args=[] if p.kind=='human' else [p.name,'agent']
        args.extend(['approval_request', status if p.kind=='human' else ('pending_approval' if inbox else status)])
        # NULLS LAST, including the NULL cursor branch; never interpolate user data.
        sql=f'SELECT {_LIST_COLUMNS} FROM public.tasks WHERE {identity} AND request_kind=%s AND (%s::text IS NULL OR status=%s::text)'
        args.append(args[-1])
        if uid is not None:
            if ts is None:
                sql+=' AND created_at IS NULL AND id>%s'; args.append(uid)
            else:
                sql+=' AND (created_at>%s OR (created_at=%s AND id>%s) OR created_at IS NULL)'; args.extend([ts,ts,uid])
        sql+=' ORDER BY created_at ASC NULLS LAST,id ASC LIMIT %s'; args.append(limit+1)
        # one transaction, cursor local to connection; no commit until rows consumed
        conn=None
        try:
            conn=self._connect()
            if conn.autocommit: raise ValueError('autocommit connection forbidden')
            with conn.cursor() as cur:
                cur.execute(sql,tuple(args)); columns=[d.name for d in cur.description]
                rows=[dict(r) if isinstance(r,dict) else dict(zip(columns,r)) for r in cur.fetchall()]
            conn.commit()
        except Exception as exc:
            if isinstance(exc,(ValueError,ApprovalError)): raise
            raise map_error(exc) from exc
        finally:
            if conn is not None: conn.close()
        items=rows[:limit]
        next_cursor=encode_cursor(items[-1]['created_at'],str(items[-1]['id'])) if len(rows)>limit else None
        return {'items':items,'next_cursor':next_cursor}
    def list_inbox(self,p,limit=50,cursor=None,status=None):
        # Only human BFF may select a non-pending inbox state; agent REST remains pending-only.
        if p.kind == 'human':
            if status is None: status='pending_approval'
            if status != 'all' and status not in _STATUSES: raise ValueError('invalid status')
            return self._list(p,limit,cursor,None if status == 'all' else status,True)
        if status is not None: raise ValueError('agent inbox status forbidden')
        return self._list(p,limit,cursor,None,True)
    def list_mine(self,p,limit=50,cursor=None,status=None): return self._list(p,limit,cursor,status,False)
