"""类型策略：配置驱动的抽取指令、来源角色与策略版本。

覆盖三件事：
- 抽取指令随 profile 存放、按文件名带版本，由服务侧与全局默认拼接后传入；
- 非法配置在任何环境都 fail-visible，不静默回落默认策略；
- 每条记忆记录产生它的策略版本与指令版本。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from thinkback.api.dependencies import build_registry_provider
from thinkback.domain.memory_types import (
    MemoryTypeRegistry,
    MemoryTypeSpec,
    load_registry,
    prompt_version_from_filename,
    registry_from_dict,
)
from thinkback.domain.prompts import DEFAULT_EXTRACTION_INSTRUCTIONS, build_extraction_prompt
from thinkback.infra.config import Settings
from thinkback.memory.backends.fake import FakeMemoryBackend
from thinkback.memory.repositories.in_memory import InMemoryMemoryRepository
from thinkback.memory.schemas import AppendMemoryRequest, MessageRole
from thinkback.memory.service import MemoryService


def _profile_payload(types: dict[str, dict[str, Any]], *, version: str = "1") -> dict[str, Any]:
    return {"version": version, "namespace": "acme", "types": types}


def _type_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "extractor": "mem0",
        "source_roles": ["user"],
        "conflict": "overwrite",
        "retention": "forever",
        "classification": "personal",
    }
    payload.update(overrides)
    return payload


class _RecordingBackend(FakeMemoryBackend):
    """记录 add 的指令与 metadata。"""

    def __init__(self) -> None:
        super().__init__()
        self.prompts: list[str | None] = []
        self.metadatas: list[dict[str, Any]] = []

    def add(  # type: ignore[no-untyped-def]
        self,
        messages,
        *,
        user_id,
        memory_scope_id,
        metadata=None,
        infer=True,
        expiration_date=None,
        prompt=None,
    ):
        self.prompts.append(prompt)
        self.metadatas.append(dict(metadata or {}))
        return super().add(
            messages,
            user_id=user_id,
            memory_scope_id=memory_scope_id,
            metadata=metadata,
            infer=infer,
            expiration_date=expiration_date,
            prompt=prompt,
        )


def _append_request(round_id: str, *, metadata: dict[str, Any] | None = None) -> AppendMemoryRequest:
    return AppendMemoryRequest(
        request_id=f"req-{round_id}",
        user_id="user-1",
        session_id="session-1",
        round_id=round_id,
        messages=[
            {
                "message_id": f"{round_id}-u",
                "role": MessageRole.USER,
                "content": "我们主要做 3nm 的工艺",
                "timestamp": "2026-05-04T10:00:00Z",
            },
            {
                "message_id": f"{round_id}-a",
                "role": MessageRole.ASSISTANT,
                "content": "收到。",
                "timestamp": "2026-05-04T10:00:03Z",
            },
        ],
        source_timestamp="2026-05-04T10:00:03Z",
        namespace="acme",
        metadata=metadata or {},
    )


def test_prompt_file_is_loaded_with_version(tmp_path: Path) -> None:
    """抽取指令随 profile 存放，版本从文件名解析。"""
    prompts_dir = tmp_path / "prompts" / "acme"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "process_preference.v3.md").write_text(
        "工艺节点偏好按产线口径抽取。", encoding="utf-8"
    )
    (tmp_path / "acme.json").write_text(
        json.dumps(
            _profile_payload(
                {
                    "process_preference": _type_payload(
                        extraction_prompt="prompts/acme/process_preference.v3.md"
                    )
                }
            )
        ),
        encoding="utf-8",
    )

    registry = load_registry(tmp_path, "acme")
    spec = registry.get("process_preference")

    assert spec.extraction_prompt == "prompts/acme/process_preference.v3.md"
    assert spec.extraction_prompt_text == "工艺节点偏好按产线口径抽取。"
    assert spec.extraction_prompt_version == "v3"
    assert registry.version == "1"


def test_prompt_version_falls_back_to_stem() -> None:
    assert prompt_version_from_filename("prompts/acme/preference.md") == "preference"
    assert prompt_version_from_filename("prompts/acme/preference.v12.md") == "v12"


def test_prompt_merge_keeps_global_constraints_first() -> None:
    """类型指令只能加约束，不能删全局硬约束：全局在前，类型在后。"""
    merged = build_extraction_prompt(DEFAULT_EXTRACTION_INSTRUCTIONS, "只抽取工艺节点。")

    assert merged.startswith(DEFAULT_EXTRACTION_INSTRUCTIONS)
    assert merged.endswith("只抽取工艺节点。")


def test_prompt_merge_without_type_instruction_returns_default() -> None:
    assert build_extraction_prompt(DEFAULT_EXTRACTION_INSTRUCTIONS, "") == DEFAULT_EXTRACTION_INSTRUCTIONS
    assert build_extraction_prompt(DEFAULT_EXTRACTION_INSTRUCTIONS, None) == DEFAULT_EXTRACTION_INSTRUCTIONS


def test_backend_receives_merged_prompt_and_policy_version(tmp_path: Path) -> None:
    """写入路径带上拼好的指令，记忆记录里留下策略与指令版本。"""
    prompts_dir = tmp_path / "prompts" / "acme"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "process_preference.v1.md").write_text("按产线口径抽取。", encoding="utf-8")
    (tmp_path / "acme.json").write_text(
        json.dumps(
            _profile_payload(
                {
                    "process_preference": _type_payload(
                        extraction_prompt="prompts/acme/process_preference.v1.md"
                    )
                },
                version="7",
            )
        ),
        encoding="utf-8",
    )

    registry = load_registry(tmp_path, "acme")
    backend = _RecordingBackend()
    repository = InMemoryMemoryRepository()
    service = MemoryService(
        repository=repository,
        backend=backend,
        l3_write_mode="sync",
        registry_provider=lambda namespace: registry,
    )

    service.append(_append_request("r-1", metadata={"memory_type": "process_preference"}))

    assert backend.prompts
    prompt = backend.prompts[0] or ""
    assert prompt.startswith(DEFAULT_EXTRACTION_INSTRUCTIONS)
    assert "按产线口径抽取。" in prompt

    metadata = backend.metadatas[0]
    assert metadata["policy_version"] == "7"
    assert metadata["extraction_prompt_version"] == "v1"
    assert metadata["extractor"] == "mem0"

    indexed = repository.active_memories("user-1", "acme")
    assert indexed
    assert indexed[0].metadata["policy_version"] == "7"
    assert indexed[0].metadata["extraction_prompt_version"] == "v1"


def test_type_without_prompt_file_still_records_default_version(tmp_path: Path) -> None:
    """没有指令文件的类型也记版本，标成 default，便于解释来源。"""
    registry = MemoryTypeRegistry(
        namespace="acme",
        version="3",
        types={"profile": MemoryTypeSpec(name="profile", extractor="profile")},
    )
    backend = _RecordingBackend()
    service = MemoryService(
        repository=InMemoryMemoryRepository(),
        backend=backend,
        l3_write_mode="sync",
        registry_provider=lambda namespace: registry,
    )

    service.append(_append_request("r-2"))

    assert backend.metadatas[0]["extraction_prompt_version"] == "default"
    assert backend.metadatas[0]["policy_version"] == "3"


def test_invalid_profile_fails_visible_in_development(tmp_path: Path) -> None:
    """非法配置在任何环境都报错：静默回落默认等于跑另一套策略。"""
    (tmp_path / "acme.json").write_text(
        json.dumps(_profile_payload({"profile": {"extractor": "mem0"}})),
        encoding="utf-8",
    )
    settings = Settings(memory_type_profile_dir=str(tmp_path), environment="development")
    provider = build_registry_provider(settings)

    with pytest.raises(RuntimeError, match="could not be loaded"):
        provider("acme")


def test_missing_profile_still_falls_back_in_development(tmp_path: Path) -> None:
    """缺文件与非法配置是两回事：开发期缺文件仍回落默认。"""
    settings = Settings(memory_type_profile_dir=str(tmp_path), environment="development")
    provider = build_registry_provider(settings)

    registry = provider("acme")

    assert "profile" in registry.names()


def test_registry_rejects_unknown_top_level_field() -> None:
    with pytest.raises(ValueError, match="unknown fields"):
        registry_from_dict(
            {"types": {}, "unexpected": 1},
            namespace="acme",
        )
