"""Fetch only the two pinned, public organizer archives; never alter existing data.

Python 3.11+ is required for the archives' legacy CP866 member filenames.
No authentication, executable archive content, or external dependencies are used.
"""

import argparse
from hashlib import sha256
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys
import tempfile
from urllib.error import URLError
from urllib.request import Request, urlopen
from zipfile import BadZipFile, ZipFile


ARCHIVES = (
    {"name": "IEK.zip", "folder": "IEK", "size": 10334256,
     "url": "https://drive.usercontent.google.com/uc?id=18AqNcbkaqrvoLuX9fPsLMleEj7g6CWFb&export=download",
     "sha256": "fed312f909943e69b17d2cc5dd577123b5fe0874064558e8f503deaf9fc50054"},
    {"name": "Systeme_electric.zip", "folder": "Systeme electric", "size": 3037139,
     "url": "https://drive.usercontent.google.com/uc?id=1CkzeElvVm_YwkFCofXX1MGBJuUjMecTz&export=download",
     "sha256": "10ce24d572b750e199bfe8aa9887d467f67471950804392dccc97715dd0344cc"},
)
FILE_HASHES = {
    "IEK/MOQ  ИЭК.xlsx": "4705b5bbbaa7c55541a151f146270bcbdc72a4ce28686fdb3378575733c4a446",
    "IEK/Динамика продаж_2025-2026.xlsx": "7b0100a662eba816fd2e6a477ba56ca2a7846d3489ce1083821c94333ac30e4c",
    "IEK/Ежемесячные остатки продукции за последние 2 года  ИЭК.xlsx": "ad8a8df9cbb242a3df60a147fb4011b146ca7cdd9e98c1247b1b19fd517f4a30",
    "IEK/Ежемесячные продажи в количественном выражении за последние 2 года.xlsx": "048a0af940370153511769396570259a1bb28da63709d9b974f99c45081fa255",
    "IEK/Путь ИЭК 22.09.2026.xlsx": "751fd3c2cbdfabe32f9c20c7f0289135273f007471f39101f03f9c6e1231ce7d",
    "IEK/Сезонность ИЭК.xlsx": "d4d9a8f208884c5197d0dcea0eb60d3972e9382cb3530675d0760fe9a8339635",
    "Systeme electric/MOQ SystemElectric.xlsx": "4fc809ecf9c0a73d97f13f7a16e7dbb20425ab97a6b2d9a0f6be0bbe1a326a7b",
    "Systeme electric/Динамика продаж_Syseme Electric_2025-2026.xlsx": "5ca3997c13f1f65d73db5141122b92deab64f1029bf9b43fd3dd7ade65151128",
    "Systeme electric/Ежемесячные остатки SystemElectric 2024-2026.xlsx": "45d55eb244966ef9fdba1aa09c666e9dbc4c0b05c1088f4041edcd86b908c85e",
    "Systeme electric/Ежемесячные продажи в кол-м выражении SystemElectric 2024-2026.xlsx": "f7782ab9ea5c06d68526543953c6fce7ca83a80d1c1a4e8d88fd083de415f9d0",
    "Systeme electric/Сезонность SystemElectric 2024-2026.xlsx": "19bdbc1c2331d3643e39294f68f11f7427f625107e8b025f6663bb559af669ab",
    "Systeme electric/Товар в пути_SystemElectric на 22.09.2026.xlsx": "17aa50b7697aba5598997b1b4bba9de3950169bc81e97fd10c4d8b0348bd9a3d",
}
MAX_EXPANDED_BYTES = 32 * 1024 * 1024


def file_hash(path):
    digest = sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_link(path):
    """Reject symlinks and Windows junctions without following their targets."""
    if path.is_symlink():
        return True
    return bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)


def validate_files(root, expected):
    if not root.is_dir() or is_link(root):
        raise ValueError("Папка данных отсутствует, не является каталогом или является ссылкой.")
    folders = {name.split("/", 1)[0] for name in expected}
    if {entry.name for entry in root.iterdir()} != folders:
        raise ValueError("Папка данных неполная или содержит посторонние файлы; ничего не изменено.")
    actual = set()
    for folder in folders:
        directory = root / folder
        if is_link(directory) or not directory.is_dir():
            raise ValueError("Ссылки и неправильные каталоги внутри данных запрещены.")
        for entry in directory.iterdir():
            if is_link(entry) or not entry.is_file():
                raise ValueError("В папке данных допустимы только исходные файлы, не ссылки или каталоги.")
            actual.add(entry.relative_to(root).as_posix())
    if actual != set(expected):
        raise ValueError("Состав XLSX отличается от выданного набора; ничего не изменено.")
    for name, checksum in expected.items():
        if file_hash(root / name) != checksum:
            raise ValueError(f"Изменён исходный файл {name}; существующие данные не перезаписываются.")


