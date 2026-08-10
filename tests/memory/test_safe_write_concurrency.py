"""Concurrency and interrupted-write regressions for vault Markdown writes."""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from openjarvis.memory.safe_write import (
    AtomicMarkdownWriter,
    ConcurrentMemoryWrite,
    sha256_bytes,
)


def test_parallel_writers_do_not_lose_a_committed_change(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    target = vault / "fact.md"
    target.write_text("before\n", encoding="utf-8")
    restore = tmp_path / "restore"
    first = AtomicMarkdownWriter(vault, restore)
    second = AtomicMarkdownWriter(vault, restore)
    expected = sha256_bytes(target.read_bytes())
    barrier = threading.Barrier(3)
    results: list[str] = []
    errors: list[Exception] = []

    def run(writer: AtomicMarkdownWriter, content: str, operation_id: str) -> None:
        barrier.wait()
        try:
            writer.write(
                "fact.md",
                content,
                expected_hash=expected,
                operation_id=operation_id,
            )
            results.append(content)
        except Exception as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    threads = [
        threading.Thread(target=run, args=(first, "first\n", "op-first")),
        threading.Thread(target=run, args=(second, "second\n", "op-second")),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert len(results) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], ConcurrentMemoryWrite)
    assert target.read_text(encoding="utf-8") == results[0]


def test_failed_replace_keeps_original_and_releases_lock(
    tmp_path: Path,
    monkeypatch,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    target = vault / "fact.md"
    target.write_text("before\n", encoding="utf-8")
    atomic = AtomicMarkdownWriter(vault, tmp_path / "restore")
    expected = sha256_bytes(target.read_bytes())
    original_replace = os.replace

    def fail_replace(src, dst):
        if Path(dst) == target:
            raise OSError("simulated interrupted replace")
        return original_replace(src, dst)

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated"):
        atomic.write(
            "fact.md",
            "after\n",
            expected_hash=expected,
            operation_id="failed-operation",
        )

    assert target.read_text(encoding="utf-8") == "before\n"
    assert not list(vault.glob(".*.tmp"))
    assert not (atomic.restore_root / "failed-operation.restore").exists()

    monkeypatch.setattr(os, "replace", original_replace)
    result = atomic.write(
        "fact.md",
        "recovered\n",
        expected_hash=expected,
        operation_id="recovered-operation",
    )
    assert target.read_text(encoding="utf-8") == "recovered\n"
    assert result.after_hash == sha256_bytes(b"recovered\n")
