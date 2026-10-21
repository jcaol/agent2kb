"""Wiki linter: check for broken wikilinks, frontmatter issues, and formatting."""

from __future__ import annotations

import re
from typing import Optional

from loguru import logger

from agent2kb.language import health_report_headings, health_report_language_clause
from agent2kb.models import Frontmatter, LLMProvider, ProfileConfig
from agent2kb.s3_client import S3Client
from agent2kb.utils import extract_wikilinks, slugify


class WikiLinter:
    """Lint wiki pages for quality issues."""

    def __init__(self, profile: ProfileConfig, s3: S3Client):
        self.profile = profile
        self.s3 = s3

    def lint(
        self,
        auto_fix: bool = False,
        check_links: bool = True,
        dry_run: bool = False,
    ) -> list[str]:
        """Run all lint checks. Returns list of issue descriptions.

        Unresolved wikilinks are NOT reported as errors in the default
        pass. Karpathy's wiki pattern treats orphan wikilinks (mention
        without page) as useful "ghost nodes" in Obsidian's graph view,
        so a missing target is informational, not a defect.

        ``check_links`` (default ``True``) controls whether the resolver
        runs at all. ``auto_fix=True`` runs the rewriter against ghost
        links and reports both the confident fixes and the residual
        targets the rewriter could not match — that residual set is the
        only time unresolved wikilinks surface as lint output.

        ``dry_run`` runs the auto-fix matcher but skips every S3 upload —
        report lines are prefixed ``(dry-run)`` so a tail-grep can verify
        what *would* change before actually writing. Implies ``auto_fix``
        for the matcher; pass ``dry_run=True, auto_fix=False`` and the
        matcher still runs (handy for previewing the wikilink rewriter
        on a fresh KB without exposing the underlying CLI flag combo).
        """
        # Dry-run forces the matcher path on so the caller sees what
        # *would* be rewritten, without writing anything.
        if dry_run:
            auto_fix = True
        wiki_keys = self.s3.list_wiki_files()
        if not wiki_keys:
            return []

        # Build a comprehensive resolution table for wikilinks. Same page
        # can be referenced by title, by filename, by slugified-title,
        # or by ``concepts/slug`` form, and a robust lint needs all four.
        # Keys are lowercased so resolution is case-insensitive.
        known_titles: set[str] = set()
        resolution: dict[str, str] = {}  # lookup_key → s3_key
        pages: dict[str, tuple[Frontmatter, str]] = {}

        for key in wiki_keys:
            try:
                content = self.s3.download(key)
                fm, body = Frontmatter.from_yaml_block(content)
                pages[key] = (fm, body)
                filename = key.split("/")[-1].replace(".md", "")
                known_titles.add(filename.lower())
                resolution[filename.lower()] = key
                resolution[slugify(filename).lower()] = key
                if fm.title:
                    known_titles.add(fm.title.lower())
                    resolution[fm.title.lower()] = key
                    resolution[slugify(fm.title).lower()] = key
                # Concept pages are commonly referenced via short form:
                # [[PostgreSQL]] resolves to wiki/concepts/postgresql.md.
                # Strip the concepts/ prefix in lookups so both forms work.
                if "/concepts/" in key:
                    short = filename
                    resolution[f"concepts/{short}".lower()] = key
                # Topic pages support both [[topics/<slug>]] (canonical
                # form, what compile emits in index.md) and [[<slug>]]
                # (short form, used in body prose). Both must resolve.
                if "/topics/" in key:
                    short = filename
                    resolution[f"topics/{short}".lower()] = key
            except Exception as e:
                logger.warning(f"Cannot read {key}: {e}")

        # Outputs/ resolver — closes the wiki ↔ query-output graph gap.
        # Query answers land at outputs/<timestamp>-<slug>.md (see
        # QueryEngine.save_output). Without this, wiki pages that link
        # back to a past query — e.g. ``[[query:20260512-203432-Compare-
        # Redis-Cluster...]]`` cited from a concept page — render as
        # broken even though the file exists in S3 (and in the user's
        # Obsidian vault after Remotely Save sync). Three forms accepted
        # so both the path-style ``[[outputs/<slug>]]`` and the legacy
        # ``[[query:<slug>]]`` prefix resolve, and Obsidian's bare
        # ``[[<basename>]]`` form works too.
        # Resolution-only (NOT added to ``known_titles``): outputs
        # filenames are long timestamped slugs that ratio-based auto-fix
        # could erroneously match against typo'd concept links. Keeping
        # them out of the candidate pool means broken-link detection
        # finds them, but auto-fix will never rewrite a wiki link to
        # point at a query log.
        for out_key in self.s3.list_output_files():
            stem = out_key.split("/")[-1].replace(".md", "")
            stem_low = stem.lower()
            resolution[stem_low] = out_key
            resolution[f"outputs/{stem}".lower()] = out_key
            resolution[f"query:{stem}".lower()] = out_key

        issues = []

        for key, (fm, body) in pages.items():
            page_issues = []
            filename = key.split("/")[-1]
            is_meta = filename in {"index.md", "log.md"}
            is_concept = "/concepts/" in key
            is_topic = "/topics/" in key
            # Article = not meta, not concept, not topic. Articles are where
            # provenance actually lives in the Karpathy pattern; the others
            # are derived/curated and don't carry an upstream source field.
            is_article = not is_meta and not is_concept and not is_topic

            # Check 1: Missing frontmatter fields
            # - title/date: universal. Concepts, topics, index, log all need these.
            # - source: ONLY articles. Concepts are synthesized from other
            #   articles, topics are hand-pinned references, and index.md/log.md
            #   are meta files. Requiring source on these would generate
            #   noisy false positives.
            if not fm.title:
                page_issues.append(f"{key}: missing title in frontmatter")
            if not fm.date:
                page_issues.append(f"{key}: missing date in frontmatter")
            if is_article and not fm.source:
                page_issues.append(f"{key}: missing source in frontmatter")

            # Check 2: Broken wikilinks. Resolution tries title, slug,
            # filename, and concepts/short-form variants. Whatever
            # remains unresolved after all of those is genuinely broken
            # (the concept doesn't exist) — distinguishing "actually
            # missing" from "wrong link form" was the whole point of
            # building the multi-form resolution table above.
            broken_in_page: list[str] = []
            for raw_link in extract_wikilinks(body):
                # Strip |display alias and #anchor — only the target matters.
                target = raw_link.split("|", 1)[0].split("#", 1)[0].strip()
                if not target:
                    continue
                if not _resolve_link(target, resolution):
                    broken_in_page.append(target)
            if broken_in_page and check_links:
                unique = set(broken_in_page)
                # Auto-fix attempts nearest-match replacement (edit
                # distance <= 3 against the resolution table). Karpathy
                # ghost nodes are preserved when no match is close
                # enough — only confident rewrites land on disk.
                if auto_fix:
                    body, fixed_links = _autofix_wikilinks(
                        body, unique, resolution, known_titles
                    )
                    if fixed_links:
                        sample = ", ".join(
                            f"{orig!r}→{new!r}" for orig, new in list(fixed_links.items())[:3]
                        )
                        unique -= set(fixed_links)
                        if dry_run:
                            # Preview only — never touch S3. Tag every
                            # report line so a tail-grep can confirm
                            # nothing was actually written.
                            page_issues.append(
                                f"{key}: (dry-run) WOULD auto-fix "
                                f"{len(fixed_links)} wikilink(s) — {sample}"
                            )
                        else:
                            try:
                                full = self._build_full_content(fm, body)
                                self.s3.upload(key, full)
                                pages[key] = (fm, body)
                                page_issues.append(
                                    f"{key}: auto-fixed {len(fixed_links)} wikilink(s) — {sample}"
                                )
                            except Exception as e:
                                logger.warning(f"Wikilink auto-fix upload failed for {key}: {e}")
                if unique and auto_fix:
                    # Ghost wikilinks (Obsidian-style placeholders for
                    # not-yet-written pages) are part of the Karpathy
                    # wiki workflow, so we don't surface them as lint
                    # errors in the default pass. We do report them
                    # under --auto-fix so the user can see which links
                    # the auto-rewriter couldn't confidently resolve —
                    # those are the ones that genuinely need attention.
                    shown = sorted(unique)[:5]
                    more = f" (+ {len(unique) - 5} more)" if len(unique) > 5 else ""
                    page_issues.append(
                        f"{key}: {len(unique)} unresolved wikilink(s) — "
                        f"{', '.join(repr(t) for t in shown)}{more}"
                    )

            # Check 3: Empty body
            if not body.strip():
                page_issues.append(f"{key}: empty page body")

            # Check 4: No headings
            # concept_synthesis_prompt() produces concept pages that use
            # **bold** section labels (Definition, Key Points, Related
            # Articles, Related Concepts) rather than # H1/H2 headings,
            # so the heading check doesn't apply to concepts. Articles and
            # meta files still need to have headings.
            if (
                not is_concept
                and body.strip()
                and not any(line.strip().startswith("#") for line in body.split("\n"))
            ):
                page_issues.append(f"{key}: no headings found")

            # Check 5: Visible source line — article pages with a known source
            # should surface it so Obsidian readers can trace provenance.
            # Excludes index.md, log.md, and wiki/concepts/ (synthesized).
            if is_article:
                has_source_meta = bool(fm.source or fm.original_link)
                has_visible_source = "> Source:" in body or "> source:" in body
                if has_source_meta and not has_visible_source:
                    page_issues.append(f"{key}: missing visible source line in body")

            # Auto-fix: add missing frontmatter fields. Skipped under
            # dry_run — _auto_fix uploads to S3 directly, no preview hook.
            if auto_fix and page_issues and not dry_run:
                fixed = self._auto_fix(key, fm, body)
                if fixed:
                    page_issues = [(i + " (fixed)" if "missing" in i else i) for i in page_issues]
            elif dry_run and page_issues and any("missing" in i for i in page_issues):
                page_issues = [
                    (i + " (dry-run) WOULD fix" if "missing" in i else i)
                    for i in page_issues
                ]

            issues.extend(page_issues)

        return issues

    def _auto_fix(self, key: str, fm: Frontmatter, body: str) -> bool:
        """Attempt to auto-fix common issues."""
        changed = False

        if not fm.title:
            # Try to extract title from first heading
            for line in body.split("\n"):
                if line.strip().startswith("# "):
                    fm.title = line.strip()[2:].strip()
                    changed = True
                    break
            if not fm.title:
                fm.title = key.split("/")[-1].replace(".md", "")
                changed = True

        if not fm.date:
            fm.date = "unknown"
            changed = True

        if changed:
            try:
                full_content = fm.to_yaml_block() + "\n" + body
                self.s3.upload(key, full_content)
                logger.info(f"Auto-fixed: {key}")
                return True
            except Exception as e:
                logger.error(f"Auto-fix failed for {key}: {e}")

        return False

    def deep_health_check(self, provider: Optional[LLMProvider] = None) -> str:
        """LLM-powered semantic health check across the entire wiki.

        Reads all wiki pages, builds a condensed summary, then asks the LLM to:
        1. Flag contradictions between articles
        2. Find topics mentioned but never explained
        3. Identify claims not backed by raw sources
        4. Suggest new articles to fill knowledge gaps
        """
        from agent2kb.compile.compiler import Compiler

        wiki_keys = self.s3.list_wiki_files()
        skip = {"index.md", "log.md", ".gitkeep"}

        # Build condensed wiki summary for LLM analysis
        summaries = []
        all_wikilinks = set()
        known_pages = set()

        for key in wiki_keys:
            fn = key.split("/")[-1]
            if fn in skip:
                continue
            known_pages.add(fn.replace(".md", "").lower())
            try:
                content = self.s3.download(key)
                fm, body = Frontmatter.from_yaml_block(content)
                title = fm.title or fn.replace(".md", "")
                # Truncate body for LLM context efficiency
                body_preview = body[:800] if len(body) > 800 else body
                summaries.append(
                    f"### {title}\nFile: {fn} | Tags: {', '.join(fm.tags or [])}\n{body_preview}"
                )
                for link in extract_wikilinks(body):
                    all_wikilinks.add(link.lower())
            except Exception:
                continue

        if not summaries:
            return "No wiki pages to analyse."

        # Find orphan wikilinks (mentioned but no page exists)
        orphans = all_wikilinks - known_pages

        # Also check raw sources for coverage
        raw_keys = self.s3.list_raw_files()
        raw_titles = []
        for rk in raw_keys:
            try:
                rc = self.s3.download(rk)
                rfm, _ = Frontmatter.from_yaml_block(rc)
                raw_titles.append(rfm.title or rk.split("/")[-1])
            except Exception:
                continue

        wiki_digest = "\n\n---\n\n".join(summaries[:30])  # Cap at 30 pages

        lang = self.profile.output_language
        sections = "\n".join(
            f"{i}. {heading}" for i, heading in enumerate(health_report_headings(lang), 1)
        )
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a knowledge base health auditor. Analyze the wiki pages below and "
                    "produce a structured health report "
                    f"{health_report_language_clause(lang)}\n\n"
                    "Report sections:\n"
                    f"{sections}\n\n"
                    "Be concise and actionable. Use [[wikilinks]] when referencing pages."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Wiki pages ({len(summaries)} total):\n\n{wiki_digest}\n\n"
                    f"---\n\n"
                    f"Orphan wikilinks (no page exists): "
                    f"{', '.join(sorted(orphans)[:30]) or 'none'}\n\n"
                    f"Raw sources ({len(raw_keys)} files): {', '.join(raw_titles[:20])}\n\n"
                    f"Please produce the health report."
                ),
            },
        ]

        use_provider = provider or self.profile.compile_provider
        compiler = Compiler(profile=self.profile, s3=self.s3, provider=use_provider)
        report = compiler._call_llm_with_fallback(messages, purpose="aux")

        return f"\n## Semantic Health Report\n\n{report}"


    def _build_full_content(self, fm: Frontmatter, body: str) -> str:
        """Re-serialize a page after a body edit. Stays a method so the
        Frontmatter import stays local to the module that owns it.
        """
        return fm.to_yaml_block() + "\n" + body


