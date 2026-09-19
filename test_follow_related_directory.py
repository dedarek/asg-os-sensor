import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from runtime import analyst_tools as at

class FollowDirectoryTests(unittest.TestCase):
    def test_observed_file_authorizes_only_its_immediate_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve(); folder = home/'some-runtime'/'config'; folder.mkdir(parents=True)
            source = folder/'settings.json'; source.write_text('{}')
            ref = 'ev-123456789-0123456789'
            (home/(ref+'.json')).write_text(json.dumps({'tool':'read_related_file',
                'target':{'pid':123,'create_time':45},'result':{'content':f'Configuration: {source}'}}))
            with patch.object(at,'EVIDENCE_DIR',home), patch.object(at,'TARGET_PID',123), \
                 patch.object(at,'TARGET_CREATE_TIME',45), patch.object(at,'target_process'), \
                 patch.object(Path,'home',return_value=home), patch.dict(at._RELATED_EVIDENCE_ROOTS,{},clear=True):
                args = {'path':str(folder),'source_file':str(source),'evidence_id':ref}
                self.assertEqual(at.call_tool('follow_related_directory',args)['status'],'collected')
                with self.assertRaises(ValueError):
                    at.call_tool('follow_related_directory',{**args,'path':str(folder.parent)})
                with self.assertRaises(ValueError):
                    at.call_tool('follow_related_directory',{**args,'source_file':str(folder/'guessed.json')})

    def test_follows_observed_directory_not_guesses_or_other_instances(self):
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp).resolve(); folder=home/'user-extensions'; folder.mkdir()
            (folder/'SKILL.md').write_text('---\nname: arbitrary-skill\ndescription: A test skill\n---')
            linked=home/'shared-skill'; linked.mkdir()
            (linked/'SKILL.md').write_text('name: linked-skill')
            (folder/'linked').symlink_to(linked, target_is_directory=True)
            (linked/'cycle').symlink_to(folder, target_is_directory=True)
            ref='ev-123456789-0123456789'
            evidence={'tool':'read_related_file','target':{'pid':123,'create_time':45},'result':{'content':f'Search skills in {folder}'}}
            (home/(ref+'.json')).write_text(json.dumps(evidence))
            with patch.object(at,'EVIDENCE_DIR',home), patch.object(at,'TARGET_PID',123), patch.object(at,'TARGET_CREATE_TIME',45), patch.object(at,'target_process'), patch.object(Path,'home',return_value=home), patch.dict(at._RELATED_EVIDENCE_ROOTS,{},clear=True), patch.object(at,'entry_surface',return_value={'related_roots':[]}):
                self.assertEqual(at.call_tool('follow_related_directory',{'path':str(folder),'evidence_id':ref})['status'],'collected')
                page=at.call_tool('find_related_files',{'scope':str(folder),'name_pattern':'SKILL.md'})
                self.assertEqual(len(page['files']),2)
                for item in page['files']: self.assertEqual(at.call_tool('read_related_file',{'path':item['path']})['status'],'collected')
                self.assertIn('arbitrary-skill',at.call_tool('read_related_file',{'path':page['files'][0]['path']})['content'])
                for bad in [str(home),str(folder)[: -1],str(home/'guessed')]:
                    with self.assertRaises(ValueError): at.call_tool('follow_related_directory',{'path':bad,'evidence_id':ref})
                evidence['target']['create_time']=46
                (home/(ref+'.json')).write_text(json.dumps(evidence))
                with self.assertRaises(ValueError): at.call_tool('follow_related_directory',{'path':str(folder),'evidence_id':ref})
