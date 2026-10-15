import argparse
import sqlite3
from pathlib import Path

parser=argparse.ArgumentParser(description='SQLite 一致性备份；不删除或覆盖运行数据库')
parser.add_argument('--source', default='storage/sellersupport.sqlite3')
parser.add_argument('--target', required=True)
args=parser.parse_args()
source,target=Path(args.source).resolve(),Path(args.target).resolve()
if not source.is_file() or target.exists() or source==target:
    parser.error('源数据库必须存在，目标必须为新的文件路径')
target.parent.mkdir(parents=True,exist_ok=True)
with sqlite3.connect(f'file:{source.as_posix()}?mode=ro',uri=True) as src,sqlite3.connect(target) as dst:
    src.backup(dst)
    if dst.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
        raise RuntimeError('备份完整性检查失败')
print(target)
