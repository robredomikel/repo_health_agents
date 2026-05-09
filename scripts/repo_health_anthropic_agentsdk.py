#!/usr/bin/env python3
"""GitHub Repository Health Analyzer implemented with Claude Agent SDK.

This version uses Anthropic's Claude Agent SDK. The Supervisor is the main
Claude Agent SDK session. It routes work to programmatically defined subagents:

Supervisor Agent
    - repository-inspector subagent
    - code-quality subagent
    - risk-assessment subagent
    - recommendation subagent

Local repository inspection functions are exposed as in-process MCP tools via
the SDK's @tool decorator and create_sdk_mcp_server().
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Any


try:
    from claude_agent_sdk import (
        AgentDefinition,
        ClaudeAgentOptions,
        create_sdk_mcp_server,
        query,
        tool,
    )
except ImportError as exc:  # pragma: no cover - only runs when dependency is missing.
    raise SystemExit(
        "Missing dependency: install Claude Agent SDK with "
        "`pip install -r requirements.txt`."
    ) from exc


# The MCP tools need to know which repository is currently being analyzed.
# The value is set in main() after command-line parsing and validation.
ACTIVE_REPO_ROOT: Path | None = None


IGNORED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "dist",
    "build",
    "coverage",
    ".coverage",
}


CI_FILE_CANDIDATES = [
    ".github/workflows",
    ".gitlab-ci.yml",
    "azure-pipelines.yml",
    ".circleci/config.yml",
    "Jenkinsfile",
    ".travis.yml",
    "bitbucket-pipelines.yml",
]


DEPENDENCY_FILE_CANDIDATES = [
    "requirements.txt",
    "requirements-dev.txt",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "Pipfile",
    "poetry.lock",
    "environment.yml",
    "package.json",
    "package-lock.json",
    "Dockerfile",
    "docker-compose.yml",
    "compose.yml",
]


def load_first_value(path: Path, *, required: bool = True) -> str | None:
    """Read the first non-comment line from a small config/secret text file."""

    if not path.exists():
        if required:
            raise FileNotFoundError(
                f"Required file not found: {path}. Copy the example proxy file "
                "to config/openrouter_proxy_url.txt and paste the proxy URL."
            )
        return None

    for line in path.read_text(encoding="utf-8").splitlines():
        cleaned = line.strip()
        if cleaned and not cleaned.startswith("#"):
            return cleaned.rstrip("/")

    if required:
        raise ValueError(f"File {path} did not contain a usable value.")
    return None


def resolve_repo_root(repo: Path) -> Path:
    """Resolve and validate the local repository path supplied by the user."""

    repo_root = repo.expanduser().resolve()
    if not repo_root.exists():
        raise FileNotFoundError(f"Repository path does not exist: {repo_root}")
    if not repo_root.is_dir():
        raise NotADirectoryError(f"Repository path is not a directory: {repo_root}")
    return repo_root


def repo_root() -> Path:
    """Return the active repository root or fail with a clear message."""

    if ACTIVE_REPO_ROOT is None:
        raise RuntimeError("ACTIVE_REPO_ROOT was not set before tool execution.")
    return ACTIVE_REPO_ROOT


def resolve_inside_repo(path: str | Path) -> Path:
    """Resolve a tool path while preventing reads outside the target repository."""

    root = repo_root()
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate

    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Refusing to inspect {resolved}; it is outside {root}.") from exc
    return resolved


def relative_path(path: Path) -> str:
    """Return a path relative to the analyzed repository when possible."""

    try:
        return str(path.relative_to(repo_root()))
    except ValueError:
        return str(path)


def walk_repo_files(start: Path, limit: int = 500) -> list[str]:
    """Collect a bounded list of files and skip noisy generated folders."""

    files: list[str] = []
    for current_root, dirnames, filenames in os.walk(start):
        dirnames[:] = [
            name
            for name in dirnames
            if name not in IGNORED_DIRS and not name.startswith(".DS_Store")
        ]
        for filename in sorted(filenames):
            files.append(relative_path(Path(current_root) / filename))
            if len(files) >= limit:
                return files
    return files


def read_text_preview(path: Path, max_bytes: int = 12_000) -> dict[str, Any]:
    """Read a safe text preview from a local repository file."""

    raw = path.read_bytes()
    truncated = len(raw) > max_bytes
    preview = raw[:max_bytes]
    if b"\x00" in preview:
        return {
            "path": relative_path(path),
            "is_binary": True,
            "truncated": truncated,
            "content": "",
        }
    return {
        "path": relative_path(path),
        "is_binary": False,
        "truncated": truncated,
        "content": preview.decode("utf-8", errors="replace"),
    }


def detect_license_name(text: str) -> str:
    """Guess the license family from common license text phrases."""

    lowered = text.lower()
    if "mit license" in lowered:
        return "MIT"
    if "apache license" in lowered:
        return "Apache"
    if "bsd" in lowered and "redistribution and use" in lowered:
        return "BSD"
    if "gnu general public license" in lowered:
        return "GPL"
    if "gnu lesser general public license" in lowered:
        return "LGPL"
    if "mozilla public license" in lowered:
        return "MPL"
    if "isc license" in lowered:
        return "ISC"
    return "unknown"


def find_existing_files(candidates: list[str]) -> list[Path]:
    """Find candidate files or files inside candidate directories."""

    root = repo_root()
    matches: list[Path] = []
    for candidate in candidates:
        path = root / candidate
        if path.is_file():
            matches.append(path)
        elif path.is_dir():
            matches.extend(sorted(item for item in path.rglob("*") if item.is_file()))
    return matches


def mcp_text_response(payload: dict[str, Any]) -> dict[str, Any]:
    """Format a Python payload as an MCP text response for Claude Agent SDK."""

    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload, indent=2, sort_keys=True),
            }
        ]
    }


@tool("list_repo_files", "List repository files while ignoring generated folders.", {"path": str})
async def list_repo_files_tool(args: dict[str, Any]) -> dict[str, Any]:
    """MCP tool: list repository files."""

    start = resolve_inside_repo(args.get("path", "."))
    if not start.is_dir():
        raise NotADirectoryError(f"Not a directory: {start}")
    files = walk_repo_files(start)
    return mcp_text_response(
        {
            "repo_root": str(repo_root()),
            "start": relative_path(start),
            "file_count_returned": len(files),
            "files": files,
        }
    )


@tool("read_file", "Read a safe text preview of a repository file.", {"path": str})
async def read_file_tool(args: dict[str, Any]) -> dict[str, Any]:
    """MCP tool: read a bounded preview from one file."""

    file_path = resolve_inside_repo(args["path"])
    if not file_path.is_file():
        raise FileNotFoundError(f"Not a file: {file_path}")
    return mcp_text_response(read_text_preview(file_path))


@tool(
    "count_test_files",
    "Count local test files and identify whether common test folders exist.",
    {"path": str},
)
async def count_test_files_tool(args: dict[str, Any]) -> dict[str, Any]:
    """MCP tool: count tests and return representative examples."""

    start = resolve_inside_repo(args.get("path", "."))
    test_files: list[str] = []
    for file_name in walk_repo_files(start, limit=2_000):
        name = Path(file_name).name.lower()
        parts = {part.lower() for part in Path(file_name).parts}
        if (
            "tests" in parts
            or "test" in parts
            or name.startswith("test_")
            or name.endswith("_test.py")
            or name.endswith(".spec.js")
            or name.endswith(".test.js")
            or name.endswith(".spec.ts")
            or name.endswith(".test.ts")
        ):
            test_files.append(file_name)

    return mcp_text_response(
        {
            "count": len(test_files),
            "examples": test_files[:30],
            "has_tests_directory": any(
                (repo_root() / dirname).is_dir() for dirname in ("tests", "test")
            ),
        }
    )


@tool(
    "detect_ci_files",
    "Detect common CI workflow files such as GitHub Actions or GitLab CI.",
    {"path": str},
)
async def detect_ci_files_tool(args: dict[str, Any]) -> dict[str, Any]:
    """MCP tool: detect local CI configuration."""

    _ = resolve_inside_repo(args.get("path", "."))
    matches = find_existing_files(CI_FILE_CANDIDATES)
    return mcp_text_response(
        {
            "count": len(matches),
            "ci_files": [relative_path(match) for match in matches],
        }
    )


@tool(
    "detect_license",
    "Detect a local license file and guess its license family.",
    {"path": str},
)
async def detect_license_tool(args: dict[str, Any]) -> dict[str, Any]:
    """MCP tool: detect license information."""

    _ = resolve_inside_repo(args.get("path", "."))
    license_files: list[dict[str, Any]] = []
    for candidate in repo_root().iterdir():
        if candidate.is_file() and candidate.name.lower() in {
            "license",
            "license.md",
            "license.txt",
            "copying",
            "copying.md",
            "copying.txt",
        }:
            preview = read_text_preview(candidate, max_bytes=4_000)
            license_files.append(
                {
                    "path": relative_path(candidate),
                    "detected_family": detect_license_name(preview["content"]),
                    "preview": preview["content"][:500],
                }
            )

    return mcp_text_response(
        {
            "has_license": bool(license_files),
            "licenses": license_files,
        }
    )


@tool(
    "recent_commits",
    "Read recent local git commit summaries if git history is available.",
    {"path": str, "limit": int},
)
async def recent_commits_tool(args: dict[str, Any]) -> dict[str, Any]:
    """MCP tool: inspect local git history without network access."""

    _ = resolve_inside_repo(args.get("path", "."))
    safe_limit = max(1, min(int(args.get("limit", 5)), 20))
    command = [
        "git",
        "-C",
        str(repo_root()),
        "log",
        f"-n{safe_limit}",
        "--pretty=format:%h%x09%ad%x09%s",
        "--date=short",
    ]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if completed.returncode != 0:
        return mcp_text_response(
            {
                "available": False,
                "reason": completed.stderr.strip() or "No git history available.",
                "commits": [],
            }
        )
    return mcp_text_response({"available": True, "commits": completed.stdout.splitlines()})


@tool(
    "summarize_dependency_files",
    "Find dependency files and estimate dependency complexity.",
    {"path": str},
)
async def summarize_dependency_files_tool(args: dict[str, Any]) -> dict[str, Any]:
    """MCP tool: summarize dependency declaration files."""

    _ = resolve_inside_repo(args.get("path", "."))
    matches = find_existing_files(DEPENDENCY_FILE_CANDIDATES)
    summaries: list[dict[str, Any]] = []
    for match in matches:
        preview = read_text_preview(match, max_bytes=6_000)
        content = preview["content"]
        dependency_lines = [
            line.strip()
            for line in content.splitlines()
            if line.strip()
            and not line.strip().startswith("#")
            and not line.strip().startswith("[")
        ]
        summaries.append(
            {
                "path": relative_path(match),
                "line_count_previewed": len(content.splitlines()),
                "rough_dependency_line_count": len(dependency_lines),
                "truncated": preview["truncated"],
            }
        )
    return mcp_text_response({"dependency_files": summaries, "count": len(summaries)})


@tool(
    "detect_risky_scripts",
    "Scan source and config files for risky script patterns.",
    {"path": str},
)
async def detect_risky_scripts_tool(args: dict[str, Any]) -> dict[str, Any]:
    """MCP tool: look for simple risk patterns in local scripts."""

    start = resolve_inside_repo(args.get("path", "."))
    risky_patterns = [
        "curl ",
        "wget ",
        "| sh",
        "| bash",
        "rm -rf",
        "sudo ",
        "chmod 777",
        "eval(",
        "exec(",
        "shell=True",
        "pickle.load",
        "yaml.load(",
    ]
    findings: list[dict[str, str]] = []

    for file_name in walk_repo_files(start, limit=2_000):
        file_path = repo_root() / file_name
        if file_path.suffix.lower() not in {
            ".py",
            ".sh",
            ".bash",
            ".zsh",
            ".js",
            ".ts",
            ".yml",
            ".yaml",
            ".toml",
            ".json",
        } and file_path.name not in {"Dockerfile", "Makefile"}:
            continue

        preview = read_text_preview(file_path, max_bytes=20_000)
        if preview["is_binary"]:
            continue
        for line_number, line in enumerate(preview["content"].splitlines(), start=1):
            lowered = line.lower()
            if any(pattern.lower() in lowered for pattern in risky_patterns):
                findings.append(
                    {
                        "path": file_name,
                        "line": str(line_number),
                        "snippet": line.strip()[:200],
                    }
                )
                if len(findings) >= 40:
                    return mcp_text_response({"findings": findings, "truncated": True})

    return mcp_text_response({"findings": findings, "truncated": False})


def build_subagents(mcp_tool_names: list[str]) -> dict[str, AgentDefinition]:
    """Define the specialist subagents used by the Supervisor."""

    return {
        "repository-inspector": AgentDefinition(
            description=(
                "Use this agent to inspect repository metadata, README files, "
                "tests, CI, license, dependencies, and recent local commits."
            ),
            prompt=(
                "You are the Repository Inspector Agent. Use the provided MCP "
                "tools to inspect the target repository. Call list_repo_files, "
                "detect_ci_files, detect_license, count_test_files, recent_commits, "
                "and read_file for README/dependency files when present. Return "
                "factual findings with explicit evidence."
            ),
            tools=mcp_tool_names,
            model="inherit",
            mcpServers=["repo_health"],
        ),
        "code-quality": AgentDefinition(
            description=(
                "Use this agent to analyze maintainability signals, project "
                "structure, dependency complexity, missing tests, missing docs, "
                "and risky scripts."
            ),
            prompt=(
                "You are the Code Quality Agent. Use the provided MCP tools to "
                "evaluate maintainability. Call list_repo_files, "
                "summarize_dependency_files, count_test_files, and "
                "detect_risky_scripts. You may call read_file for relevant files. "
                "Prioritize concrete, local evidence."
            ),
            tools=mcp_tool_names,
            model="inherit",
            mcpServers=["repo_health"],
        ),
        "risk-assessment": AgentDefinition(
            description=(
                "Use this agent after inspection and quality analysis to classify "
                "repository adoption risk."
            ),
            prompt=(
                "You are the Risk Assessment Agent. Given specialist findings, "
                "choose exactly one label: safe to use, use with caution, avoid, "
                "or missing information. Explain the evidence, risk drivers, and "
                "confidence level."
            ),
            tools=[],
            model="inherit",
        ),
        "recommendation": AgentDefinition(
            description=(
                "Use this agent to suggest concrete next steps for a developer "
                "deciding whether to use, contribute to, or depend on a repository."
            ),
            prompt=(
                "You are the Recommendation Agent. Suggest concrete next steps "
                "such as running tests, checking open issues, pinning versions, "
                "reviewing the license, and inspecting security policy. Keep the "
                "advice practical."
            ),
            tools=[],
            model="inherit",
        ),
    }


def build_prompt(repo: Path, question: str) -> str:
    """Create the Supervisor prompt that explicitly routes to specialists."""

    return f"""
