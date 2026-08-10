"""Knowledge base search utility for this repository's research directory.

Usage:
    python kb_search.py 流动性
    python kb_search.py "iv crush" --tag earnings
    python kb_search.py --list-tags
    python kb_search.py gex --max 5
"""
import argparse
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
SOURCES = BASE / "sources.json"
DOCS = sorted(BASE.glob("*.md"))

ALIASES = {
    "流动性": ["liquidity"],
    "期权": ["pricing", "american-options", "greeks"],
    "业绩": ["earnings", "iv-crush"],
    "合规": ["compliance", "sfc"],
    "风险": ["risk", "pin-risk", "assignment"],
    "数据": ["data-license", "free-data", "openapi"],
    "富途": ["futu"],
    "港股": ["hk", "hkex"],
    "保证金": ["margin"],
}

def load_sources():
    with open(SOURCES, encoding="utf-8-sig") as f:
        return json.load(f)["sources"]

def load_docs():
    docs = []
    for p in DOCS:
        text = p.read_text(encoding="utf-8-sig")
        docs.append({"file": p.name, "text": text})
    return docs

def score_text(text, query):
    text_l = text.lower()
    q = query.lower()
    return text_l.count(q)

def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Search GOAI research knowledge base")
    ap.add_argument("query", nargs="?", default=None, help="keyword(s) to search")
    ap.add_argument("--tag", default=None, help="filter sources by tag")
    ap.add_argument("--max", type=int, default=8, help="max results per section")
    ap.add_argument("--list-tags", action="store_true", help="list all source tags")
    args = ap.parse_args()

    sources = load_sources()
    docs = load_docs()

    if args.list_tags:
        tags = sorted({t for s in sources for t in s.get("tags", [])})
        print("TAGS:", ", ".join(tags))
        return

    if not args.query:
        print("Provide a query, e.g. python kb_search.py 流动性")
        return

    q = args.query
    print("=== SOURCES matching '" + q + "'" + (" (tag=" + args.tag + ")" if args.tag else "") + " ===")
    hits = []
    for s in sources:
        if args.tag and args.tag not in s.get("tags", []):
            continue
        blob = " ".join([s["id"], s["title"], s.get("type", ""), " ".join(s.get("tags", [])), s.get("key_points", "")])
        n = score_text(blob, q)
        for k, tags in ALIASES.items():
            if k in q:
                n += sum(blob.lower().count(t) for t in tags)
        if n:
            hits.append((n, s))
    hits.sort(key=lambda x: -x[0])
    if not hits:
        print("(none)")
    for _, s in hits[: args.max]:
        print("[" + s["id"] + "] " + s["title"] + " (" + s.get("type", "") + ")")
        print("    " + s["url"])
        print("    " + s.get("key_points", ""))

    print("")
    print("=== DOCS containing '" + q + "' ===")
    doc_hits = []
    for d in docs:
        n = score_text(d["text"], q)
        if n:
            doc_hits.append((n, d))
    doc_hits.sort(key=lambda x: -x[0])
    if not doc_hits:
        print("(none)")
    for n, d in doc_hits[: args.max]:
        print("[" + d["file"] + "] hits=" + str(n))

if __name__ == "__main__":
    main()
