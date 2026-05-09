#!/usr/bin/env python3
"""GitHub Repository Health Analyzer implemented with AG2.

This script follows the supervisor/router pattern requested in the project idea:

Supervisor Agent
    - Repository Inspector Agent
    - Code Quality Agent
    - Risk Assessment Agent
    - Recommendation Agent

The specialist agents use local tool calls to inspect a repository. The
Supervisor integrates the findings into a final recommendation.
"""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path
from typing import Annotated, Any, Callable


# AG2 is installed from the PyPI package named "ag2", but its public Python
# module is still named "autogen".
try:
    from autogen import AssistantAgent, UserProxyAgent
except ImportError as exc:  # pragma: no cover - only runs when dependency is missing.
    raise SystemExit(
        "Missing dependency: install AG2 with `pip install -r requirements.txt`."
    ) from exc


# Directories that usually contain generated files, dependencies, or large
# histories. Skipping them keeps tool output short and focused on source health.
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


# File names that indicate continuous integration configuration.
CI_FILE_CANDIDATES = [
    ".github/workflows",
    ".gitlab-ci.yml",
    "azure-pipelines.yml",
    ".circleci/config.yml",
    "Jenkinsfile",
    ".travis.yml",
    "bitbucket-pipelines.yml",
]


# File names that usually declare runtime or development dependencies.
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


def load_proxy_url(proxy_file: Path) -> str:
    """Read the OpenRouter/course proxy URL from a small text file.

    The file may contain comments, so students can keep a helpful example file
    in git while keeping the real proxy URL in an ignored local file.
    """

    if not proxy_file.exists():
        raise FileNotFoundError(
            f"Proxy file not found: {proxy_file}. Copy "
            "config/openrouter_proxy_url.example.txt to "
            "config/openrouter_proxy_url.txt and paste the proxy URL."
        )

    for line in proxy_file.read_text(encoding="utf-8").splitlines():
        cleaned = line.strip()
        if cleaned and not cleaned.startswith("#"):
            return normalize_openai_base_url(cleaned)

    raise ValueError(f"Proxy file {proxy_file} did not contain a URL.")


def normalize_openai_base_url(url: str) -> str:
    """Convert a full chat-completions endpoint into the base URL AG2 expects.

    AG2 passes this value to the OpenAI-compatible client. That client appends
    the chat-completions path itself, so a file containing
    ".../v1/chat/completions" is shortened to ".../v1".
    """

    return url.rstrip("/").removesuffix("/chat/completions")


def resolve_repo_root(repo: Path) -> Path:
    """Resolve and validate the repository path supplied by the user."""

    repo_root = repo.expanduser().resolve()
    if not repo_root.exists():
        raise FileNotFoundError(f"Repository path does not exist: {repo_root}")
    if not repo_root.is_dir():
        raise NotADirectoryError(f"Repository path is not a directory: {repo_root}")
    return repo_root


def make_safe_resolver(repo_root: Path) -> Callable[[str | Path], Path]:
    """Create a resolver that prevents tools from reading outside repo_root."""

    def resolve(path: str | Path) -> Path:
        candidate = Path(path).expanduser()

        # In prompts we tell agents to use "." for the target repository root.
        # Relative paths are interpreted inside the analyzed repository.
        if not candidate.is_absolute():
            candidate = repo_root / candidate

        resolved = candidate.resolve()
        try:
            resolved.relative_to(repo_root)
        except ValueError as exc:
            raise ValueError(
                f"Refusing to inspect {resolved}; it is outside {repo_root}."
            ) from exc
        return resolved

    return resolve


def relative_path(repo_root: Path, path: Path) -> str:
    """Return a stable, readable path relative to the analyzed repository."""

    try:
        return str(path.relative_to(repo_root))
    except ValueError:
        return str(path)