You are the Supervisor Agent for a GitHub Repository Health Analyzer.

Target repository:
{repo}

Decision question:
{question}

Use the Agent tool to call these specialist subagents in order:
1. repository-inspector
2. code-quality
3. risk-assessment
4. recommendation

When asking specialists to inspect files, tell them to use "." as the repository
root. After all specialists respond, integrate their findings into a final
Markdown report with:
- Recommendation label
- Short rationale
- Evidence table
- Missing information
- Concrete next steps
""".strip()


def build_options(args: argparse.Namespace, repo: Path, proxy_url: str) -> ClaudeAgentOptions:
    """Configure Claude Agent SDK with subagents and MCP tools."""

    auth_token = None
    if args.auth_token_file:
        auth_token = load_first_value(Path(args.auth_token_file), required=False)

    # Claude Agent SDK uses Anthropic-protocol routing. OpenRouter's Anthropic
    # skin uses https://openrouter.ai/api. If your course proxy only implements
    # OpenAI chat completions, use the AG2 script or provide a protocol-translation
    # proxy for this script.
    env = {
        "ANTHROPIC_BASE_URL": proxy_url,
        "ANTHROPIC_MODEL": args.model,
        # A dummy key keeps Claude Code in API-key mode when the course proxy
        # does not require authentication. If auth_token is provided below, this
        # is overwritten with an empty string to match OpenRouter's guidance.
        "ANTHROPIC_API_KEY": "not-required-for-course-proxy",
    }
    if auth_token:
        env["ANTHROPIC_AUTH_TOKEN"] = auth_token
        env["ANTHROPIC_API_KEY"] = ""

    mcp_tools = [
        list_repo_files_tool,
        read_file_tool,
        count_test_files_tool,
        detect_ci_files_tool,
        detect_license_tool,
        recent_commits_tool,
        summarize_dependency_files_tool,
        detect_risky_scripts_tool,
    ]
    mcp_server = create_sdk_mcp_server(
        name="repo_health",
        version="1.0.0",
        tools=mcp_tools,
    )

    mcp_tool_names = [
        "mcp__repo_health__list_repo_files",
        "mcp__repo_health__read_file",
        "mcp__repo_health__count_test_files",
        "mcp__repo_health__detect_ci_files",
        "mcp__repo_health__detect_license",
        "mcp__repo_health__recent_commits",
        "mcp__repo_health__summarize_dependency_files",
        "mcp__repo_health__detect_risky_scripts",
    ]

    return ClaudeAgentOptions(
        cwd=repo,
        env=env,
        model=args.model,
        max_turns=args.max_turns,
        permission_mode="dontAsk",
        mcp_servers={"repo_health": mcp_server},
        allowed_tools=["Agent", *mcp_tool_names],
        agents=build_subagents(mcp_tool_names),
        system_prompt=(
            "You are a careful Supervisor Agent. Delegate repository health "
            "analysis to specialist subagents, then integrate their final "
            "answers. Do not edit files."
        ),
    )


async def run_analysis(args: argparse.Namespace) -> str:
    """Run the Claude Agent SDK analysis and return the final result text."""

    global ACTIVE_REPO_ROOT

    repo = resolve_repo_root(Path(args.repo))
    ACTIVE_REPO_ROOT = repo
    proxy_url = load_first_value(Path(args.proxy_file), required=True)
    options = build_options(args, repo, proxy_url)
    prompt = build_prompt(repo, args.question)

    final_results: list[str] = []
    async for message in query(prompt=prompt, options=options):
        # The SDK emits many message types. The final ResultMessage has a
        # "result" attribute, which contains the user-facing final answer.
        if hasattr(message, "result"):
            final_results.append(str(message.result))

        # Optional progress logging helps demonstrate subagent/tool activity
        # during classroom demos without cluttering the default output.
        if args.verbose and hasattr(message, "content") and message.content:
            for block in message.content:
                if getattr(block, "type", None) == "tool_use":
                    print(f"[tool] {getattr(block, 'name', 'unknown')}")

    if not final_results:
        return "No final result was returned by Claude Agent SDK."
    return final_results[-1]


def parse_args() -> argparse.Namespace:
    """Parse command-line options for the Claude Agent SDK implementation."""

    parser = argparse.ArgumentParser(
        description=(
            "Analyze local GitHub repository health with Claude Agent SDK "
            "subagents and local MCP tools."
        )
    )
    parser.add_argument(
        "--repo",
        default=".",
        help="Path to the local repository to analyze. Defaults to the current directory.",
    )
    parser.add_argument(
        "--proxy-file",
        default="config/openrouter_proxy_url.txt",
        help="Text file containing the Anthropic-compatible proxy base URL.",
    )
    parser.add_argument(
        "--auth-token-file",
        default=None,
        help=(
            "Optional file containing an OpenRouter auth token. The course AWS "
            "proxy normally does not need this."
        ),
    )
    parser.add_argument(
        "--model",
        default="anthropic/claude-3.5-haiku",
        help="Model name to request through the proxy.",
    )
    parser.add_argument(
        "--question",
        default="Should I use this repository in my project?",
        help="Decision question the final report should answer.",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=12,
        help="Maximum Claude Agent SDK turns for the supervisor workflow.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print tool/subagent progress while the SDK runs.",
    )
    return parser.parse_args()


def main() -> None:
    """Script entry point."""

    args = parse_args()
    report = asyncio.run(run_analysis(args))
    print("\n# Repository Health Report\n")
    print(report)


if __name__ == "__main__":
    main()
