# Tool delivery status

## send_message_to_thread
- 实际工具名（tools 清单/ALL_TOOLS 过滤确认）: mcp__codex_app__send_message_to_thread
- 调用入口: functions.exec 内 tools.mcp__codex_app__send_message_to_thread({threadId, prompt})
- 验证: 2026-09-10 成功投递（isError=false, 返回 {"threadId":"01a0843f-7250-7591-b63b-e46e5fa83725"}）
- 备注: 顶层直接调用同名工具返回 unsupported call; 经 exec 内 tools.* 命名空间调用有效。
  本文件不含密钥/凭据。
