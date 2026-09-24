"""Durable, instance-filtered JSONL transfer from validated ASG bindings."""
import json
import os
from pathlib import Path
from runtime import hook_data
from .protocol import canonical, sha

NL=bytes([10])
WS=chr(32)+chr(13)+chr(10)+chr(9)

class EventSpool:
    def __init__(self,endpoint):
        self.endpoint=endpoint
        endpoint.db.executescript('''CREATE TABLE IF NOT EXISTS event_offsets(source TEXT PRIMARY KEY,offset INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS event_outbox(id TEXT PRIMARY KEY,agent TEXT,body BLOB NOT NULL);''')
    def collect(self,agent,hooks):
        instance=agent['asg_instance_id'];root=hooks.get('run_dir')
        if not root:return
        bindings,_,_=hook_data._registry_bindings(Path(root))
        for binding in bindings:
            if binding['instance_id']!=instance:continue
            path=Path(binding['config']['log_path']);target=binding['target'];fields=binding['config'].get('fields') or {}
            try:
                with path.open('rb') as stream:
                    st=os.fstat(stream.fileno());source=sha(canonical([instance,str(path),st.st_dev,st.st_ino]))
                    prior=self.endpoint.db.execute('SELECT offset FROM event_offsets WHERE source=?',(source,)).fetchone();offset=prior[0] if prior else 0
                    # Truncation is reported explicitly instead of assuming continuity.
                    if st.st_size<offset:
                        self.save(agent,source,offset,{'event_type':'collection.gap','reason':'source_truncated'},0);offset=0
                    stream.seek(offset);consumed=0
                    while consumed<4*1024*1024:
                        start=stream.tell();line=stream.readline(8*1024*1024+1)
                        if not line:break
                        if not line.endswith(NL):
                            if len(line)<=8*1024*1024:
                                break  # partial write: wait for the next cycle
                            # Oversized record (crazytest B16: one 966 KB Codex
                            # record exceeded the old 512 KB cap): the previous
                            # code advanced the durable offset by only the capped
                            # read, so every later batch decoded from mid-record
                            # offsets and reported a flood of malformed gaps.
                            # Skip the whole physical line and resynchronize at
                            # the next newline; an unterminated tail retries from
                            # the saved offset on the next cycle.
                            rest=b'';skipped=False
                            while len(rest)<16*1024*1024:
                                more=stream.readline(8*1024*1024+1)
                                if not more:break
                                rest+=more
                                if rest.endswith(NL):skipped=True;break
                            if not skipped:break  # tail still growing: retry next cycle from saved offset
                            self.save(agent,source,start,{'event_type':'collection.gap','reason':'oversized_or_incomplete_record'},stream.tell())
                            consumed+=len(line)+len(rest)
                            continue
                        consumed+=len(line)
                        # One physical line may hold several JSON objects when an
                        # earlier append was torn apart by a writer crash; the old
                        # single json.loads per line marked the whole glued line
                        # malformed and dropped every valid record inside it.
                        try:text=line.decode('utf-8')
                        except UnicodeDecodeError:
                            self.save(agent,source,start,{'event_type':'collection.gap','reason':'malformed_jsonl'},stream.tell());continue
                        dec=json.JSONDecoder(strict=False);pos=0;salvaged=False
                        while pos<len(text):
                            while pos<len(text) and text[pos] in WS:pos+=1
                            if pos>=len(text):break
                            try:obj,end=dec.raw_decode(text,pos)
                            except ValueError:
                                self.save(agent,source,start,{'event_type':'collection.gap','reason':'malformed_jsonl'},stream.tell());break
                            salvaged=True
                            self.save(agent,source,start,self.project(target,fields,obj),stream.tell())
                            pos=end
                        if not salvaged and pos==0:
                            self.save(agent,source,start,{'event_type':'collection.gap','reason':'malformed_jsonl'},stream.tell())
            except OSError:continue
    def project(self,target,fields,event):
        """Map one decoded hook record onto an instance-bound event, or a gap."""
        if not isinstance(event,dict):return {'event_type':'collection.gap','reason':'missing_or_invalid_event_type'}
        pid=hook_data._event_pid(event.get(fields.get('pid','pid')))
        ct=hook_data._optional_create_time(event);ts=hook_data._parse_time(event.get(fields.get('timestamp','timestamp')))
        if pid==target['pid'] and ts is not None and ts>=target['create_time']-1 and (ct is None or isinstance(ct,(int,float)) and abs(ct-target['create_time'])<=0.001):
            name=event.get(fields.get('event','event'))
            return {'event_type':name,'timestamp':ts,'payload':hook_data.redact(event)} if isinstance(name,str) and 0<len(name)<=200 else {'event_type':'collection.gap','reason':'missing_or_invalid_event_type'}
        return None
    def save(self,agent,source,start,record,end):
        with self.endpoint.db:
            if record is not None:
                event={'event_id':sha(canonical([source,start,record])),'instance_id':agent['asg_instance_id'],'channel':'bridge',**record}
                self.endpoint.db.execute('INSERT OR IGNORE INTO event_outbox VALUES(?,?,?)',(event['event_id'],agent['agent_id'],canonical(event)))
            self.endpoint.db.execute('INSERT OR REPLACE INTO event_offsets VALUES(?,?)',(source,end))
    def flush(self,agent):
        # Batches are grouped by the instance that produced each event, not the
        # currently live instance: a restart must never strand older events of
        # the same agent behind an envelope mismatch.
        rows=self.endpoint.db.execute('SELECT id,body,json_extract(CAST(body AS TEXT),\'$.instance_id\') FROM event_outbox WHERE agent=? ORDER BY rowid LIMIT 200',(agent['agent_id'],)).fetchall()
        if not rows:return
        groups={}
        for row in rows:
            instance=row[2] if len(row)>2 and row[2] else agent['asg_instance_id']
            groups.setdefault(instance,[]).append(row)
        for instance,group in groups.items():
            # Bound each request including large model/tool records.
            # The gateway accepts at most 50 events per batch; exceeding that
            # rejects the whole request and strands the outbox forever.
            selected=[];size=0
            for row in group:
                if selected and (len(selected)>=50 or size+len(row[1])>1024*1024):break
                selected.append(row);size+=len(row[1])
            if not selected:continue
            try:
                receipt=self.endpoint.request(agent,'/api/asg/events',canonical({'instance_id':instance,'events':[json.loads(r[1]) for r in selected]}))
                if receipt.get('accepted') is not True:continue
            except OSError:continue
            # A failing/old instance group must not starve newer instance
            # groups of the same agent (crazytest B4: return->continue).
            with self.endpoint.db:self.endpoint.db.executemany('DELETE FROM event_outbox WHERE id=?',[(r[0],) for r in selected])
