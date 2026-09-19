# 早期 OS 传感器模拟演示

保留 good_agent.py、evil_agent.py 和 verify.py 供回溯，不参与部署或自动验收。

从仓库根目录用 `python e2e/legacy/<文件名>` 运行。模拟器使用根目录 honey/ 中的假文件和 tmp/ 临时输出；evil_agent 会建立 example.com:80 连接。这些脚本只演示 OS 规则，不能证明真实 Agent Hook 已接通。
