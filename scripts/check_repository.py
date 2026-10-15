"""Check publication files without printing credential values."""
import json
import re
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.project_files import source_files

SECRET=re.compile(r'\b(?:sk-[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9_]{24,}|AKIA[0-9A-Z]{16})\b')
UNWANTED=re.compile(r'\u9762\u8bd5|\u5047\u8bbe|\u6a21\u62df|\u6f14\u793a|'
                    r'de' r'mo|simu' r'lated|synth' r'etic', re.I)


def verify(root=ROOT):
    root=Path(root)
    issues=[]
    files=source_files(root)
    for path in files:
        name=path.relative_to(root).as_posix()
        if not path.is_file():
            issues.append(dict(file=name,reason='required_file_missing'))
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            issues.append(dict(file=name,reason='linked_file'))
            continue
        if Path(name).parts[0] in {'.private','storage','output','outputs'} or any(part in {'node_modules','dist','__pycache__'} for part in Path(name).parts):
            issues.append(dict(file=name,reason='private_or_generated_directory'))
        if UNWANTED.search(name):
            issues.append(dict(file=name,reason='unwanted_filename'))
        clean_name=re.sub(r'TEST-S[0-9]+', 'PRODUCT', name)
        if re.search(r'(?i)(?:^|[/_.-])s[1-3](?:[/_.-]|$)',clean_name):
            issues.append(dict(file=name,reason='internal_stage_filename'))
        if path.suffix.lower() in {'.zip','.sqlite','.sqlite3','.db','.log'} or (path.name.startswith('.env') and path.name!='.env.example'):
            issues.append(dict(file=name,reason='local_or_generated_file'))
        try:
            content=path.read_text(encoding='utf-8-sig')
        except UnicodeDecodeError:
            continue
        for number,line in enumerate(content.splitlines(),1):
            if SECRET.search(line):
                issues.append(dict(file=name,line=number,reason='possible_credential'))
            if UNWANTED.search(line):
                issues.append(dict(file=name,line=number,reason='unwanted_wording'))
    return dict(state='passed' if not issues else 'failed',files=len(files),issues=issues,
                excluded=['.env','.private/','storage/','output/','outputs/','node_modules/','dist/'])


if __name__=='__main__':
    result=verify()
    print(json.dumps(result,ensure_ascii=False,indent=2))
    raise SystemExit(result['state']!='passed')
