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

The terminal prints process logs for setup, each agent stage, local tool calls, and report saving. The final report is also written to the project root as:

```text
<repository-name>_ag2_repository_health_report.md
```

## Run Claude Agent SDK Version

```bash
python scripts/repo_health_anthropic_agentsdk.py --repo /path/to/repository
```

The Claude Agent SDK version expects an Anthropic-protocol compatible proxy URL. OpenRouter's Anthropic-compatible base URL is usually `https://openrouter.ai/api`; the course AWS proxy must support that protocol for this script to run directly.

The terminal prints process logs for setup, SDK tool calls, MCP tool calls, and report saving. The final report is also written to the project root as:

```text
<repository-name>_anthropic_agentsdk_repository_health_report.md
```

If `config/openrouter_proxy_url.txt` contains the course OpenAI `/v1` chat-completions URL, use the AG2 script with that file. For the Claude Agent SDK script, put an Anthropic-compatible URL in a separate ignored file and pass it explicitly:

```bash
python scripts/repo_health_anthropic_agentsdk.py \
  --repo /path/to/repository \
  --proxy-file config/anthropic_proxy_url.txt
```

## Design

The orchestration pattern is Supervisor/router:

- Supervisor Agent: routes work and integrates the final report.
- Repository Inspector Agent: inspects README, requirements, tests, CI, license, and local git commits.
- Code Quality Agent: checks maintainability, dependency complexity, missing tests/docs, and risky scripts.
- Risk Assessment Agent: chooses the final risk label.
- Recommendation Agent: suggests practical next steps.

Local tools include `list_repo_files`, `read_file`, `count_test_files`, `detect_ci_files`, `detect_license`, `recent_commits`, `summarize_dependency_files`, and `detect_risky_scripts`.

## Agent Workflow

Both implementations use the same conceptual workflow, but the frameworks wire the agents differently.

### Shared Tool Layer

The system never fetches GitHub data from the internet. It analyzes the local repository path passed with `--repo`.

The local tools provide the evidence:

- `list_repo_files(path)`: lists source files while skipping folders such as `.git`, `.venv`, `node_modules`, `dist`, and `build`.
- `read_file(path)`: reads a bounded text preview of a repository file so agents can inspect README and dependency files safely.
- `count_test_files(path)`: counts files that look like tests and reports examples.
- `detect_ci_files(path)`: checks for GitHub Actions, GitLab CI, Jenkins, CircleCI, and similar CI files.
- `detect_license(path)`: finds local license files and makes a conservative license-family guess.
- `recent_commits(path)`: reads recent local git commits if the analyzed folder has git history.
- `summarize_dependency_files(path)`: estimates dependency complexity from files such as `requirements.txt`, `pyproject.toml`, and `package.json`.
- `detect_risky_scripts(path)`: scans common source/config files for risky patterns such as shell pipes, `rm -rf`, `sudo`, unsafe `eval`, or `shell=True`.

### AG2 Workflow

In `scripts/repo_health_ag2.py`, Python acts as the explicit supervisor/router:

1. The script reads the proxy URL from `config/openrouter_proxy_url.txt`.
2. It creates five AG2 agents: Supervisor, Repository Inspector, Code Quality, Risk Assessment, and Recommendation.
3. It registers the repository tools with AG2. Specialist agents decide when to call tools, and the `UserProxyAgent` executes those Python functions locally.
4. The Supervisor routes the first task to the Repository Inspector Agent. This agent gathers factual evidence about README files, dependencies, tests, CI, license, and local commits.
5. The Supervisor routes the next task to the Code Quality Agent. This agent focuses on maintainability, project structure, dependency complexity, missing tests/docs, and risky scripts.
6. The Risk Assessment Agent receives the previous findings and chooses one label: `safe to use`, `use with caution`, `avoid`, or `missing information`.
7. The Recommendation Agent turns the risk assessment into concrete next steps.
8. The Supervisor integrates all specialist outputs into the final Markdown report.

### Claude Agent SDK Workflow

In `scripts/repo_health_anthropic_agentsdk.py`, Claude Agent SDK is the supervisor runtime:

1. The script reads an Anthropic-compatible proxy URL from a file.
2. It exposes the same repository functions as in-process MCP tools using the SDK `@tool` decorator.
3. It creates specialist subagents with `AgentDefinition`: `repository-inspector`, `code-quality`, `risk-assessment`, and `recommendation`.
4. The main Claude Agent SDK session acts as the Supervisor Agent.
5. The Supervisor uses the SDK `Agent` tool to call the Repository Inspector subagent first. That subagent calls MCP tools to gather local repository evidence.
6. The Supervisor calls the Code Quality subagent next. That subagent also uses MCP tools, especially dependency and risky-script checks.
7. The Supervisor sends the collected findings to the Risk Assessment subagent, which chooses the final risk label.
8. The Supervisor sends the risk assessment to the Recommendation subagent, which proposes practical next actions.
9. The Supervisor produces the final Markdown repository health report.

The important difference is protocol compatibility: AG2 talks directly to the OpenAI-compatible `/v1` proxy, while Claude Agent SDK runs Claude Code and expects an Anthropic Messages API proxy.
