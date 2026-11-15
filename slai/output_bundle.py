"""Safe, collision-free downloads of already verified conversion outputs."""
from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

from .errors import ValidationFailed
from .files import reserve_path, safe_filename


def artifacts(out: Any) -> list[tuple[Path, str]]:
    """Only registered output artifacts within their containing directory are downloadable."""
    base = out.path.parent.resolve()
    result = [(out.path, safe_filename(out.name))]
    for rel in out.extras:
        candidate = (base / rel).resolve()
        if not candidate.is_relative_to(base) or not candidate.is_file():
            raise ValidationFailed('A supporting output file is missing or outside the output directory')
        parts = Path(rel).parts
        if not parts or any(p in ('', '.', '..') for p in parts):
            raise ValidationFailed('Invalid supporting output filename')
        result.append((candidate, '/'.join(safe_filename(part) for part in parts)))
    return result


def bundle(ws: Any, ids: list[str]) -> Any:
    if (not isinstance(ids, list) or not ids or len(ids) > 500 or
            any(not isinstance(ident, str) or len(ident) > 64 for ident in ids) or len(set(ids)) != len(ids)):
        raise ValidationFailed('Select between 1 and 500 distinct converted results')
    outputs = [ws.output(ident) for ident in ids]
    names: set[str] = set()

    def unique(name: str) -> str:
        if name not in names:
            names.add(name)
            return name
        original = Path(name)
        for n in range(1, 10_000):
            candidate = f'{original.stem} ({n}){original.suffix}'
            if candidate not in names:
                names.add(candidate)
                return candidate
        raise ValidationFailed('Too many duplicate output filenames')

    dest = reserve_path(ws.output_dir(), 'Documaster converted files', '.zip')
    try:
        with zipfile.ZipFile(dest, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
            for out in outputs:
                files = artifacts(out)
                main_name = unique(files[0][1])
                archive.write(files[0][0], main_name)
                for path, rel in files[1:]:
                    archive.write(path, unique(f'{Path(main_name).stem}_files/{rel}'))
        with zipfile.ZipFile(dest) as archive:
            if archive.testzip() is not None:
                raise ValidationFailed('The ZIP archive failed integrity verification')
        return ws.register_output(dest, 'zip')
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
