#!/usr/bin/env python3
"""TalkChief wiki quality gate. Exit 1 on any ERROR.  Needs PyYAML.

Deployed files = everything except Mintlify's built-in exclusions and .mintignore matches
(gitignore semantics via `git check-ignore`). Pages = nav entries (.mdx/.md) + any .mdx
outside snippets/ (which must then be in nav):
  - frontmatter parsed as YAML: title (<=60), description (110-165), unique; sidebarTitle warn
    (SEO checks are skipped for `noindex: true` pages; content/confidentiality checks never are)
  - navigation: every nav page exists, no duplicates, no orphan published pages
  - body: no H1 (title is the H1), no TODO/TBD/FIXME/lorem, no empty links
All deployed text files (pages, snippets, .md, docs.json, .js/.jsx/.ts/.tsx, .svg, ...):
  - no vendor/stack names, no private IPs, no internal hostnames
Usage: python3 scripts/ci/wiki_lint.py [--root .]
"""
import html, ipaddress, json, os, re, shutil, subprocess, sys, tempfile

import yaml

ROOT = sys.argv[sys.argv.index("--root") + 1] if "--root" in sys.argv else "."
TITLE_MAX = 60
DESC_MIN, DESC_MAX = 110, 165

# Mintlify's built-in exclusions (docs: .mintignore). Everything else is governed by
# .mintignore with real gitignore semantics (negation, nested dirs) via `git check-ignore`.
MINT_DEFAULT_EXCLUDES = {".git", ".github", ".claude", ".agents", ".idea", "node_modules",
                         "README.md", "LICENSE.md", "CHANGELOG.md", "CONTRIBUTING.md"}
TEXT_EXT = (".mdx", ".md", ".json", ".yml", ".yaml", ".txt", ".svg", ".js", ".jsx", ".ts", ".tsx", ".css", ".html")
PAGE_EXT = (".mdx", ".md")
SNIPPET_DIRS = ("snippets/",)

# Public docs describe TalkChief capabilities, never the underlying stack or vendors.
BANNED = [
    (re.compile(r"\bmattermost\b", re.I), "vendor name (CoWork is TalkChief's own product)"),
    (re.compile(r"\b(?:freeswitch|kamailio|opensips|freepbx)\b", re.I), "telephony stack name"),
    (re.compile(r"\basterisk\s+(?:pbx|server|dialplan|manager|ami|ari)\b", re.I), "telephony stack name"),
    (re.compile(r"\bhetzner\b", re.I), "hosting provider"),
    (re.compile(r"\b(?:clickhouse|rabbitmq)\b", re.I), "internal data stack"),
    (re.compile(r"\b(?:gemini|openai|chatgpt|anthropic)\b", re.I), "AI vendor name"),
    (re.compile(r"\b(?:api2|app2|app-new|api-new|cdb\d*|billdb|appdb|n8n|bi|kz|bill|router)"
                r"\.talkchief\.io\b", re.I), "internal hostname"),
]
PRIVATE_NETS = [ipaddress.ip_network(n) for n in
                ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10")]
IP_CAND = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?!\d|\.\d)")
# Page prose only: config needs the vendor's $schema URL, and AGENTS.md is the editor's own file.
PROSE_BANNED = [(re.compile(r"\bmintlify\b", re.I), "docs vendor")]
PLACEHOLDER = re.compile(r"\b(TODO|TBD|FIXME|lorem ipsum)\b", re.I)
EMPTY_LINK = re.compile(r"\[[^\]]*\]\(\s*\)")

errors, warnings = [], []


def err(path, msg):
    errors.append(f"ERROR  {path}: {msg}")


def warn(path, msg):
    warnings.append(f"WARN   {path}: {msg}")


def deployed_files():
    cand = []
    for dp, dn, fn in os.walk(ROOT):
        rel_dir = os.path.relpath(dp, ROOT).replace(os.sep, "/")
        dn[:] = [d for d in dn if d not in MINT_DEFAULT_EXCLUDES]
        for f in fn:
            rel = f if rel_dir == "." else f"{rel_dir}/{f}"
            if rel in MINT_DEFAULT_EXCLUDES or rel == ".mintignore":
                continue
            cand.append(rel)
    ignored = set()
    mi = os.path.join(ROOT, ".mintignore")
    if os.path.exists(mi) and cand:
        # Evaluate .mintignore ALONE: a fresh empty repo whose only ignore file is a copy of it,
        # so the wiki repo's .gitignore files, .git/info/exclude and global excludes play no part.
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "init", "-q", tmp], check=True)
            shutil.copyfile(mi, os.path.join(tmp, ".gitignore"))
            # -z: NUL-delimited in/out, so non-ASCII / quoted / tab paths are never C-quoted
            r = subprocess.run(["git", "-c", "core.excludesFile=" + os.devnull, "check-ignore",
                                "--no-index", "-z", "--stdin"], cwd=tmp,
                               input="\0".join(cand) + "\0", capture_output=True, text=True)
        if r.returncode not in (0, 1):
            print(f"ERROR  .mintignore: git check-ignore failed: {r.stderr.strip()}")
            sys.exit(1)
        ignored = set(r.stdout.split("\0")) - {""}
    return sorted(f for f in cand if f not in ignored)


