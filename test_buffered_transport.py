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

    def test_client_disconnect_does_not_emit_second_response(self):
        import io
        from unittest.mock import Mock, patch
        from runtime import llm_proxy
        handler = llm_proxy._ProxyHandler.__new__(llm_proxy._ProxyHandler)
        handler.path = '/v1/chat/completions'
        handler.headers = {'Content-Length':'2'}
        handler.rfile = io.BytesIO(b'{}')
        handler.wfile = Mock()
        handler.wfile.write.side_effect = BrokenPipeError()
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        upstream = Mock(status_code=200, headers={'Content-Type':'text/event-stream'})
        upstream.iter_content.return_value = [b'data: first\n\n']
        with patch.dict(llm_proxy._STATE, {'base_url':'https://example.com/v1','origin':'https://example.com','key':'test'}, clear=True), patch('runtime.model_capture.begin_capture', return_value=None), patch.object(llm_proxy.requests,'post',return_value=upstream):
            handler.do_POST()
        handler.send_response.assert_called_once_with(200)
        upstream.close.assert_called_once()
        self.assertTrue(handler.close_connection)
