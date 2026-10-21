"""Run one prompt through a profile's CLI routing, for shell scripts.

Cron jobs such as the weekly digest used to call ``codex exec -m <model>``
directly, which meant a second, hard-coded copy of the model choice. When
that model retired the digest failed every week without anyone noticing,
because the Python side had been migrated and the shell side had not.
This entry point gives scripts the same model resolution, isolation and
fallback that compile uses.

Usage::

    python3 -m agent2kb.llm_run --profile alpha --purpose aux < prompt.txt

The prompt is read from stdin and the model's text is written to stdout.
``--print-language-rule`` prints the profile's language rule for the digest
prompt instead of calling a model, so scripts never hard-code a language.

The profile's ``compile_provider`` decides the route: ``cli`` goes through the
CLI router (with its fallback CLI); an API or Ollama provider goes through the
same provider call compile uses for its aux work, but only that provider: no
other API or local provider is tried, so a scheduled job sends content nowhere
but the provider the KB names.
Exit status: 0 success, 2 this call failed, 3 every configured CLI is down
(for an API or Ollama profile: the provider is hard-down).
"""

from __future__ import annotations

import argparse
import sys

from loguru import logger

from agent2kb.cli_runner import PURPOSES, CliError, ProviderExhaustedError, router_for
from agent2kb.config import resolve_profile
from agent2kb.language import digest_language_rule
from agent2kb.models import LLMProvider


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m agent2kb.llm_run")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--purpose", choices=PURPOSES, default="aux")
    parser.add_argument(
        "--print-language-rule",
        action="store_true",
        help="Print the profile's output-language rule for digest prompts and exit "
        "(no LLM call, no stdin).",
    )
    parser.add_argument(
        "--system",
        default="Follow the user's instructions exactly. Output only the requested content.",
        help="System prompt; the stdin text is the user message.",
    )
    args = parser.parse_args(argv)

    # Logs go to stderr so stdout carries only the model's text.
    logger.remove()
    logger.add(sys.stderr, level="INFO")

    if args.print_language_rule:
        sys.stdout.write(digest_language_rule(resolve_profile(args.profile).output_language))
        sys.stdout.write("\n")
        return 0

    prompt = sys.stdin.read()
    if not prompt.strip():
        logger.error("Empty prompt on stdin")
        return 2

    profile = resolve_profile(args.profile)
    if profile.compile_provider is not LLMProvider.CLI:
        return _run_provider(profile, args.purpose, args.system, prompt)

    router = router_for(profile, args.purpose)
    try:
        result = router.run(args.system, prompt)
    except ProviderExhaustedError as e:
        logger.error(f"All configured CLIs are unavailable: {e}")
        return 3
    except CliError as e:
        logger.error(f"LLM call failed: {e}")
        return 2

    cost = f", ${result.cost_usd:.4f}" if result.cost_usd is not None else ""
    logger.info(f"llm_run: {result.target} ({result.input_tokens} in / "
                f"{result.output_tokens} out{cost})")
    sys.stdout.write(result.text + "\n")
    return 0


def _run_provider(profile, purpose: str, system: str, prompt: str) -> int:
    """Run the prompt through an API or Ollama provider, as compile's aux calls do."""
    # Imported here: the CLI path above must not pay for the compiler import.
    from agent2kb.compile.compiler import Compiler
    from agent2kb.extras import MissingExtraError

    # No S3 access is needed for a single call. Only the configured provider is tried:
    # compile's chain would send a cloud profile's content to whichever other cloud
    # provider has a key, and a scheduled script has no --allow-cloud-fallback to ask.
    compiler = Compiler(profile, s3=None, allow_cloud_fallback=False)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt},
    ]
    try:
        text = compiler._call_llm_with_fallback(messages, purpose=purpose, primary_only=True)
    except ProviderExhaustedError as e:
        logger.error(f"All configured providers are unavailable: {e}")
        return 3
    except MissingExtraError as e:
        logger.error(str(e))
        return 2
    except Exception as e:
        logger.error(f"LLM call failed: {e}")
        return 2
    if not text or not text.strip():
        logger.error("LLM returned an empty response")
        return 2

    logger.info(f"llm_run: {profile.compile_provider.value} ({compiler.model or 'default'})")
    sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