def walk_repo_files(repo_root: Path, start: Path, limit: int = 500) -> list[str]:
    """Collect a bounded list of repository files for the inspection tools."""

    files: list[str] = []
    for current_root, dirnames, filenames in os.walk(start):
        # Mutating dirnames in-place tells os.walk which directories to skip.
        dirnames[:] = [
            name
            for name in dirnames
            if name not in IGNORED_DIRS and not name.startswith(".DS_Store")
        ]

        for filename in sorted(filenames):
            path = Path(current_root) / filename
            files.append(relative_path(repo_root, path))
            if len(files) >= limit:
                return files
    return files


def read_text_preview(path: Path, max_bytes: int = 12_000) -> dict[str, Any]:
    """Read a file safely as text and report whether the content was truncated."""

    raw = path.read_bytes()
    truncated = len(raw) > max_bytes
    preview = raw[:max_bytes]

    # A null byte is a strong signal that a file is binary. Returning a short
    # note is safer and more useful than sending binary data to the model.
    if b"\x00" in preview:
        return {
            "path": str(path),
            "is_binary": True,
            "truncated": truncated,
            "content": "",
        }

    return {
        "path": str(path),
        "is_binary": False,
        "truncated": truncated,
        "content": preview.decode("utf-8", errors="replace"),
    }


def detect_license_name(text: str) -> str:
    """Guess the license family from common phrases in the license text."""

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


def find_existing_files(repo_root: Path, candidates: list[str]) -> list[Path]:
    """Return candidate files or directory contents that exist in repo_root."""

    matches: list[Path] = []
    for candidate in candidates:
        path = repo_root / candidate
        if path.is_file():
            matches.append(path)
        elif path.is_dir():
            matches.extend(sorted(item for item in path.rglob("*") if item.is_file()))
    return matches


def build_llm_config(proxy_url: str, model: str, temperature: float) -> dict[str, Any]:
    """Create the AG2 LLM configuration for an OpenAI-compatible proxy."""

    return {
        "config_list": [
            {
                "model": model,
                "base_url": proxy_url,
                # The course proxy does not require a real API key, but the
                # OpenAI-compatible client expects a non-empty string.
                "api_key": "not-required-for-course-proxy",
            }
        ],
        "temperature": temperature,
        # Disable AG2's response cache so repeated runs reflect the repository
        # state at execution time.
        "cache_seed": None,
    }