#: Below this length, ANY edit-distance fix is rejected. Short tokens
#: like "o3", "s3", "ai", "ml" are 1 edit apart from many unrelated
#: concepts; merging them is almost always semantic violence.
_AUTOFIX_MIN_LENGTH = 5

#: Maximum edit-distance / target-length ratio for an auto-fix to be
#: accepted. 0.25 means a 12-char target tolerates up to 3 edits, an
#: 8-char target tolerates 2, and a 4-char target tolerates 1 (but
#: short targets are caught by _AUTOFIX_MIN_LENGTH first). This is
#: stricter than a hard distance cap because long names need more
#: tolerance for typos than short ones do.
_AUTOFIX_MAX_RATIO = 0.25

#: Tech acronyms where 1-edit Levenshtein matches are almost always
#: semantically wrong — e.g. JSONL→json (JSON Lines is NOT JSON), API→CLI
#: (one is a programmatic interface, the other a terminal interface),
#: o3→s3 (an OpenAI model vs an AWS storage service). Auto-fix is
#: short-circuited when EITHER the broken token OR the candidate matches
#: this set, regardless of edit distance. Lowercased for case-insensitive
#: comparison. The list is conservative — anything ambiguous is added.
_AUTOFIX_ACRONYM_BLACKLIST = frozenset({
    # Data formats
    "json", "jsonl", "yaml", "yml", "toml", "xml", "csv", "tsv",
    "html", "css", "scss", "md", "rst",
    # Network protocols / APIs
    "api", "cli", "sdk", "rpc", "rest", "grpc", "graphql", "oauth", "jwt",
    "http", "https", "tcp", "udp", "ssh", "ssl", "tls", "ftp", "smtp",
    # Cloud / infra services
    "s3", "ec2", "rds", "iam", "vpc", "cdn", "dns", "url", "uri",
    # AI/ML model names — short, easily confused
    "ai", "ml", "nlp", "llm", "rag", "moe", "o3", "o1", "v0",
    # Image / media formats
    "jpg", "jpeg", "png", "gif", "svg", "mp3", "mp4", "wav", "pdf",
})


