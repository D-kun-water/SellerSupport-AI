"""List the actual Git submission files, including new untracked files."""
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT=Path(__file__).resolve().parents[1]
DOCUMENTS=('README.md','docs/使用说明.md','docs/开发说明.md','docs/产品说明.md','docs/architecture/系统架构.md')


def source_files(root=ROOT):
    root=Path(root).resolve()
    if not shutil.which('git'):
        raise RuntimeError('Git is required for the repository check.')
    if (root/'.git').exists():
        command=['git','-C',str(root),'ls-files','--cached','--others','--exclude-standard','-z']
        raw=subprocess.check_output(command)
    else:
        folder=root/'storage'
        folder.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='git-check-',dir=folder) as temporary:
            subprocess.run(['git','init','--bare','--quiet',temporary],check=True)
            raw=subprocess.check_output(['git','--git-dir='+temporary,'--work-tree='+str(root),
                                         'ls-files','--cached','--others','--exclude-standard','-z'])
    return sorted({root/name for name in raw.decode('utf-8').split('\0') if name and (root/name).exists()})
