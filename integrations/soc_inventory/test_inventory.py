import json
import tempfile
import unittest
from pathlib import Path
from .collector import collect
from .store import Store


class InventoryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.skill = self.root / 'SKILL.md'
        self.skill.write_text('---\nname: example\ndescription: A test skill\n---\nHello')
        self.config = self.root / 'mcp.json'
        self.config.write_text(json.dumps({'mcpServers': {'sample': {'url': 'https://example.com/token-secret?key=secret', 'headers': {'Authorization':'secret'}, 'disabled':True}}}))
        self.source = self.root / 'findings.json'
        self.source.write_text(json.dumps({'target':{'pid':123,'create_time':1},'findings':{'identity':{'value':{'name':'Fixture'}},'assets':{
            'skills':{'value':{'items':[{'path':str(self.skill)}]}},
            'mcp':{'value':{'items':[{'source':str(self.config)}]}}
        }}}))
        self.store = Store(self.root / 'inventory.sqlite')

    def tearDown(self):
        self.temp.cleanup()

    def refresh(self, agent='a'):
        result = collect(self.source, agent)
        self.store.accept(result)
        return self.store.inventory(agent)

    def test_real_bytes_versions_and_idempotence(self):
        first = self.refresh()
        self.refresh()
        self.assertEqual(self.store.inventory('a')['categories']['skill']['count'],1)
        self.assertEqual(self.store.inventory('a')['categories']['skill']['items'][0]['version_count'],1)
        self.skill.write_text(self.skill.read_text()+'\nChanged')
        next_report=self.refresh()
        self.assertEqual(next_report['categories']['skill']['items'][0]['version_count'],2)
        self.assertNotEqual(first['categories']['skill']['items'][0]['fingerprint'],next_report['categories']['skill']['items'][0]['fingerprint'])

    def test_failure_preserves_other_categories_and_explicit_delete(self):
        self.refresh()
        self.config.write_text('{broken')
        self.skill.unlink()
        report=self.refresh()
        self.assertEqual(report['categories']['skill']['count'],0)
        self.assertEqual(report['categories']['mcp_server']['count'],1)
        self.assertTrue(report['categories']['mcp_server']['items'][0]['stale'])

    def test_agent_isolation(self):
        self.refresh('a'); self.refresh('b')
        self.skill.unlink(); self.refresh('a')
        self.assertEqual(self.store.inventory('b')['categories']['skill']['count'],1)

    def test_merge_preserves_resources_and_undo_restores_group(self):
        self.refresh('a');self.refresh('b')
        source=self.store.agents()[0]['type_id']
        before=self.store.inventory('a')
        self.store.merge_type(source,'codex');self.store.merge_type(source,'codex')
        self.assertEqual({a['type_id'] for a in self.store.agents()},{'codex'})
        self.assertEqual(before,self.store.inventory('a'))
        self.refresh('c')
        self.assertEqual({a['type_id'] for a in self.store.agents()},{'codex'})
        self.assertEqual(Store(self.root/'inventory.sqlite').resolve_type(source),'codex')
        with self.assertRaises(ValueError):self.store.merge_type('codex',source)
        self.store.undo_merge(source)
        self.assertEqual({a['type_id'] for a in self.store.agents()},{source})

    def test_merge_rejects_cycles_and_unknown_targets(self):
        self.refresh('a');first=self.store.agents()[0]['type_id']
        report=collect(self.source,'b');report['identity']['name']='Second';self.store.accept(report)
        second=next(a['type_id'] for a in self.store.agents() if a['id']=='b')
        with self.assertRaises(ValueError):self.store.merge_type(first,'missing')
        self.store.merge_type(first,second)
        with self.assertRaises(ValueError):self.store.merge_type(second,first)

    def test_type_and_instance_registration_is_idempotent(self):
        self.refresh('a'); self.refresh('a'); self.refresh('b')
        types=[p for p in self.store.platforms() if p['origin']=='discovered']
        self.assertEqual(len(types),1)
        self.assertEqual(types[0]['instance_count'],2)
        self.assertEqual(len(self.store.agents()),2)
        reopened=Store(self.root/'inventory.sqlite')
        self.assertEqual(reopened.platforms(),self.store.platforms())
        report=collect(self.source,'c');report['identity']['name']='Different Agent'
        self.store.accept(report)
        self.assertEqual(len([p for p in self.store.platforms() if p['origin']=='discovered']),2)

    def test_discovery_excludes_infrastructure_and_bad_files(self):
        from .preview import discover_sources
        directory=self.root/'new';directory.mkdir()
        path=directory/'investigation_findings.json'
        data={'target':{'pid':456,'create_time':123},'findings':{'identity':{'status':'identified','value':{'name':'Unseen','roles':['model_gateway']}}}}
        path.write_text(json.dumps(data));self.assertEqual(list(discover_sources(self.root,0)),[])
        data['findings']['identity']['value']['roles']=['agent'];path.write_text(json.dumps(data))
        first=list(discover_sources(self.root,0));self.assertEqual(len(first),1)
        self.assertEqual(first,list(discover_sources(self.root,0)))
        path.write_text('{');self.assertEqual(list(discover_sources(self.root,0)),[])

    def test_no_secrets_exported(self):
        report=self.refresh()
        item=report['categories']['mcp_server']['items'][0]
        self.assertFalse(item['enabled'])
        self.assertEqual(item['url'],'https://example.com')
        self.assertNotIn('secret',json.dumps(report))

    def test_unknown_not_zero_and_stale_rejected(self):
        first=collect(self.source,'a'); self.store.accept(first)
        stale=dict(first, collected_at=first['collected_at']-1)
        with self.assertRaises(ValueError): self.store.accept(stale)
        self.source.write_text(json.dumps({'findings':{}}))
        result=self.refresh('new')
        self.assertIsNone(result['categories']['skill']['count'])


if __name__=='__main__': unittest.main()
