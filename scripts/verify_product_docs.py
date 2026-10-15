"""Check current local documentation links and evidence without importing the app."""
import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0,str(ROOT))


def verify():
    from scripts.project_files import DOCUMENTS
    docs = [ROOT / name for name in DOCUMENTS]
    missing, empty = [], []
    checked = 0
    for path in docs:
        text = path.read_text(encoding='utf-8')
        if not text.strip():
            empty.append(str(path.relative_to(ROOT)))
        # Ignore code fences and code examples, not actual Markdown destinations.
        clean = re.sub(r'```[\s\S]*?```', '', text)
        clean = re.sub(r'`[^`\n]*`', '', clean)
        for match in re.finditer(r'!?\[[^\]\n]*\]\((<[^>]+>|[^\s)]+)(?:\s+"[^"]*")?\)', clean):
            target = match.group(1).strip('<>')
            if target.startswith('#') or urlsplit(target).scheme:
                continue
            target = unquote(target.split('#', 1)[0].split('?', 1)[0])
            if not target:
                continue
            checked += 1
            resolved = (ROOT / target.lstrip('/')) if target.startswith('/') else (path.parent / target)
            if not resolved.exists():
                missing.append({'file': str(path.relative_to(ROOT)), 'target': target})
    return {
        'state': 'passed' if not (missing or empty) else 'failed',
        'markdown_files': len(docs), 'local_links_checked': checked,
        'missing_links': missing, 'empty_markdown': empty,
    }



if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    result = verify()
    if args.report:
        destination = args.report.resolve()
        if not destination.is_relative_to(ROOT):
            raise ValueError('Report must remain in the workspace.')
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result['state'] == 'passed' else 1)
