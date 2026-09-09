# ASG OS Sensor & Autonomous Agent Runtime Governance

OS-level non-invasive agent discovery, autonomous reverse-engineering & governance framework.

## Overview
Traditional AI Agent security relies either on proprietary SDK interception or manual proxy routing. When an unfamiliar Agent harness is deployed in enterprise environments, security teams are blind to its behavior.

This project implements **Zero-Prior OS-Level Agent Governance**:
1. **OS Sensor (Millisecond Detection)**: Monitors process trees, launch parameters, IPC/network patterns, and structured stream protocols. Accurately scores and identifies unknown active Agent runtimes without knowing executable names or vendor identities.
2. **Autonomous Analyst (Agent Work)**: Triggers an isolated governance sub-agent (driven by mature harnesses like Goose + LLM) to perform non-interactive, read-only reverse-engineering on the target runtime. It determines process structures, CLI protocol declarations, and event envelopes to propose a formal governance Recipe.
3. **Automated Hook & Event Ingestion**: Supervisor validates and commits the candidate Recipe, establishing streaming sinks to capture, redact, and govern high-level semantic events (`llm.session`, `llm.request`, `llm.response`, `tool.call`, `tool.result`).
4. **Behavioral Memory & Instant Routing**: Successful recipes and structural features are fingerprinted. Subsequent encounters achieve millisecond-level routing, skipping autonomous exploration.

## Architecture
- `monitor_dashboard.py`: Web-based governance console and real-time dashboard (`http://127.0.0.1:8080`) featuring KPI metrics, dynamic fingerprint library drawer, and per-agent deep inspection.
- `asg_os_sensor.py`: Lightweight OS-level daemon tracking process behaviors, network egress, and dangerous CLI patterns.
- `start_dashboard.bat` / `start_dashboard.sh`: One-click portable launch scripts for Windows and Linux/macOS.
- `runtime/`:
  - `analyzer.py`: Process inspection and runtime feature extraction.
  - `analyst_tools.py`: Sandboxed, non-invasive investigation toolset exposed to the autonomous analyst.
  - `matcher.py`: Structural fingerprint matching engine.
  - `stream_parser.py`: Semantic envelope parser and credential sanitizer.
  - `fingerprints.json`: Persistent repository of runtime recipes and behavioral signatures.
- `recipes/`: Investigation directives and boundary policies for the Analyst agent.
- `e2e/`: Full end-to-end verification suites (`e2e_unknown.py`, test runners, and real binary verification).

## Quick Start
1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Start the governance dashboard:
   - On Windows: double-click `start_dashboard.bat` or run:
     ```cmd
     python monitor_dashboard.py
     ```
   - On Linux/macOS:
     ```bash
     bash start_dashboard.sh
     ```
3. Open your browser and navigate to:
   ```
   http://127.0.0.1:8080
   ```


## Key Guarantees
- **Zero-Prior Detection**: No hardcoded harness names, vendor domains, or pre-configured signatures.
- **Strict Safety Sandbox**: Analyst agents are strictly forbidden from executing arbitrary shell commands, modifying target configurations, or exfiltrating credentials.
- **Fail-Open & Passive**: Does not disrupt target execution or steal OS focus.
