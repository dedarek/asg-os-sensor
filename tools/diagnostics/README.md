# 手工诊断与探索

这些脚本不被看板启动流程导入，不是三套并行的生产发现器：

- `find_agents.py`：调用现有 Sensor 做一次只读进程盘点。
- `find_agents_behavior.py`：早期纯行为评分实验。
- `find_agents_v2.py`：早期分类及多轮行为采样实验。

在仓库根目录执行 `python tools/diagnostics/<文件名>`。后两个接受轮数、间隔秒数，例如 `python tools/diagnostics/find_agents_v2.py 2 5`。

正常产品发现入口是 `monitor_dashboard.py`，结合 `asg_os_sensor.py` 和 `runtime/identity.py`；不要用这些实验输出替代页面的实例验收结果。