def download_archive(spec, destination):
    digest, size = sha256(), 0
    request = Request(spec["url"], headers={"User-Agent": "QORAI-organizer-data-bootstrap/1"})
    with urlopen(request, timeout=40) as response, destination.open("xb") as target:
        for chunk in iter(lambda: response.read(1024 * 1024), b""):
            size += len(chunk)
            if size > spec["size"]:
                raise ValueError(f"Размер {spec['name']} не совпал с закреплённым архивом.")
            digest.update(chunk)
            target.write(chunk)
    if size != spec["size"] or digest.hexdigest() != spec["sha256"]:
        raise ValueError(f"SHA256/размер {spec['name']} не совпал: загрузка отклонена до распаковки.")


def extract_archive(archive_path, destination, expected):
    """Validate the complete member set first; never call extractall."""
    folders = {name.split("/", 1)[0] + "/" for name in expected}
    with ZipFile(archive_path, metadata_encoding="cp866") as archive:
        seen, files, expanded = set(), [], 0
        for member in archive.infolist():
            name = member.filename
            parts = PurePosixPath(name).parts
            kind = stat.S_IFMT(member.external_attr >> 16)
            if (not parts or "\\" in name or ":" in name or name.startswith("/")
                    or ".." in parts or "." in name.split("/")
                    or member.orig_filename != name or name in seen
                    or kind not in (0, stat.S_IFREG, stat.S_IFDIR)
                    or member.flag_bits & 1):
                raise ValueError("Небезопасный путь, ссылка, дубликат или шифрованный элемент ZIP.")
            seen.add(name)
            if member.is_dir():
                if name not in folders:
                    raise ValueError("Неожиданный каталог ZIP.")
                continue
            if name not in expected or kind == stat.S_IFDIR:
                raise ValueError("В архиве присутствует невыданный файл.")
            expanded += member.file_size
            if member.file_size < 0 or expanded > MAX_EXPANDED_BYTES:
                raise ValueError("Распакованный архив превышает допустимый размер.")
            files.append(member)
        if {member.filename for member in files} != set(expected):
            raise ValueError("Архив не содержит точный набор ожидаемых XLSX.")
        for member in files:
            target = destination / member.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output, 1024 * 1024)
            if file_hash(target) != expected[member.filename]:
                raise ValueError(f"SHA256 распакованного файла не совпал: {member.filename}.")


def bootstrap(output_dir):
    if sys.version_info < (3, 11):
        raise ValueError("Нужен Python 3.11+ для корректной CP866-кодировки имён ZIP.")
    output = Path(output_dir).expanduser().absolute()
    if ".." in output.parts or any(part.lower() in ("web", ".git") for part in output.parts):
        raise ValueError("Используйте отдельную папку data/issued вне web и .git без переходов '..'.")
    for path in (output, *output.parents):
        if path.is_symlink() or (path.exists() and is_link(path)):
            raise ValueError("Путь назначения не должен содержать ссылки или junction.")
    if output.exists():
        validate_files(output, FILE_HASHES)
        return "reused"
    output.parent.mkdir(parents=True, exist_ok=True)
    # Staging belongs only to this process; existing/partial output is never deleted.
    with tempfile.TemporaryDirectory(prefix="qorai-issued-", dir=output.parent) as temporary:
        temporary = Path(temporary)
        staged = temporary / "issued"
        staged.mkdir()
        for spec in ARCHIVES:
            archive = temporary / spec["name"]
            download_archive(spec, archive)
            expected = {name: checksum for name, checksum in FILE_HASHES.items()
                        if name.startswith(spec["folder"] + "/")}
            extract_archive(archive, staged, expected)
        validate_files(staged, FILE_HASHES)
        # Exclusive creation also prevents replacing an output created during download.
        output.mkdir(exist_ok=False)
        for name in FILE_HASHES:
            target = output / name
            target.parent.mkdir(exist_ok=True)
            with (staged / name).open("rb") as source, target.open("xb") as destination:
                shutil.copyfileobj(source, destination, 1024 * 1024)
        validate_files(output, FILE_HASHES)
    return "downloaded"


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Получить неизменные выданные XLSX без ключей доступа.")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        status = bootstrap(args.output_dir)
        print(f"QORAI: {len(FILE_HASHES)} XLSX проверены по SHA256 ({status}).")
        return 0
    except (ValueError, OSError, URLError, BadZipFile) as error:
        print(f"Ошибка подготовки данных: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
