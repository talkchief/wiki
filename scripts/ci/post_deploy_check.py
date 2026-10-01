#!/usr/bin/env python3
"""Post-deploy verification for the TalkChief wiki. Exit 1 = not verified.  Needs PyYAML.

1. GitHub check run "Mintlify Deployment" on CIRCLE_SHA1 must complete with success.
   Rate limits are waited out (Retry-After / X-RateLimit-Reset) inside the deadline and
   are never treated as success.
2. Every nav page must serve the commit's exact content: the live `/<page>.md`
   (Mintlify's markdown copy, fetched with a unique query string to avoid stale CDN
   copies), with only Mintlify's generated preamble removed (index note, "# title",
   "> description"), must EQUAL the repo body after canon(), which normalises only the
   rendering differences Mintlify introduces (escapes, bullet/table/quote style, code-fence
   meta, whitespace). Punctuation, signs, identifiers and URLs are compared as-is. Its H1
   must equal the YAML title. Covers additions, deletions anywhere, one-character edits and
   any script (Arabic), and needs no git history.
   Pages that import snippets are rendered with the snippet inlined, so they are checked
   by H1 only and listed as such.
Env: CIRCLE_SHA1, WIKI_URL (default https://docs.talkchief.io), DEPLOY_WAIT_S (900),
     GITHUB_TOKEN (optional, raises the API rate limit).
"""
import html, json, os, re, sys, time, unicodedata, urllib.error, urllib.request

import yaml