def _is_blacklisted_acronym(s: str) -> bool:
    """Return True if ``s`` (case-insensitive) is in the acronym blacklist."""
    return s.lower() in _AUTOFIX_ACRONYM_BLACKLIST


def _token_diff_acceptable(orig: str, candidate: str) -> bool:
    """Return True when ``orig`` and ``candidate`` share enough whitespace
    tokens that auto-fix is semantically safe.

    Pure character-level Levenshtein cannot distinguish ``Gemini API`` vs
    ``gemini cli`` — they share a 7-char prefix and differ in only 3
    characters, well within the ratio cap. But the differing token (API
    vs CLI) is conceptually critical. This guard splits both names on
    whitespace and rejects the rewrite when more than one token differs
    OR when any differing token is itself a blacklisted acronym (where
    1-token swaps almost always change the meaning).

    Single-token names always pass — the character-level guards already
    handle them and this check would be a no-op anyway.
    """
    orig_tokens = orig.lower().split()
    cand_tokens = candidate.lower().split()
    # Single-token comparisons fall through to other guards.
    if len(orig_tokens) <= 1 and len(cand_tokens) <= 1:
        return True
    common = set(orig_tokens) & set(cand_tokens)
    diff = set(orig_tokens) ^ set(cand_tokens)
    # All-but-one tokens must match (single typo OK), AND no differing
    # token is a known acronym (acronym swaps imply a concept change).
    if len(diff) > 2:  # > 1 token unique on each side combined
        return False
    if any(_is_blacklisted_acronym(t) for t in diff):
        return False
    # Reject when the names share NO tokens — clearly different concepts.
    if not common:
        return False
    return True