def register_repo_tools(
    repo_root: Path,
    tool_executor: UserProxyAgent,
    llm_tool_agents: list[AssistantAgent],
) -> None:
    """Register local repository inspection tools for AG2 agents.

    AG2 separates tool *selection* from tool *execution*:
    - AssistantAgent instances receive tool schemas and decide which tool to call.
    - UserProxyAgent executes the Python functions and returns tool results.
    """

    resolve = make_safe_resolver(repo_root)

    def list_repo_files(
        path: Annotated[str, "Repository path to list; use '.' for the target root."] = ".",
    ) -> dict[str, Any]:
        """List repository files while skipping generated or dependency folders."""

        start = resolve(path)
        if not start.is_dir():
            raise NotADirectoryError(f"Not a directory: {start}")
        files = walk_repo_files(repo_root, start)
        return {
            "repo_root": str(repo_root),
            "start": relative_path(repo_root, start),
            "file_count_returned": len(files),
            "files": files,
        }

    def read_file(
        path: Annotated[str, "File path to read, relative to the target repository."],
    ) -> dict[str, Any]:
        """Read a bounded text preview of a repository file."""

        file_path = resolve(path)
        if not file_path.is_file():
            raise FileNotFoundError(f"Not a file: {file_path}")
        preview = read_text_preview(file_path)
        preview["path"] = relative_path(repo_root, file_path)
        return preview

    def count_test_files(
        path: Annotated[str, "Repository path to scan; use '.' for the target root."] = ".",
    ) -> dict[str, Any]:
        """Count common test files and report representative examples."""

        start = resolve(path)
        test_files: list[str] = []
        for file_name in walk_repo_files(repo_root, start, limit=2_000):
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

        return {
            "count": len(test_files),
            "examples": test_files[:30],
            "has_tests_directory": any(
                (repo_root / dirname).is_dir() for dirname in ("tests", "test")
            ),
        }

    def detect_ci_files(
        path: Annotated[str, "Repository path to scan; use '.' for the target root."] = ".",
    ) -> dict[str, Any]:
        """Detect common continuous integration configuration files."""

        _ = resolve(path)  # Validates that the requested path stays in repo_root.
        matches = find_existing_files(repo_root, CI_FILE_CANDIDATES)
        return {
            "count": len(matches),
            "ci_files": [relative_path(repo_root, match) for match in matches],
        }

    def detect_license(
        path: Annotated[str, "Repository path to scan; use '.' for the target root."] = ".",
    ) -> dict[str, Any]:
        """Detect the repository license file and make a conservative guess."""

        _ = resolve(path)
        license_files = []
        for candidate in repo_root.iterdir():
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
                        "path": relative_path(repo_root, candidate),
                        "detected_family": detect_license_name(preview["content"]),
                        "preview": preview["content"][:500],
                    }
                )

        return {
            "has_license": bool(license_files),
            "licenses": license_files,
        }

    def recent_commits(
        path: Annotated[str, "Repository path; use '.' for the target root."] = ".",
        limit: Annotated[int, "Maximum number of local commits to return."] = 5,
    ) -> dict[str, Any]:
        """Read recent local git commits if the target directory is a git repo."""

        _ = resolve(path)
        safe_limit = max(1, min(limit, 20))
        command = [
            "git",
            "-C",
            str(repo_root),
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
            return {
                "available": False,
                "reason": completed.stderr.strip() or "No git history available.",
                "commits": [],
            }
        return {
            "available": True,
            "commits": completed.stdout.splitlines(),
        }

    def summarize_dependency_files(
        path: Annotated[str, "Repository path to scan; use '.' for the target root."] = ".",
    ) -> dict[str, Any]:
        """Find dependency files and estimate dependency complexity."""

        _ = resolve(path)
        matches = find_existing_files(repo_root, DEPENDENCY_FILE_CANDIDATES)
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
                    "path": relative_path(repo_root, match),
                    "line_count_previewed": len(content.splitlines()),
                    "rough_dependency_line_count": len(dependency_lines),
                    "truncated": preview["truncated"],
                }
            )
        return {"dependency_files": summaries, "count": len(summaries)}

    def detect_risky_scripts(
        path: Annotated[str, "Repository path to scan; use '.' for the target root."] = ".",
    ) -> dict[str, Any]:
        """Look for simple high-risk shell/script patterns in repository files."""

        start = resolve(path)
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

        for file_name in walk_repo_files(repo_root, start, limit=2_000):
            file_path = repo_root / file_name
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
                        return {"findings": findings, "truncated": True}

        return {"findings": findings, "truncated": False}

    def register(
        func: Callable[..., Any],
        description: str,
        agents: list[AssistantAgent] | None = None,
    ) -> None:
        """Attach one Python function to the executor and selected LLM agents."""

        selected_agents = agents or llm_tool_agents
        for agent in selected_agents:
            agent.register_for_llm(description=description)(func)
        tool_executor.register_for_execution()(func)

    register(
        list_repo_files,
        "List repository files while ignoring generated folders.",
    )
    register(
        read_file,
        "Read a safe text preview of a repository file.",
    )
    register(
        count_test_files,
        "Count local test files and identify whether common test folders exist.",
    )
    register(
        detect_ci_files,
        "Detect common CI workflow files such as GitHub Actions or GitLab CI.",
    )
    register(
        detect_license,
        "Detect a local license file and guess its license family.",
    )
    register(
        recent_commits,
        "Read recent local git commit summaries if git history is available.",
    )
    register(
        summarize_dependency_files,
        "Find dependency files and estimate dependency complexity.",
    )
    register(
        detect_risky_scripts,
        "Scan common source and config files for risky script patterns.",
    )


