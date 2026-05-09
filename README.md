# GitHub Repository Health Analyzer

### NOTE: 
This project is part of the course AI Engineering offered at the University of Oulu. The requirements for this project, as well as the resources provided for completing it are thanks to the University of Oulu.

Similarly, this project has been assisted by OpenAI's coding copilot "Codex", with the model GPT-5.5 to be precise.

------------

This project contains two implementations of the same multi-agent system:

- `scripts/repo_health_ag2.py`: AG2 supervisor/router implementation.
- `scripts/repo_health_anthropic_agentsdk.py`: Claude Agent SDK supervisor with subagents and local MCP tools.

Both scripts analyze a local repository and produce a recommendation: `safe to use`, `use with caution`, `avoid`, or `missing information`.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config/openrouter_proxy_url.example.txt config/openrouter_proxy_url.txt
```

Edit `config/openrouter_proxy_url.txt` and paste your AWS/OpenRouter proxy URL.

## Run AG2 Version

```bash
python scripts/repo_health_ag2.py --repo /path/to/repository
```

The AG2 version expects an OpenAI chat-completions compatible proxy base URL, such as a URL ending in `/v1`.

## Run Claude Agent SDK Version

```bash
python scripts/repo_health_anthropic_agentsdk.py --repo /path/to/repository
```

The Claude Agent SDK version expects an Anthropic-protocol compatible proxy URL. OpenRouter's Anthropic-compatible base URL is usually `https://openrouter.ai/api`; the course AWS proxy must support that protocol for this script to run directly.

## Design

The orchestration pattern is Supervisor/router:

- Supervisor Agent: routes work and integrates the final report.
- Repository Inspector Agent: inspects README, requirements, tests, CI, license, and local git commits.
- Code Quality Agent: checks maintainability, dependency complexity, missing tests/docs, and risky scripts.
- Risk Assessment Agent: chooses the final risk label.
- Recommendation Agent: suggests practical next steps.

Local tools include `list_repo_files`, `read_file`, `count_test_files`, `detect_ci_files`, `detect_license`, and `recent_commits`.