def _autofix_wikilinks(
    body: str,
    broken: set[str],
    resolution: dict[str, str],
    known_titles: set[str],
) -> tuple[str, dict[str, str]]:
    """Replace broken `[[wikilinks]]` in ``body`` with nearest-match links.

    Conservative — three guards keep the rewriter from collapsing
    semantically distinct concepts onto each other (the failure mode
    that wrecked an earlier auto-fix run, e.g. "o3"→"s3" and
    "Runway"→"ruby"):

    1. Skip if the original is shorter than ``_AUTOFIX_MIN_LENGTH`` —
       short tokens have too many semantic neighbours at edit distance 1.
    2. Skip if best edit distance / target length exceeds
       ``_AUTOFIX_MAX_RATIO`` — long names tolerate proportionally
       more typo, short names tolerate almost none.
    3. Skip if the unique-nearest check finds a tie (silence beats
       coin-flipping a wikilink target).

    Returns the (possibly modified) body and a ``{old: new}`` map of
    rewrites. Empty map means nothing was confident enough to change.
    """
    fixed: dict[str, str] = {}
    candidates = list(known_titles)
    for orig in broken:
        if len(orig) < _AUTOFIX_MIN_LENGTH:
            continue
        # Guard 1a: blacklisted acronyms reject early. ``[[JSONL]]``
        # broken doesn't get fixed to ``[[json]]`` even though ratio
        # would otherwise allow it — JSON Lines is a distinct concept.
        if _is_blacklisted_acronym(orig):
            continue
        match = _nearest_known_title(orig, candidates)
        if not match or match == orig.lower():
            continue
        # Guard 1b: also reject if the chosen candidate is an acronym;
        # protects ghost nodes like ``[[Gemini API]]`` from collapsing
        # onto the bare acronym page.
        if _is_blacklisted_acronym(match):
            continue
        # Guard 2: token-aware comparison. Catches multi-word phrases
        # where character distance is small but the differing token is
        # the conceptually-critical one (e.g. ``Gemini API`` vs
        # ``gemini cli`` — same prefix, different concept).
        if not _token_diff_acceptable(orig, match):
            continue
        fixed[orig] = match
        # Replace inside [[ ]] only — bare ``orig`` mentions in prose
        # are intentional. Strict regex avoids clobbering adjacent links.
        pattern = re.compile(
            r"\[\[" + re.escape(orig) + r"(\|[^\]]*)?\]\]",
            flags=re.UNICODE,
        )
        body = pattern.sub(lambda m: f"[[{match}{m.group(1) or ''}]]", body)
    return body, fixed


