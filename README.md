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
- `asg_os_sensor.py`: Lightweight OS-level daemon tracking process behaviors, network egress, and dangerous CLI patterns.
- `runtime/`:
  - `analyzer.py`: Process inspection and runtime feature extraction.
  - `analyst_tools.py`: Sandboxed, non-invasive investigation toolset exposed to the autonomous analyst.
  - `matcher.py`: Structural fingerprint matching engine.
  - `stream_parser.py`: Semantic envelope parser and credential sanitizer.
  - `fingerprints.json`: Persistent repository of runtime recipes and behavioral signatures.
- `recipes/`: Investigation directives and boundary policies for the Analyst agent.
- `e2e/`: Full end-to-end verification suites (`e2e_unknown.py`, test runners, and real binary verification).

## Key Guarantees
- **Zero-Prior Detection**: No hardcoded harness names, vendor domains, or pre-configured signatures.
- **Strict Safety Sandbox**: Analyst agents are strictly forbidden from executing arbitrary shell commands, modifying target configurations, or exfiltrating credentials.
- **Fail-Open & Passive**: Does not disrupt target execution or steal OS focus.
