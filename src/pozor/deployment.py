"""Verify operator-supplied local artifacts without downloading or launching them."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from pozor.config import Settings
from pozor.operations import OperationsError


class Artifact(BaseModel):
    filename: str = Field(min_length=1)
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


class Model(Artifact):
    repo: str = Field(min_length=1)
    revision: str = Field(pattern=r'^[0-9a-f]{40}$')
    alias: str = Field(min_length=1)


class Runtime(BaseModel):
    context_size: int
    slots: Literal[1]
    enable_thinking: Literal[False]
    max_output_tokens: int


class Deployment(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    schema_version: Literal[1]
    llama_cpp_build: str = Field(min_length=1)
    llama_cpp_artifact: Artifact
    model: Model
    runtime: Runtime


def check_artifacts(lock: Path, model: Path, server: Path, settings: Settings) -> None:
    deployment = Deployment.model_validate_json(lock.read_bytes())
    if (deployment.model.alias != settings.llm_model
            or deployment.runtime.context_size != settings.context_size
            or deployment.runtime.max_output_tokens != settings.max_output_tokens):
        raise OperationsError('deployment runtime does not match configuration')
    for path, artifact in ((model, deployment.model), (server, deployment.llama_cpp_artifact)):
        if path.name != artifact.filename:
            raise OperationsError('artifact filename mismatch')
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        if digest != artifact.sha256:
            raise OperationsError('artifact checksum mismatch')