REPO = "talkchief/wiki"
SHA = os.environ.get("CIRCLE_SHA1") or (sys.argv[1] if len(sys.argv) > 1 else "")
SITE = os.environ.get("WIKI_URL", "https://docs.talkchief.io").rstrip("/")
DEADLINE = time.time() + int(os.environ.get("DEPLOY_WAIT_S", "900"))
UA = {"User-Agent": "talkchief-wiki-ci"}
FM = re.compile(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", re.S)


def get(url, headers=None):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, r.read().decode("utf-8", "replace")


def sleep_upto(seconds):
    time.sleep(max(1, min(seconds, DEADLINE - time.time())))


def wait_for_mintlify():
    if not SHA:
        print("FAIL no commit sha")
        return False
    hdr = {"Accept": "application/vnd.github+json"}
    if os.environ.get("GITHUB_TOKEN"):
        hdr["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    url = (f"https://api.github.com/repos/{REPO}/commits/{SHA}/check-runs"
           "?check_name=Mintlify%20Deployment")
    while time.time() < DEADLINE:
        try:
            _, body = get(url, hdr)
            runs = json.loads(body).get("check_runs", [])
            if runs:
                r = max(runs, key=lambda x: x.get("started_at") or "")
                print(f"Mintlify Deployment: {r['status']} / {r.get('conclusion')}")
                if r["status"] == "completed":
                    return r.get("conclusion") == "success"
            else:
                print("Mintlify Deployment not reported yet")
            sleep_upto(20)
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                ra = e.headers.get("Retry-After")
                reset = e.headers.get("X-RateLimit-Reset")
                if ra and ra.isdigit():
                    wait = int(ra)
                elif reset and reset.isdigit():
                    wait = int(reset) - int(time.time()) + 2
                else:
                    wait = 60
                print(f"GitHub API rate-limited; waiting {wait}s (deadline still applies)")
                sleep_upto(wait)
            else:
                print(f"GitHub API HTTP {e.code}; retrying")
                sleep_upto(20)
        except Exception as e:
            print(f"GitHub API error {e}; retrying")
            sleep_upto(20)
    print("FAIL Mintlify Deployment not confirmed before the deadline")
    return False


FENCE = re.compile(r"^(\s*)(`{3,}|~{3,})(.*)$")


JSX_TAG = re.compile(r"<[A-Z][\w.]*(?:\s[^<>]*)?/?>")
INLINE_CODE = re.compile(r"(`+)(.+?)\1", re.S)


def _canon_text(s):
    # input is already entity-decoded by _canon_prose().seg() (decode exactly once)
    s = re.sub(r"\\([\\`*_{}\[\]()#+\-.!$|<>~])", r"\1", s)          # markdown escapes added by Mintlify
    s = JSX_TAG.sub(lambda m: re.sub(r"(\s[\w:-]+)='([^'\n]*)'", r'\1="\2"', m.group(0)), s)  # JSX attr quote style, only inside <Component ...> tags
    out = []
    for l in s.split("\n"):
        l = re.sub(r"^(\s*)[*+-]\s+", r"\1- ", l)                        # bullet marker style
        if re.fullmatch(r"\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?\s*", l):   # table separator style
            l = "|---|"
        out.append(l)
    return " ".join("\n".join(out).split())


def _canon_prose(s):
    """Prose outside fences. Inline `code` spans are kept literally (no unescaping, quote
    conversion or whitespace collapsing); only the text between them is normalised.
    No Unicode compatibility folding: URLs and identifiers are compared as written."""
    def seg(t):  # normalise, but keep whether (rendered) whitespace existed at each code-span boundary
        t = html.unescape(t)  # decode first so &#32; / &nbsp; count as the whitespace they render as
        c = _canon_text(t)
        if not c:
            return " " if t and t.isspace() else ""
        return (" " if t[:1].isspace() else "") + c + (" " if t[-1:].isspace() else "")
    out, pos = [], 0
    for m in INLINE_CODE.finditer(s):
        out.append(seg(s[pos:m.start()]))
        out.append("\x00" + m.group(0) + "\x00")
        pos = m.end()
    out.append(seg(s[pos:]))
    return "".join(out).strip()


def canon(s):
    """Canonical text for comparing repo MDX with Mintlify's served /<page>.md.
    Prose: only rendering differences Mintlify introduces are normalised (entities, escapes,
    bullet/table/quote style, whitespace); punctuation, signs and URLs are kept.
    Fenced code: compared literally; only `theme={null}` on the fence line and trailing
    whitespace are dropped."""
    s = s.replace("\r", "")
    parts, prose, fence, code = [], [], None, []
    for line in s.split("\n"):
        m = FENCE.match(line)
        if fence is None:
            if m:
                parts.append(_canon_prose("\n".join(prose))); prose = []
                fence = m.group(2)
                meta = re.sub(r"(?:^|\s+)theme=\{null\}\s*$", "", m.group(3)).strip()  # only the trailing attribute Mintlify appends
                code = [f"{fence}{meta}"]
            else:
                prose.append(line)
        else:
            if m and m.group(2).startswith(fence[0]) and len(m.group(2)) >= len(fence) and not m.group(3).strip():
                code.append(fence)
                parts.append("\n".join(l.rstrip() for l in code)); fence, code = None, []
            else:
                code.append(line)
    if fence is not None:
        parts.append("\n".join(l.rstrip() for l in code))
    parts.append(_canon_prose("\n".join(prose)))
    return "\n".join(p for p in parts if p)


def split_served(served):
    """Drop Mintlify's generated preamble: index blockquote, '# title', '> description'."""
    L = served.replace("\r", "").split("\n")
    i = next((i for i, l in enumerate(L) if l.startswith("# ")), None)
    if i is None:
        return "", served
    h1 = L[i][2:]
    j = i + 1
    while j < len(L) and not L[j].strip():
        j += 1
    while j < len(L) and L[j].startswith(">"):
        j += 1
    return h1, "\n".join(L[j:])


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
    return acc


def repo_page(page):
    for ext in (".mdx", ".md"):
        if os.path.exists(page + ext):
            text = open(page + ext, encoding="utf-8").read()
            m = FM.match(text)
            fm = (yaml.safe_load(m.group(1)) or {}) if m else {}
            body = text[m.end():] if m else text
            title = fm.get("title") if isinstance(fm, dict) else ""
            return str(title or ""), body
    raise FileNotFoundError(page)


def live_md(page):
    _, body = get(f"{SITE}/{page}.md?rev={SHA[:12]}-{time.time_ns()}")
    return body


def check_page(page, title, body, imports):
    h1, live_body = split_served(live_md(page))
    if canon(h1) != canon(title):
        return f"live H1 {h1!r} != repo title {title!r}"
    if not imports and canon(live_body) != canon(body):
        return "live body differs from this commit"
    return None


def verify_pages(pages):
    pending = {}
    for p in pages:
        title, body = repo_page(p)
        imports = bool(re.search(r"^import\s", body, re.M))
        pending[p] = (title, body, imports)
    h1_only = [p for p, v in pending.items() if v[2]]
    reasons = {}
    while pending and time.time() < DEADLINE:
        for p in list(pending):
            try:
                reasons[p] = check_page(p, *pending[p])
            except Exception as e:
                reasons[p] = str(e)
            if reasons[p] is None:
                print(f"OK   /{p}")
                del pending[p]
        if pending:
            print(f"waiting for {len(pending)} page(s) to serve this commit")
            sleep_upto(30)
    for p in pending:
        print(f"FAIL /{p}: {reasons.get(p)}")
    if h1_only:
        print(f"note: H1-only check (page imports snippets): {', '.join(h1_only)}")
    print(f"pages: {len(pages) - len(pending)}/{len(pages)} serve this commit")
    return not pending


if __name__ == "__main__":
    pages = nav_pages(json.load(open("docs.json", encoding="utf-8")).get("navigation", {}), [])
    ok = wait_for_mintlify()
    ok = verify_pages(pages) and ok
    print("\nVERIFIED: commit is live" if ok else "\nNOT VERIFIED")
    sys.exit(0 if ok else 1)