def nav_pages(node, acc):
    if isinstance(node, str):
        acc.append(node.lstrip("/"))
    elif isinstance(node, list):
        for x in node:
            nav_pages(x, acc)
    elif isinstance(node, dict):
        for k, v in node.items():
            if k in ("pages", "groups", "tabs", "anchors", "versions", "languages",
                     "products", "dropdowns", "menu", "navigation", "items"):
                nav_pages(v, acc)
            elif k == "page" and isinstance(v, str):
                acc.append(v.lstrip("/"))
    return acc


def split_frontmatter(text):
    m = re.match(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", text, re.S)
    if not m:
        return None, text
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as e:
        return {"__error__": str(e).splitlines()[0]}, text[m.end():]
    if not isinstance(fm, dict):
        return {"__error__": "frontmatter is not a mapping"}, text[m.end():]
    return fm, text[m.end():]


def strip_code(body):
    body = re.sub(r"```.*?```", "", body, flags=re.S)
    return re.sub(r"`[^`\n]*`", "", body)


def confidentiality(rel, text):
    # scan the source AND its entity-decoded form (M&#97;ttermost, 10&#46;0&#46;0&#46;1 render as text)
    dec = html.unescape(text)
    text = text if dec == text else text + "\n" + dec
    for pat, why in BANNED:
        m = pat.search(text)
        if m:
            err(rel, f"banned term {m.group(0)!r} ({why})")
    for m in IP_CAND.finditer(text):
        try:
            ip = ipaddress.ip_address(m.group(1))
        except ValueError:
            continue
        if any(ip in n for n in PRIVATE_NETS):
            err(rel, f"private IP address {m.group(1)!r}")
            break


def main():
    try:
        cfg = json.load(open(os.path.join(ROOT, "docs.json"), encoding="utf-8"))
    except Exception as e:
        print(f"ERROR  docs.json: invalid JSON ({e})")
        return 1

    files = deployed_files()
    nav = nav_pages(cfg.get("navigation", {}), [])
    nav_set = set(nav)
    # Pages = .mdx/.md that are in navigation, plus any .mdx outside snippets/ (must be in nav).
    # snippets/ and nav-less .md files (e.g. AGENTS.md) are scanned for confidentiality only.
    pages = {}
    for f in files:
        if not f.endswith(PAGE_EXT):
            continue
        stem = f.rsplit(".", 1)[0]
        if stem in nav_set or (f.endswith(".mdx") and not f.startswith(SNIPPET_DIRS)):
            pages.setdefault(stem, f)

    seen = set()
    for p in nav:
        if p in seen:
            err("docs.json", f"page listed twice in navigation: {p}")
        seen.add(p)
        if p not in pages:
            err("docs.json", f"navigation points to missing page: {p}")
    for p, rel in pages.items():
        if p not in seen:
            err(rel, "published page not in navigation (orphan); add it to docs.json, move it under snippets/, or exclude it in .mintignore")

    titles, descs = {}, {}
    for p, rel in sorted(pages.items()):
        text = open(os.path.join(ROOT, rel), encoding="utf-8").read()
        fm, body = split_frontmatter(text)
        if fm is None:
            err(rel, "missing frontmatter block")
            continue
        if "__error__" in fm:
            err(rel, f"invalid frontmatter YAML: {fm['__error__']}")
            continue
        for k in ("title", "sidebarTitle", "description"):
            if k in fm and not isinstance(fm[k], str):
                err(rel, f"frontmatter {k} must be a string")
        if fm.get("noindex") is not True:  # SEO rules apply to indexed pages only
            t = fm.get("title") if isinstance(fm.get("title"), str) else ""
            d = fm.get("description") if isinstance(fm.get("description"), str) else ""
            d = " ".join(d.split())
            if not t:
                err(rel, "missing title")
            elif len(t) > TITLE_MAX:
                err(rel, f"title is {len(t)} chars (max {TITLE_MAX}): {t!r}")
            if not fm.get("sidebarTitle"):
                warn(rel, "missing sidebarTitle")
            if not d:
                err(rel, "missing description (SEO)")
            elif not (DESC_MIN <= len(d) <= DESC_MAX):
                err(rel, f"description is {len(d)} chars (want {DESC_MIN}-{DESC_MAX})")
            if t:
                if t.lower() in titles:
                    err(rel, f"duplicate title with {titles[t.lower()]}")
                titles[t.lower()] = rel
            if d:
                if d.lower() in descs:
                    err(rel, f"duplicate description with {descs[d.lower()]}")
                descs[d.lower()] = rel

        # Content rules apply to every page, noindex or not (noindex pages are still public).
        plain = strip_code(body)
        for i, line in enumerate(plain.splitlines(), 1):
            if re.match(r"^#\s+\S", line):
                err(rel, f"body line ~{i}: H1 heading; the title is the H1, use ## instead")
                break
        ph = PLACEHOLDER.search(plain)
        if ph:
            err(rel, f"placeholder text {ph.group(0)!r}")
        if EMPTY_LINK.search(plain):
            err(rel, "empty link target []()")
        # One image form only: ![plain alt](/images/...png). The post-deploy check compares it
        # to Mintlify's <img>; formatted alt text, titles or <...> destinations would fail there.
        # Every unescaped "![" must start a construct in the permitted grammar; anything else
        # (nested brackets, reference images, broken syntax) is rejected, not skipped.
        strict = re.compile(r"!\[([^\[\]\\\n]*)\]\(([^)\n]*)\)")  # one line only, like MD_IMG
        # scan with fenced code removed but inline code KEPT, so backticks inside alt are seen
        imgsrc = re.sub(r"```.*?```", "", body, flags=re.S)
        # Escaped backticks (\`) are banned: they make code-span boundaries ambiguous between
        # this lint, the post-deploy check and Mintlify's renderer. Use &#96; or a code block.
        if re.search(r"(?<!\\)(?:\\\\)*\\`", imgsrc):
            err(rel, "escaped backtick \\` in prose; rephrase or put it in a code block")
        code_spans = [m.span() for m in re.finditer(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)", imgsrc, re.S)]  # same rule as post_deploy_check.INLINE_CODE
        in_code = lambda i: any(a <= i < b for a, b in code_spans)  # "![" written inside `code` is an example
        def escaped(i):  # odd number of backslashes before "![" makes it literal text
            n = 0
            while i - n - 1 >= 0 and imgsrc[i - n - 1] == "\\":
                n += 1
            return n % 2 == 1
        img_starts = [st.start() for st in re.finditer(r"!\[", imgsrc)
                      if not in_code(st.start()) and not escaped(st.start())]
        for st in img_starts:
            if not strict.match(imgsrc, st):
                err(rel, f"image {imgsrc[st:st + 60]!r}: use ![plain alt](/images/name.png); "
                         "no brackets in alt, no reference-style images")
        for m in (strict.match(imgsrc, st) for st in img_starts):
            if not m:
                continue
            alt, dest = m.group(1), m.group(2)
            if not re.fullmatch(r"/images/[A-Za-z0-9_./-]+\.(png|jpe?g|gif|webp|svg)", dest.strip()):  # ASCII names only
                err(rel, f"image {m.group(0)[:60]!r}: use ![alt](/images/name.png) with no title or <...>")
            # Alt text is plain words and punctuation only (allow-list): no Markdown, HTML, entities,
            # escapes or code, so it renders identically in the repo and in Mintlify's <img alt>.
            elif not re.fullmatch(r"[\w ,.'\"\-–>/()&:…·?%+]*", alt) or re.search(r"&\w*;|(?:^|\W)_\S|\S_(?:\W|$)", alt):
                err(rel, f"image alt text must be plain text (no Markdown formatting): {alt[:60]!r}")
        for pat, why in PROSE_BANNED:
            m = pat.search(text) or pat.search(html.unescape(text))
            if m:
                err(rel, f"banned term {m.group(0)!r} ({why})")

    for rel in files:  # every deployed text file: pages, docs.json, snippets, SVGs
        if rel.lower().endswith(TEXT_EXT):
            try:
                confidentiality(rel, open(os.path.join(ROOT, rel), encoding="utf-8").read())
            except UnicodeDecodeError:
                pass

    for w in warnings:
        print(w)
    for e in errors:
        print(e)
    print(f"\nwiki_lint: {len(pages)} pages, {len(files)} deployed files, "
          f"{len(errors)} error(s), {len(warnings)} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
