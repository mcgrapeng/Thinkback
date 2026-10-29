"""变异审计前置（范围指纹）的自检。

门禁本身必须可验证 —— 这是第四轮审计里反复出现的教训：
门禁漏门（静默不生效）比门禁误报危险得多，因为它看起来是绿的。

这里守的是一个**静默失效**的真实缺陷：``only_mutate`` 新增文件后，
``mutants/`` 里已有的普通副本 mtime 比源文件新，mutmut 就当成
「这份文件已有变异体」直接复用那个**空集合** —— 新增文件一个变异体
都不生成，门禁照样绿，实际什么都没审计。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.eval import refresh_mutation_stats as refresh
from tests.eval.refresh_mutation_stats import (
    SCOPE_FIELDS,
    compute_scope_fingerprint,
    scope_changed,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

_SCOPE = """
[tool.mutmut]
source_paths = ["src"]
only_mutate = ["src/thinkback/domain/keys.py"]
pytest_add_cli_args_test_selection = ["tests/unit/test_a.py"]
"""


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "pyproject.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_fingerprint_is_stable_for_the_same_scope(tmp_path: Path) -> None:
    path = _write(tmp_path, _SCOPE)
    assert compute_scope_fingerprint(path) == compute_scope_fingerprint(path)


def test_fingerprint_changes_when_the_mutation_scope_changes(tmp_path: Path) -> None:
    """范围变了必须换指纹 —— 否则旧的 mutants/ 树会被继续复用。"""
    before = compute_scope_fingerprint(_write(tmp_path, _SCOPE))
    after = compute_scope_fingerprint(
        _write(
            tmp_path,
            _SCOPE.replace(
                'only_mutate = ["src/thinkback/domain/keys.py"]',
                'only_mutate = ["src/thinkback/domain/keys.py", '
                '"src/thinkback/domain/recall_policy.py"]',
            ),
        )
    )
    assert before != after


def test_fingerprint_ignores_the_test_selection(tmp_path: Path) -> None:
    """测试选择列表变了不影响「存在哪些变异体」。

    它只影响映射缓存（映射缓存本来就会被丢弃），不该触发全量重生成。
    """
    before = compute_scope_fingerprint(_write(tmp_path, _SCOPE))
    after = compute_scope_fingerprint(
        _write(
            tmp_path,
            _SCOPE.replace(
                'pytest_add_cli_args_test_selection = ["tests/unit/test_a.py"]',
                'pytest_add_cli_args_test_selection = ["tests/unit/test_b.py"]',
            ),
        )
    )
    assert before == after


def test_fingerprint_covers_every_scope_field() -> None:
    """新增一个范围字段时要记得加进指纹 —— 否则它改了没人知道。"""
    assert set(SCOPE_FIELDS) == {
        "source_paths",
        "paths_to_mutate",
        "only_mutate",
        "do_not_mutate",
    }


def test_a_missing_fingerprint_counts_as_changed() -> None:
    """没有指纹文件 = 从没审计过 = 必须按「变了」处理。"""
    assert scope_changed("some-fingerprint") is True


def _mutants_tree(tmp_path: Path) -> Path:
    mutants = tmp_path / "mutants"
    (mutants / "src" / "thinkback").mkdir(parents=True)
    return mutants


def test_a_stale_copy_of_an_in_scope_file_is_removed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """进范围但没有 .meta 的文件：旧副本会遮蔽变异生成，必须删掉。

    这就是「新增文件进范围却一个变异体都没生成」的成因 —— mutmut 看到
    mtime 较新的副本就复用那个空集合，删掉它，新范围才真的被变异。
    """
    mutants = _mutants_tree(tmp_path)
    stale = mutants / "src" / "thinkback" / "new_scope.py"
    stale.write_text("# 范围外时拷进来的普通副本\n", encoding="utf-8")

    monkeypatch.setattr(refresh, "MUTANTS_DIR", mutants)
    monkeypatch.setattr(refresh, "_iter_source_files", lambda: ["src/thinkback/new_scope.py"])

    removed = refresh.clear_stale_scope_artifacts(["src/thinkback/new_scope.py"])

    assert removed == ["src/thinkback/new_scope.py"]
    assert not stale.exists(), "遮蔽变异生成的旧副本没有被删掉"


def test_an_in_scope_file_with_its_own_mutants_is_left_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """已经有 .meta 的文件是真变异树，不能当残留删掉。"""
    mutants = _mutants_tree(tmp_path)
    meta = mutants / "src" / "thinkback" / "kept.py.meta"
    meta.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(refresh, "MUTANTS_DIR", mutants)
    monkeypatch.setattr(refresh, "_iter_source_files", lambda: ["src/thinkback/kept.py"])

    removed = refresh.clear_stale_scope_artifacts(["src/thinkback/kept.py"])

    assert removed == []
    assert meta.exists()


def test_an_out_of_scope_file_keeps_no_stale_mutant_records(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """出范围的文件：.meta / .spans 必须清掉，否则审计范围比声明的大。"""
    mutants = _mutants_tree(tmp_path)
    meta = mutants / "src" / "thinkback" / "left_scope.py.meta"
    spans = mutants / "src" / "thinkback" / "left_scope.py.spans"
    meta.write_text("{}", encoding="utf-8")
    spans.write_text("[]", encoding="utf-8")

    monkeypatch.setattr(refresh, "MUTANTS_DIR", mutants)
    monkeypatch.setattr(refresh, "_iter_source_files", lambda: [])

    removed = refresh.clear_stale_scope_artifacts(["src/thinkback/kept.py"])

    assert removed == ["src/thinkback/left_scope.py"]
    assert not meta.exists()
    assert not spans.exists()


def test_a_do_not_mutate_file_is_out_of_scope() -> None:
    """``do_not_mutate`` 命中 → 出范围（mutmut 的 should_mutate 是「包含且不忽略」）。"""
    assert refresh.in_scope("src/a.py", ["src/a.py"], ["src/a.py"]) is False
    assert refresh.in_scope("src/a.py", ["src/a.py"], []) is True


def test_an_empty_only_mutate_means_every_python_file_is_in_scope() -> None:
    """``only_mutate`` 为空 → 整个 source_paths 都在范围内。"""
    assert refresh.in_scope("src/a.py", [], []) is True
    assert refresh.in_scope("src/a.txt", [], []) is False


def test_the_scope_matches_mutmut_exactly(monkeypatch: pytest.MonkeyPatch) -> None:
    """清理脚本的范围判定必须与 mutmut 的 ``should_mutate`` 完全一致。

    两边口径不一致时，清理脚本会把 mutmut 还要变异的文件当残留删掉，或者
    把 mutmut 不变异的文件留在审计范围里 —— 两边都静默，门禁照样绿。
    这里直接拿 mutmut 自己的实现逐条比对，不靠人肉复述它的规则。
    """
    import dataclasses

    from mutmut.configuration import config, reset_config

    monkeypatch.chdir(REPO_ROOT)
    reset_config()
    try:
        cfg = dataclasses.replace(
            config(),
            only_mutate=["src/thinkback/domain/*.py", "src/thinkback/memory/decay.py"],
            do_not_mutate=["src/thinkback/domain/safety.py"],
        )
    finally:
        reset_config()

    patterns = [str(pattern) for pattern in cfg.only_mutate]
    ignores = [str(pattern) for pattern in cfg.do_not_mutate]
    table = [
        "src/thinkback/domain/keys.py",  # 命中 only_mutate 的 glob
        "src/thinkback/domain/summarization.py",
        "src/thinkback/memory/decay.py",  # 命中 only_mutate 的字面量
        "src/thinkback/domain/safety.py",  # 命中 do_not_mutate
        "src/thinkback/memory/service.py",  # 不在范围内
        "src/thinkback/domain/keys.txt",  # 非 .py
        "README.md",  # 非 .py
    ]
    for path in table:
        assert refresh.in_scope(path, patterns, ignores) == cfg.should_mutate(Path(path)), path
