"""Command-line entry point for PKM indexing, search, and MCP tools."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pkm_framework.index import KnowledgeIndex
from pkm_framework.managed_resources import install_managed_resources, verify_managed_resources
from pkm_framework.skills import install_skills
from pkm_framework.storage import resolve_storage
from pkm_framework.wiki import WikiApplyError, WikiWriter


def _add_storage_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--storage",
        help="Storage repository root (defaults to PKM_STORAGE_DIR or marker discovery)",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pkm", description="Search and read a Markdown PKM vault.")
    commands = parser.add_subparsers(dest="command", required=True)

    index_parser = commands.add_parser("index", help="Rebuild the complete SQLite FTS index")
    _add_storage_option(index_parser)

    search_parser = commands.add_parser("search", help="Search clips and wiki Markdown")
    search_parser.add_argument("query")
    _add_storage_option(search_parser)
    search_parser.add_argument("--limit", type=int, default=10)
    search_parser.add_argument("--json", action="store_true", help="Print structured JSON")

    read_parser = commands.add_parser("read", help="Read one Markdown file")
    read_parser.add_argument("path", help="Vault-relative path, such as raw/clips/example.md")
    _add_storage_option(read_parser)
    read_parser.add_argument("--max-chars", type=int, default=200_000)
    read_parser.add_argument("--offset", type=int, default=0, help="Character offset for paginated reads")
    read_parser.add_argument("--json", action="store_true", help="Include content, truncation, and full-file SHA-256 as JSON")

    wiki_parser = commands.add_parser("wiki", help="Read Wiki write rules or apply a guarded Wiki changeset")
    wiki_commands = wiki_parser.add_subparsers(dest="wiki_command", required=True)
    context_parser = wiki_commands.add_parser("context", help="Print selected Vault Wiki rules and write contract")
    _add_storage_option(context_parser)
    apply_parser = wiki_commands.add_parser("apply", help="Validate and apply one Wiki changeset JSON object")
    apply_parser.add_argument("--request-file", required=True, help="Path to a UTF-8 JSON WikiChangeSet file")
    _add_storage_option(apply_parser)

    skills_parser = commands.add_parser("skills", help="Manage packaged agent skills")
    skills_commands = skills_parser.add_subparsers(dest="skills_command", required=True)
    install_parser = skills_commands.add_parser("install", help="Install skills into a target directory")
    install_parser.add_argument("--target", required=True, help="Target .agents/skills directory")

    storage_parser = commands.add_parser("storage", help="Manage this Storage's Framework-managed resources")
    storage_commands = storage_parser.add_subparsers(dest="storage_command", required=True)
    resource_install_parser = storage_commands.add_parser("install", help="Install or update pinned Framework resources")
    _add_storage_option(resource_install_parser)
    resource_verify_parser = storage_commands.add_parser("verify", help="Verify installed resources against the pinned Framework")
    _add_storage_option(resource_verify_parser)

    mcp_parser = commands.add_parser("mcp", help="Run the stdio MCP server")
    _add_storage_option(mcp_parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "skills":
            installed = install_skills(Path(args.target).expanduser())
            print(f"Installed skills: {', '.join(installed)}")
            return 0

        layout = resolve_storage(args.storage)
        if args.command == "storage":
            if args.storage_command == "install":
                result = install_managed_resources(layout.root)
                print(f"Installed {len(result['installed'])} Framework resources for {result['framework_version']}.")
                if result["removed"]:
                    print(f"Removed retired Framework resources: {', '.join(result['removed'])}")
            elif args.storage_command == "verify":
                result = verify_managed_resources(layout.root)
                print(f"Framework resources are ready ({result['framework_version']}).")
        elif args.command == "index":
            index = KnowledgeIndex(layout)
            count = index.rebuild()
            print(f"Rebuilt FTS index: {count} Markdown files")
        elif args.command == "search":
            index = KnowledgeIndex(layout)
            results, updated = index.search(args.query, args.limit)
            if args.json:
                print(
                    json.dumps(
                        {"results": [result.as_dict() for result in results], "index_changes": updated},
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            elif not results:
                print("No results.")
            else:
                for result in results:
                    print(f"{result.title}\n  {result.path}")
                    if result.url:
                        print(f"  {result.url}")
                    if result.snippet:
                        print(f"  {result.snippet}")
                    print()
        elif args.command == "read":
            index = KnowledgeIndex(layout)
            if not 1 <= args.max_chars <= 200_000:
                raise ValueError("max-chars must be between 1 and 200000.")
            note = index.read(args.path, args.max_chars, args.offset)
            if args.json:
                print(json.dumps(note, ensure_ascii=False, indent=2))
            else:
                print(note["content"], end="" if note["content"].endswith("\n") else "\n")
                if note["truncated"]:
                    print("[Content truncated; increase --max-chars to read more.]", file=sys.stderr)
        elif args.command == "wiki":
            writer = WikiWriter(layout)
            if args.wiki_command == "context":
                print(json.dumps(writer.context(), ensure_ascii=False, indent=2))
            elif args.wiki_command == "apply":
                try:
                    request = Path(args.request_file).expanduser().read_text(encoding="utf-8")
                    result = writer.apply(request)
                except WikiApplyError as exc:
                    print(json.dumps(exc.as_dict(), ensure_ascii=False, indent=2), file=sys.stderr)
                    return 2
                except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
                    print(
                        json.dumps(
                            {"ok": False, "error": str(exc), "partial": False, "applied_paths": []},
                            ensure_ascii=False,
                            indent=2,
                        ),
                        file=sys.stderr,
                    )
                    return 2
                print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == "mcp":
            from pkm_framework.mcp_server import run_server

            # Pass the selected vault itself so the server does not have to
            # rediscover it from a repository root that may contain several vaults.
            run_server(str(layout.vault))
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"pkm: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
