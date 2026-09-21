import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from release.asgctl import hook_install_records


class ReleaseDoctorTests(unittest.TestCase):
    def test_hook_records_use_install_workspace_and_verified_status(self):
        with tempfile.TemporaryDirectory() as directory:
            home=Path(directory);db_path=home/'state/endpoint/outbox.sqlite'
            db_path.parent.mkdir(parents=True)
            db=sqlite3.connect(str(db_path))
            db.execute('CREATE TABLE enrolled(instance TEXT PRIMARY KEY,configuration TEXT NOT NULL)')
            db.execute('CREATE TABLE soc_onboarding(instance TEXT PRIMARY KEY,result TEXT)')
            instance='42:123.5'
            db.execute('INSERT INTO enrolled VALUES(?,?)',(instance,json.dumps({
                'asg_instance_id':instance,'workspace':'/wrong/cwd',
                'hook_workspace':'/actual/profile'})))
            db.execute('INSERT INTO soc_onboarding VALUES(?,?)',(instance,json.dumps({
                'status':'activation_verified','transport':'soc-direct-v1'})))
            db.commit();db.close()
            records=hook_install_records(home)
            self.assertEqual(records[0]['workspace'],'/actual/profile')
            self.assertEqual(records[0]['status'],'activation_verified')


if __name__=='__main__':unittest.main()
