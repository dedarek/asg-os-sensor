import unittest
from runtime.analyst_tools import redact
class FieldLabelTests(unittest.TestCase):
    def test_label_retained_but_secret_pair_value_removed(self):
        self.assertEqual(redact({'key':'name','value':'example'}),{'field':'name','value':'example'})
        self.assertEqual(redact({'key':'api_key','value':'credential'}),{'field':'api_key','value':'[REDACTED]'})
        self.assertEqual(redact({'key':'credential'}),{'key':'[REDACTED]'})
