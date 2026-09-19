"""Build the same source layout as SOC's Hook package builder; never install it."""
import hashlib
import io
import json
import tarfile
from pathlib import Path
from datetime import datetime, timezone


def package(root, platform, output):
    if platform not in ('codex','opencode','openclaw','hermes'):
        return None
    root=Path(root).resolve()
    if not (root/'platforms'/platform).is_dir():return None
    paths=[root/name for name in ('install.sh','install.ps1','uninstall.sh','uninstall.ps1')]
    folders=[root/'install',root/'uninstall',root/'platforms'/platform]
    if platform in ('openclaw','opencode'):folders.append(root/'platforms/core')
    for folder in folders:
        paths.extend(p for p in folder.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc')
    paths=sorted(set(paths))
    files=[]
    for path in paths:
        if path.is_symlink() or root not in path.resolve().parents:raise ValueError('unsafe_package_source')
        files.append((path.relative_to(root).as_posix(),path.read_bytes(),path.stat().st_mode & 0o777))
    digest=hashlib.sha256()
    for name,data,mode in files:
        digest.update(json.dumps([name,len(data),mode]).encode());digest.update(data)
    version='source-'+digest.hexdigest()[:16]
    destination=Path(output)/platform/version
    destination.mkdir(parents=True,exist_ok=True)
    archive=destination/('securityhook-'+platform+'.tar.gz')
    if not archive.exists():
        with tarfile.open(archive,'w:gz') as tar:
            for name,data,mode in files:
                info=tarfile.TarInfo(name);info.size=len(data);info.mode=mode
                tar.addfile(info,io.BytesIO(data))
    metadata={'id':version,'version':version,'file_name':archive.name,'file_size':archive.stat().st_size,
      'checksum':hashlib.sha256(archive.read_bytes()).hexdigest(),'built_by':'SOC source / local build',
      'created_at':datetime.fromtimestamp(archive.stat().st_mtime,timezone.utc).isoformat(),
      'is_current':False,'validation_status':'built_not_runtime_verified'}
    return metadata,archive