def clean_summary(summary: str) -> str:
    """Remove AG2's termination marker from text shown to the user."""

    return summary.replace("TERMINATE", "").strip()


def run_specialist_chat(
    tool_executor: UserProxyAgent,
    recipient: AssistantAgent,
    message: str,
    max_turns: int,
    silent: bool,
) -> str:
    """Run one routed specialist conversation and return its summary."""

    result = tool_executor.initiate_chat(
        recipient=recipient,
        message=message,
        max_turns=max_turns,
        summary_method="last_msg",
        clear_history=True,
        silent=silent,
    )
    return clean_summary(str(result.summary))


def build_agents(llm_config: dict[str, Any]) -> dict[str, Any]:
    """Create the supervisor, specialist agents, and local tool executor."""

    termination = lambda msg: "TERMINATE" in str(msg.get("content", ""))

    supervisor = AssistantAgent(
        name="supervisor",
        llm_config=llm_config,
        is_termination_msg=termination,
        system_message=(
            "You are the Supervisor Agent for a Repository Health Analyzer. "
            "You route work to specialists, compare their findings, and produce "
            "a concise final recommendation. Prefer concrete evidence from tool "
            "results over generic advice. End your final answer with TERMINATE."
        ),
    )

    inspector = AssistantAgent(
        name="repository_inspector",
        llm_config=llm_config,
        is_termination_msg=termination,
        system_message=(
            "You are the Repository Inspector Agent. Use local tools to inspect "
            "README files, dependency files, test folders, CI files, license files, "
            "and recent local commits. Your output should be factual and cite the "
            "tool evidence you used. End with TERMINATE."
        ),
    )

    quality = AssistantAgent(
        name="code_quality",
        llm_config=llm_config,
        is_termination_msg=termination,
        system_message=(
            "You are the Code Quality Agent. Use local tools to evaluate project "
            "structure, dependency complexity, documentation, tests, and risky "
            "scripts. Focus on maintainability signals, not stylistic nitpicks. "
            "End with TERMINATE."
        ),
    )

    risk = AssistantAgent(
        name="risk_assessment",
        llm_config=llm_config,
        is_termination_msg=termination,
        system_message=(
            "You are the Risk Assessment Agent. Combine repository inspection "
            "and quality findings into one of these labels: safe to use, use with "
            "caution, avoid, or missing information. Explain the risk drivers and "
            "confidence level. End with TERMINATE."
        ),
    )

    recommendation = AssistantAgent(
        name="recommendation",
        llm_config=llm_config,
        is_termination_msg=termination,
        system_message=(
            "You are the Recommendation Agent. Suggest concrete next steps such "
            "as running tests, checking open issues, pinning versions, reviewing "
            "the license, and inspecting security policy. End with TERMINATE."
        ),
    )

    tool_executor = UserProxyAgent(
        name="local_tool_executor",
        human_input_mode="NEVER",
        code_execution_config=False,
        llm_config=False,
        is_termination_msg=termination,
    )

    return {
        "supervisor": supervisor,
        "inspector": inspector,
        "quality": quality,
        "risk": risk,
        "recommendation": recommendation,
        "tool_executor": tool_executor,
    }