def _nearest_known_title(target: str, candidates: list[str]) -> Optional[str]:
    """Return the unique closest title under the ratio threshold, or None.

    The threshold is ``_AUTOFIX_MAX_RATIO`` of the target length —
    long names tolerate more edits, short names almost none. Returns
    None when no candidate is close enough OR when two candidates tie
    at the same distance.
    """
    target_low = target.lower()
    # Per-target distance cap: floor of ratio * length, minimum 1.
    max_dist = max(1, int(len(target_low) * _AUTOFIX_MAX_RATIO))
    best: Optional[str] = None
    best_dist = max_dist + 1
    tied = False
    for cand in candidates:
        d = _levenshtein(target_low, cand.lower(), cap=max_dist + 1)
        if d < best_dist:
            best, best_dist, tied = cand, d, False
        elif d == best_dist:
            tied = True
    if tied or best_dist > max_dist:
        return None
    return best


def _levenshtein(a: str, b: str, cap: int) -> int:
    """Bounded Levenshtein distance. Returns ``cap`` early when exceeded.

    Pure-Python (no Levenshtein lib dep). The early-exit cap matters at
    KB scale: comparing every broken link to every known title without
    a cap is O(B*T*L²); the cap turns most comparisons into O(L) when
    candidates are clearly far apart.
    """
    if abs(len(a) - len(b)) > cap:
        return cap
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i] + [0] * len(b)
        row_min = cur[0]
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            cur[j] = min(cur[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost)
            row_min = min(row_min, cur[j])
        if row_min >= cap:
            return cap
        prev = cur
    return prev[-1]


def _resolve_link(target: str, resolution: dict[str, str]) -> Optional[str]:
    """Try every reasonable form of ``target`` against the resolution table.

    A wikilink may be written as the page title, its slugified form,
    its filename, or with a ``concepts/`` prefix — and authors mix all
    four freely. Probing each form is the only way to tell genuine
    broken links from cosmetic mismatches.
    """
    candidates = [
        target.lower(),
        slugify(target).lower(),
        target.replace(" ", "-").lower(),
        re.sub(r"\s*/\s*", "-", target).lower(),  # "Data / Données" → "data-données"
    ]
    for c in candidates:
        if c in resolution:
            return resolution[c]

    # Prefix-match fallback for ``query:`` refs only. Old wiki pages
    # baked in ``[[query:<60-char-truncated-stem>]]`` from a buggy
    # compiler emitter (now fixed); the targets don't equal any current
    # outputs/ filename. Search for a unique resolution-table entry that
    # starts with the truncated key. Restricted to ``query:`` so concept
    # links like ``[[json]]`` can't accidentally resolve to a longer
    # ``json-schema`` page via prefix expansion.
    target_low = target.lower()
    if target_low.startswith("query:") and len(target_low) >= 20:
        prefix_matches = [v for k, v in resolution.items() if k.startswith(target_low)]
        unique = set(prefix_matches)
        if len(unique) == 1:
            return prefix_matches[0]
    return None


def build_resolution_fast(
    s3: S3Client, include_outputs: bool = True
) -> tuple[dict[str, str], set[str]]:
    """LIST-only resolution map — no per-page downloads.

    Trade-off vs ``WikiLinter.lint``'s full version: misses title-based
    resolution (matching against frontmatter ``title:`` rather than the
    filename). For the lint pass that's a real omission, but for query
    post-processing it's negligible: LLM-emitted ``[[wikilinks]]`` target
    page slugs/titles, and ``slugify(target).lower()`` already matches
    the filename stem in practice. The win is wall-clock — call this
    per-query (~1s on a 1k-page KB) instead of paying ~30s for the full
    title-aware version.

    Outputs are added to the resolution table but **never** to
    ``known_titles``: the latter feeds the auto-fix matcher, and we
    don't want a typo'd concept link to be silently rewritten to point
    at a query log file.
    """
    resolution: dict[str, str] = {}
    known_titles: set[str] = set()
    for key in s3.list_wiki_files():
        stem = key.split("/")[-1].replace(".md", "")
        stem_low = stem.lower()
        known_titles.add(stem_low)
        resolution[stem_low] = key
        resolution[slugify(stem).lower()] = key
        if "/concepts/" in key:
            resolution[f"concepts/{stem}".lower()] = key
        if "/topics/" in key:
            resolution[f"topics/{stem}".lower()] = key
    if include_outputs:
        for okey in s3.list_output_files():
            stem = okey.split("/")[-1].replace(".md", "")
            stem_low = stem.lower()
            resolution[stem_low] = okey
            resolution[f"outputs/{stem}".lower()] = okey
            resolution[f"query:{stem}".lower()] = okey
    return resolution, known_titles


# Markdown-style fake S3 path citations the LLM sometimes emits inside
# query outputs e.g. " [projects/<profile>/wiki/concepts/foo.md] [9]" —
# these render as nothing useful in Obsidian (no `(target)` part). Strip
# the bracket plus *one* trailing space so the surrounding prose closes
# cleanly without merging adjacent words. Leading whitespace is left
# alone — eating both sides would turn "see [foo.md] for" into "seefor".
_FAKE_MD_PATH_RE = re.compile(r"\[(?:projects/)?[^\]\s]*?\.md\] ?")
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


def clean_links_in_body(
    body: str,
    resolution: dict[str, str],
    known_titles: set[str],
) -> tuple[str, dict[str, int]]:
    """Resolve, alias-rewrite, and unlink wikilinks in ``body``.

    Three-stage cleanup mirroring the manual /tmp/fix-output script:

    1. Drop markdown-style fake S3-path citations (Obsidian-unfriendly).
    2. For each ``[[target|alias?#anchor?]]``:
       a. If ``target`` resolves AND the resolved-stem differs from
          ``target.lower()``, rewrite to
          ``[[<resolved-stem>|<original-target-or-alias>]]`` so the link
          works AND the human-readable display is preserved.
       b. If ``target`` does NOT resolve, run the conservative auto-fix
          matcher (Levenshtein, ratio-bounded, blacklist-protected); a
          confident match is treated as in (a).
       c. Still unresolved → unlink. Drops the ``[[ ]]`` wrapping but
          keeps the alias (or target) as plain prose, so a hallucinated
          ``[[Atomic]]`` doesn't pollute Obsidian's "create new page"
          UI with LLM-invented concept names.

    Returns ``(new_body, stats)`` where ``stats`` has keys ``fake_md``,
    ``aliased``, ``autofixed``, ``unlinked``.
    """
    stats = {"fake_md": 0, "aliased": 0, "autofixed": 0, "unlinked": 0}

    new_body, n = _FAKE_MD_PATH_RE.subn("", body)
    stats["fake_md"] = n
    body = new_body

    candidates = list(known_titles)

    def _rewrite(m: re.Match) -> str:
        inner = m.group(1)
        target_part = inner.split("|", 1)[0]
        alias_part = inner.split("|", 1)[1] if "|" in inner else None
        # Anchor lives on the target-side, never the alias-side, in
        # well-formed wikilinks: ``[[target#anchor|display]]``.
        if "#" in target_part:
            target, anchor = target_part.split("#", 1)
            anchor = "#" + anchor
        else:
            target, anchor = target_part, ""
        target = target.strip()
        if not target:
            return m.group(0)
        display = alias_part.strip() if alias_part is not None else target

        resolved = _resolve_link(target, resolution)
        if resolved:
            stem = resolved.split("/")[-1].replace(".md", "")
            if stem.lower() == target.lower():
                return m.group(0)
            stats["aliased"] += 1
            return f"[[{stem}{anchor}|{display}]]"

        # Try the auto-fix matcher with the same guards lint uses.
        if (
            len(target) >= _AUTOFIX_MIN_LENGTH
            and not _is_blacklisted_acronym(target)
        ):
            match_stem = _nearest_known_title(target, candidates)
            if (
                match_stem
                and match_stem != target.lower()
                and not _is_blacklisted_acronym(match_stem)
                and _token_diff_acceptable(target, match_stem)
            ):
                stats["autofixed"] += 1
                return f"[[{match_stem}{anchor}|{display}]]"

        # Truly broken — unlink, preserve display + anchor as plain text.
        stats["unlinked"] += 1
        return display + anchor

    body = _WIKILINK_RE.sub(_rewrite, body)
    return body, stats
