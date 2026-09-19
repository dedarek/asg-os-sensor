"""Local receiver for the first UI preview. Immutable versions and scoped updates."""
import json
import sqlite3
import time
import hashlib
import unicodedata

BUILTINS = [('openclaw','OpenClaw'),('opencode','OpenCode'),('hermes','Hermes'),('codex','Codex')]

def type_identity(report):
    identity = report.get('identity') or {}
    name = str(identity.get('name') or report['name']).strip()
    key = unicodedata.normalize('NFKC', str(identity.get('package_name') or identity.get('bundle_id') or name)).casefold()
    key = ' '.join(key.split())
    # Only exact known identifiers reuse a built-in category; fuzzy names must
    # never imply compatible hooks.
    known = next((code for code,label in BUILTINS if key == code), None)
    return known or 'discovered-' + hashlib.sha256(key.encode()).hexdigest()[:20], name


class Store:
    def __init__(self, path):
        self.path = str(path)
        with self.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS reports(agent TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS bindings(agent TEXT, category TEXT, scope TEXT, key TEXT,
              body TEXT NOT NULL, active INTEGER NOT NULL, first_seen REAL, last_seen REAL,
              PRIMARY KEY(agent,category,key));
            CREATE TABLE IF NOT EXISTS versions(agent TEXT, category TEXT, key TEXT, fingerprint TEXT,
              body TEXT NOT NULL, seen REAL, PRIMARY KEY(agent,category,key,fingerprint));
            CREATE TABLE IF NOT EXISTS agent_types(id TEXT PRIMARY KEY, name TEXT NOT NULL, origin TEXT NOT NULL, first_seen REAL);
            CREATE TABLE IF NOT EXISTS instances(id TEXT PRIMARY KEY, type_id TEXT NOT NULL, name TEXT NOT NULL, source_target TEXT, first_seen REAL, last_seen REAL);
            CREATE TABLE IF NOT EXISTS type_aliases(source TEXT PRIMARY KEY,target TEXT NOT NULL,reason TEXT,created REAL);
            CREATE TABLE IF NOT EXISTS type_merge_audit(source TEXT,target TEXT,action TEXT,reason TEXT,created REAL);
            ''')
            for code, name in BUILTINS:
                db.execute('INSERT OR IGNORE INTO agent_types VALUES(?,?,?,?)',(code,name,'builtin',0))

    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def accept(self, report):
        agent = report['agent_id']
        timestamp = report['collected_at']
        with self.db() as db:
            old = db.execute('SELECT body FROM reports WHERE agent=?', (agent,)).fetchone()
            if old and json.loads(old['body'])['collected_at'] > timestamp:
                raise ValueError('stale_report')
            type_id, name = type_identity(report)
            db.execute('INSERT OR IGNORE INTO agent_types VALUES(?,?,?,?)',(type_id,name,'discovered',timestamp))
            db.execute('''INSERT INTO instances VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
              type_id=excluded.type_id,name=excluded.name,source_target=excluded.source_target,last_seen=excluded.last_seen''',
              (agent,type_id,report['name'],json.dumps(report.get('source_target',{})),timestamp,timestamp))
            for category, result in report['categories'].items():
                for scope in result['scopes']:
                    if scope['status'] != 'success':
                        continue
                    db.execute('UPDATE bindings SET active=0 WHERE agent=? AND category=? AND scope=?',
                               (agent, category, scope['scope_key']))
                    for item in scope['items']:
                        key = item['installation_key']
                        encoded = json.dumps(item, ensure_ascii=False)
                        db.execute('INSERT OR IGNORE INTO versions VALUES(?,?,?,?,?,?)',
                                   (agent, category, key, item['fingerprint'], encoded, timestamp))
                        db.execute('''INSERT INTO bindings VALUES(?,?,?,?,?,1,?,?)
                          ON CONFLICT(agent,category,key) DO UPDATE SET body=excluded.body,
                          active=1,last_seen=excluded.last_seen,scope=excluded.scope''',
                          (agent, category, scope['scope_key'], key, encoded, timestamp, timestamp))
            db.execute('INSERT OR REPLACE INTO reports VALUES(?,?)', (agent, json.dumps(report, ensure_ascii=False)))
        return {'accepted': True, 'agent_id': agent, 'received_at': time.time()}

    def agents(self):
        with self.db() as db:
            rows=[dict(row) for row in db.execute('SELECT id,name,type_id,first_seen,last_seen FROM instances ORDER BY first_seen,id')]
        for row in rows:
            row['original_type_id']=row['type_id']
            row['type_id']=self.resolve_type(row['type_id'])
        return rows

    def resolve_type(self, type_id):
        with self.db() as db:
            seen=set()
            while type_id not in seen:
                seen.add(type_id)
                row=db.execute('SELECT target FROM type_aliases WHERE source=?',(type_id,)).fetchone()
                if not row:return type_id
                type_id=row['target']
        raise ValueError('alias_cycle')

    def merge_type(self, source, target, reason='user'):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT origin FROM agent_types WHERE id=?',(source,)).fetchone()
            if not row or row['origin']!='discovered':raise ValueError('only_discovered_types_can_merge')
            if not db.execute('SELECT 1 FROM agent_types WHERE id=?',(target,)).fetchone():raise ValueError('unknown_target')
            seen={source}; current=target
            while True:
                if current in seen:raise ValueError('alias_cycle')
                seen.add(current)
                alias=db.execute('SELECT target FROM type_aliases WHERE source=?',(current,)).fetchone()
                if not alias:break
                current=alias['target']
            old=db.execute('SELECT target FROM type_aliases WHERE source=?',(source,)).fetchone()
            if old:
                if old['target']==current:return
                raise ValueError('undo_existing_merge_first')
            db.execute('INSERT INTO type_aliases VALUES(?,?,?,?)',(source,current,reason,time.time()))
            db.execute('INSERT INTO type_merge_audit VALUES(?,?,?,?,?)',(source,current,'merge',reason,time.time()))

    def undo_merge(self, source):
        with self.db() as db:
            old=db.execute('SELECT target FROM type_aliases WHERE source=?',(source,)).fetchone()
            if old:
                db.execute('DELETE FROM type_aliases WHERE source=?',(source,))
                db.execute('INSERT INTO type_merge_audit VALUES(?,?,?,?,?)',(source,old['target'],'undo','user',time.time()))

    def platforms(self):
        with self.db() as db:
            types=[dict(row) for row in db.execute('SELECT * FROM agent_types ORDER BY first_seen,id')]
        agents=self.agents()
        for row in types:
            row['canonical_id']=self.resolve_type(row['id'])
            row['instance_count']=sum(a['type_id']==row['id'] for a in agents)
            row['aliases']=[{'id':x['id'],'name':x['name']} for x in types if x['id']!=row['id'] and self.resolve_type(x['id'])==row['id']]
        return types

    def sources(self):
        with self.db() as db:
            return {row['agent']:json.loads(row['body'])['source_file'] for row in db.execute('SELECT * FROM reports')}

    def inventory(self, agent):
        with self.db() as db:
            row = db.execute('SELECT body FROM reports WHERE agent=?', (agent,)).fetchone()
            if not row:
                raise KeyError(agent)
            report = json.loads(row['body'])
            categories = {}
            for category, result in report['categories'].items():
                items = []
                failed = {s['scope_key'] for s in result['scopes'] if s['status'] == 'failed'}
                for binding in db.execute('SELECT * FROM bindings WHERE agent=? AND category=? AND active=1 ORDER BY key', (agent,category)):
                    item = json.loads(binding['body'])
                    history = [dict(v) for v in db.execute('SELECT fingerprint,seen FROM versions WHERE agent=? AND category=? AND key=? ORDER BY seen DESC', (agent,category,binding['key']))]
                    items.append({**item, 'versions': history, 'version_count': len(history),
                                  'last_seen': binding['last_seen'], 'stale': binding['scope'] in failed})
                categories[category] = {'status': result['status'], 'count': len(items) if result['scopes'] else None,
                                        'items': items, 'errors': [s for s in result['scopes'] if s['status']=='failed']}
            return {**{k:v for k,v in report.items() if k != 'categories'}, 'categories':categories}
