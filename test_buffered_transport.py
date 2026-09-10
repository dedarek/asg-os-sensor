import json
import unittest
from runtime.llm_proxy import completion_as_sse

class BufferedTransportTests(unittest.TestCase):
    def test_preserves_complete_generated_tool_arguments(self):
        arguments = json.dumps({'recipe': {'install_plan': {'files': [{'content': 'export default async () => ({})\n'}]}}})
        response = {'id': 'fixture', 'model': 'arbitrary', 'choices': [{'index': 0,
            'message': {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'c1', 'type': 'function',
                        'function': {'name': 'propose_recipe', 'arguments': arguments}}]}, 'finish_reason': 'tool_calls'}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 20, 'total_tokens': 30}}
        before = json.dumps(response)
        chunks = [json.loads(line[6:]) for line in completion_as_sse(response).decode().splitlines()
                  if line.startswith('data: {')]
        call = chunks[0]['choices'][0]['delta']['tool_calls'][0]
        self.assertEqual(call['function']['arguments'], arguments)
        self.assertEqual(call['index'], 0)
        self.assertEqual(chunks[1]['choices'][0]['finish_reason'], 'tool_calls')
        self.assertEqual(chunks[2]['usage'], response['usage'])
        self.assertEqual(json.dumps(response), before)
        self.assertTrue(completion_as_sse(response).endswith(b'data: [DONE]\n\n'))
