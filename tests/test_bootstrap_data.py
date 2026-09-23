"""Technical ZIP-security fixtures, not invented business spreadsheets."""

from hashlib import sha256
import io
from pathlib import Path
import stat
from zipfile import ZipFile, ZipInfo

import pytest

from scripts import bootstrap_data as module


CONTENT = b"technical archive validation fixture, not spreadsheet business data"
NAME = "IEK/security-check.xlsx"
EXPECTED = {NAME: sha256(CONTENT).hexdigest()}


def archive_bytes(name=NAME, mode=None):
    buffer = io.BytesIO()
    with ZipFile(buffer, "w") as archive:
        entry = ZipInfo(name)
        if mode is not None:
            entry.create_system = 3
            entry.external_attr = mode << 16
        archive.writestr(entry, CONTENT)
    return buffer.getvalue()


def write_archive(tmp_path, content):
    archive = tmp_path / "security.zip"
    archive.write_bytes(content)
    return archive


@pytest.mark.parametrize("name", ["../escape.xlsx", "/escape.xlsx", "C:/escape.xlsx", "IEK/../escape.xlsx", "IEK\\escape.xlsx", "IEK/extra.xlsx"])
def test_rejects_unexpected_or_escaping_archive_paths(tmp_path, name):
    output = tmp_path / "issued"
    with pytest.raises(ValueError):
        module.extract_archive(write_archive(tmp_path, archive_bytes(name)), output, EXPECTED)
    assert not output.exists()


def test_rejects_archive_symlink_before_writing(tmp_path):
    with pytest.raises(ValueError, match="ссылка"):
        module.extract_archive(write_archive(tmp_path, archive_bytes(mode=stat.S_IFLNK | 0o777)), tmp_path / "issued", EXPECTED)
    assert not (tmp_path / "issued").exists()


def test_rejects_wrong_unpacked_hash(tmp_path):
    with pytest.raises(ValueError, match="SHA256"):
        module.extract_archive(write_archive(tmp_path, archive_bytes()), tmp_path / "issued", {NAME: "0" * 64})


def test_duplicate_zip_member_is_rejected_before_writing(tmp_path):
    buffer = io.BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr(NAME, CONTENT)
        with pytest.warns(UserWarning):
            archive.writestr(NAME, CONTENT)
    output = tmp_path / "issued"
    with pytest.raises(ValueError, match="дубликат"):
        module.extract_archive(write_archive(tmp_path, buffer.getvalue()), output, EXPECTED)
    assert not output.exists()


@pytest.mark.parametrize("payload,expected_size,expected_hash", [
    (b"bad", 3, "0" * 64), (b"too large", 1, "0" * 64), (b"short", 8, sha256(b"short").hexdigest()),
])
def test_download_rejects_hash_or_size_before_extraction(tmp_path, monkeypatch, payload, expected_size, expected_hash):
    monkeypatch.setattr(module, "urlopen", lambda *_args, **_kwargs: io.BytesIO(payload))
    spec = {"name": "security.zip", "url": "https://example.invalid/security", "size": expected_size, "sha256": expected_hash}
    with pytest.raises(ValueError):
        module.download_archive(spec, tmp_path / "download.zip")


def test_existing_partial_data_is_untouched_without_network(tmp_path, monkeypatch):
    output = tmp_path / "issued"
    output.mkdir()
    marker = output / "keep.txt"
    marker.write_text("owned by user")
    monkeypatch.setattr(module, "download_archive", lambda *_: pytest.fail("Unexpected network"))
    with pytest.raises(ValueError):
        module.bootstrap(output)
    assert marker.read_text() == "owned by user"
    assert list(output.iterdir()) == [marker]


def test_bootstrap_is_idempotent_and_exact(tmp_path, monkeypatch):
    payload, calls = archive_bytes(), []
    spec = {"name": "security.zip", "folder": "IEK", "url": "https://example.invalid/security",
            "size": len(payload), "sha256": sha256(payload).hexdigest()}
    monkeypatch.setattr(module, "FILE_HASHES", EXPECTED)
    monkeypatch.setattr(module, "ARCHIVES", (spec,))
    def fetch(*_args, **_kwargs):
        calls.append(1)
        return io.BytesIO(payload)
    monkeypatch.setattr(module, "urlopen", fetch)
    output = tmp_path / "issued"
    assert module.bootstrap(output) == "downloaded"
    original_mtime = (output / NAME).stat().st_mtime_ns
    assert module.bootstrap(output) == "reused"
    assert calls == [1]
    assert (output / NAME).stat().st_mtime_ns == original_mtime
    assert (output / NAME).read_bytes() == CONTENT
    assert sorted(p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file()) == [NAME]


def test_changed_existing_file_is_not_repaired(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "FILE_HASHES", EXPECTED)
    output = tmp_path / "issued"
    (output / "IEK").mkdir(parents=True)
    (output / NAME).write_bytes(b"user changed this")
    with pytest.raises(ValueError, match="не перезаписываются"):
        module.bootstrap(output)
    assert (output / NAME).read_bytes() == b"user changed this"


@pytest.mark.parametrize("directory", ["web/issued", ".git/issued", "data/../issued"])
def test_refuses_public_or_git_output_directory(tmp_path, directory):
    with pytest.raises(ValueError):
        module.bootstrap(tmp_path / directory)