def run_repository_health_analysis(args: argparse.Namespace) -> str:
    """Run the complete supervisor/router workflow."""

    repo_root = resolve_repo_root(Path(args.repo))
    proxy_url = load_proxy_url(Path(args.proxy_file))
    llm_config = build_llm_config(proxy_url, args.model, args.temperature)
    agents = build_agents(llm_config)

    # The Repository Inspector and Code Quality agents are the specialists that
    # need direct local tools. Risk and Recommendation synthesize prior findings.
    register_repo_tools(
        repo_root=repo_root,
        tool_executor=agents["tool_executor"],
        llm_tool_agents=[agents["inspector"], agents["quality"]],
    )

    silent = not args.verbose

    inspection = run_specialist_chat(
        agents["tool_executor"],
        agents["inspector"],
        (
            f"Analyze repository: {repo_root}\n"
            "Use '.' as the repository root when calling tools. You must call "
            "list_repo_files, detect_ci_files, detect_license, count_test_files, "
            "recent_commits, and read_file for README/dependency files if present. "
            "Return findings under: metadata, documentation, tests, CI, license, "
            "dependencies, git_activity, missing_information."
        ),
        max_turns=args.max_turns,
        silent=silent,
    )

    quality = run_specialist_chat(
        agents["tool_executor"],
        agents["quality"],
        (
            f"Analyze maintainability for repository: {repo_root}\n"
            "Use '.' as the repository root when calling tools. You must call "
            "list_repo_files, summarize_dependency_files, count_test_files, and "
            "detect_risky_scripts. You may call read_file for relevant files. "
            f"Repository Inspector findings:\n{inspection}"
        ),
        max_turns=args.max_turns,
        silent=silent,
    )

    risk = run_specialist_chat(
        agents["tool_executor"],
        agents["risk"],
        (
            f"User question: {args.question}\n\n"
            f"Repository Inspector findings:\n{inspection}\n\n"
            f"Code Quality findings:\n{quality}\n\n"
            "Choose exactly one label: safe to use, use with caution, avoid, "
            "or missing information. Explain confidence and evidence."
        ),
        max_turns=3,
        silent=silent,
    )

    recommendation = run_specialist_chat(
        agents["tool_executor"],
        agents["recommendation"],
        (
            f"User question: {args.question}\n\n"
            f"Risk assessment:\n{risk}\n\n"
            "Suggest concrete next steps for a developer deciding whether to "
            "use, contribute to, or depend on this repository."
        ),
        max_turns=3,
        silent=silent,
    )

    final_report = run_specialist_chat(
        agents["tool_executor"],
        agents["supervisor"],
        (
            "Integrate the specialist findings into a final repository health "
            "report. Use this structure:\n"
            "1. Recommendation label\n"
            "2. Short rationale\n"
            "3. Evidence table\n"
            "4. Missing information\n"
            "5. Next steps\n\n"
            f"User question: {args.question}\n\n"
            f"Repository Inspector findings:\n{inspection}\n\n"
            f"Code Quality findings:\n{quality}\n\n"
            f"Risk Assessment findings:\n{risk}\n\n"
            f"Recommendation Agent findings:\n{recommendation}"
        ),
        max_turns=3,
        silent=silent,
    )

    return final_report


def parse_args() -> argparse.Namespace:
    """Parse command-line options for the AG2 implementation."""

    parser = argparse.ArgumentParser(
        description="Analyze local GitHub repository health with an AG2 multi-agent system."
    )
    parser.add_argument(
        "--repo",
        default=".",
        help="Path to the local repository to analyze. Defaults to the current directory.",
    )
    parser.add_argument(
        "--proxy-file",
        default="config/openrouter_proxy_url.txt",
        help="Text file containing the OpenRouter/course proxy base URL.",
    )
    parser.add_argument(
        "--model",
        default="openai/gpt-4.1-mini",
        help="OpenRouter model name to use through the proxy.",
    )
    parser.add_argument(
        "--question",
        default="Should I use this repository in my project?",
        help="Decision question the final report should answer.",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.2,
        help="LLM temperature for the specialist and supervisor agents.",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=8,
        help="Maximum AG2 turns for tool-using specialist conversations.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show the intermediate AG2 conversations and tool calls.",
    )
    return parser.parse_args()


def main() -> None:
    """Script entry point."""

    args = parse_args()
    report = run_repository_health_analysis(args)
    print("\n# Repository Health Report\n")
    print(report)


if __name__ == "__main__":
    main()
