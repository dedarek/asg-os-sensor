# ASG OS Sensor & Autonomous Agent Runtime Governance

OS-level non-invasive agent discovery, autonomous reverse-engineering & governance framework.

## Overview
Traditional AI Agent security relies either on proprietary SDK interception or manual proxy routing. When an unfamiliar Agent harness is deployed in enterprise environments, security teams are blind to its behavior.

Discovery combines configurable local identity hints (`identities.yaml`) with behavioral detection for unknown runtimes. Naming does not require Goose or an LLM credential. Entry names are evidence, not authenticated product identities; recognition scores are not risk scores or probabilities. Add missing entrypoint patterns to the catalog and restart the service.

To audit generalization, set `ASG_IDENTITY_HINTS=0`: discovery still evaluates explicit orchestration flags, entry-package model SDK declarations combined with child execution, and independent CLI child roles. Names can come from bounded `package.json` / application `Info.plist` metadata beside the entrypoint. Metadata naming does not by itself make a process an Agent. These are heuristics; generic CLI hosts and SDK consumers can still be false positives. `python3 -m unittest test_discovery -v` includes random package/CLI names, negative controls and nested different-Agent ownership.

Processes are assigned to their nearest Agent root. Same-family descendants merge; a different named Agent launched by a parent stays visible. Cards group roots by product and expose every associated PID, including helper processes. Identity recognition is separate from recipe generation and semantic attachment.

1. **OS Sensor (Polling Detection)**: Monitors process trees and launch parameters, using local identity evidence and behavioral signals. Polling cannot guarantee detection of short-lived processes or processes hidden by OS permissions.
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
 - `runtime/llm_config.py`: Analyst LLM route loader (llm.yaml + env/.env, OpenAI-compatible).
 - `llm.yaml`: Analyst LLM routes (provider/model/base_url/key_env, no secrets).
 - `.env.example`: Credential template (copy to `.env`, never commit `.env`).
 - `verify_llm.py`: LLM route smoke test (`chat/completions` + `responses`).

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
4. Configure Analyst LLM (optional, enables autonomous reverse-engineering):
   ```bash
   cp .env.example .env
   ```
   Fill `ASG_ANALYST_API_KEY` in `.env` (route `custom-openai` in `llm.yaml`), then:
   ```bash
   python verify_llm.py
   ```
   Add more routes in `llm.yaml`, switch via `ASG_ANALYST_ROUTE`, override model via `ASG_ANALYST_MODEL`.

   Runtime knobs such as `ASG_SCAN_INTERVAL`, `ASG_MAX_ANALYSTS`, `ASG_GOOSE_TIMEOUT`,
   `ASG_INGEST_URL`, and test sink settings are listed in `.env.example`.
   `ASG_INSECURE_SSL=1` is an emergency compatibility mode for an expired upstream
   certificate; smoke requests can use a loopback-only proxy. Runtime findings remain
   blocked unless `ASG_ALLOW_INSECURE_ANALYST=1` explicitly acknowledges that risk.


## Key Guarantees
- **Discovery with evidence**: Generic capability and process signals work without a product catalog; optional identity hints cover known opaque/idle entrypoints. Neither path guarantees complete detection.
- **Strict Safety Sandbox**: Analyst agents are strictly forbidden from executing arbitrary shell commands, modifying target configurations, or exfiltrating credentials.
- **Fail-Open & Passive**: Does not disrupt target execution or steal OS focus.
