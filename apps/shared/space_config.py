"""Reads profile, preferences and prompts from the private configuration directory.

They are read each time a context is built and never copied into the database, so the
directory is the single source of truth (PLAN G15). Every file comes back with the SHA-256 of
its bytes, which the context builder records in the audit log.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import yaml
from pydantic import BaseModel, ValidationError

from core.context.builder import SystemPrompt
from core.context.documents import FileRef, Preferences, SpaceProfile
from core.domain.space import Space

MAX_CONFIG_FILE_BYTES = 256 * 1024


class ConfigFileError(Exception):
    """A configuration file is missing, outside the directory or invalid.

    Messages name the file and the field, never a value from the file: the configuration is
    private and error messages end up in logs."""


@dataclass(frozen=True)
class ConfigFile:
    ref: FileRef
    data: bytes


@dataclass(frozen=True)
class SpaceConfig:
    profile: SpaceProfile
    profile_source: FileRef
    preferences: Preferences
    preferences_source: FileRef


def read_config_file(config_dir: Path, relative: str) -> ConfigFile:
    """Read one file below `config_dir`. `relative` may not leave the directory, by `..`,
    by an absolute path or through a symbolic link."""
    path = PurePosixPath(relative)
    if not path.parts or path.is_absolute() or ".." in path.parts:
        raise ConfigFileError(f"{relative}: not a path inside the configuration directory")
    root = config_dir.resolve()
    target = root.joinpath(*path.parts).resolve()
    if not target.is_relative_to(root):
        raise ConfigFileError(f"{relative}: resolves outside the configuration directory")
    if not target.is_file():
        raise ConfigFileError(f"{relative}: no such file")
    if target.stat().st_size > MAX_CONFIG_FILE_BYTES:
        raise ConfigFileError(f"{relative}: larger than {MAX_CONFIG_FILE_BYTES} bytes")
    data = target.read_bytes()
    return ConfigFile(FileRef(path=path.as_posix(), sha256=hashlib.sha256(data).hexdigest()), data)


def _text(file: ConfigFile) -> str:
    try:
        return file.data.decode("utf-8")
    except UnicodeDecodeError:
        raise ConfigFileError(f"{file.ref.path}: not UTF-8 text") from None


def _parse[M: BaseModel](model: type[M], file: ConfigFile) -> M:
    try:
        document = yaml.safe_load(_text(file))
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        where = "" if mark is None else f" at line {mark.line + 1}"
        raise ConfigFileError(f"{file.ref.path}: not valid YAML{where}") from None
    if not isinstance(document, dict):
        raise ConfigFileError(f"{file.ref.path}: must be a YAML mapping")
    try:
        return model.model_validate(document)
    except ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in problem['loc']) or '(file)'}: {problem['msg']}"
            for problem in error.errors(include_input=False, include_url=False)
        )
        raise ConfigFileError(f"{file.ref.path}: {problems}") from None


def load_space_config(config_dir: Path, space: Space) -> SpaceConfig:
    """Load and validate `spaces/<space>/profile.yaml` and `preferences.yaml`."""
    profile = read_config_file(config_dir, f"spaces/{space.value}/profile.yaml")
    preferences = read_config_file(config_dir, f"spaces/{space.value}/preferences.yaml")
    return SpaceConfig(
        profile=_parse(SpaceProfile, profile),
        profile_source=profile.ref,
        preferences=_parse(Preferences, preferences),
        preferences_source=preferences.ref,
    )


def load_system_prompt(config_dir: Path, relative: str) -> SystemPrompt:
    """Load an agent's system prompt (`AgentSpec.system_prompt_path`)."""
    file = read_config_file(config_dir, relative)
    text = _text(file)
    if not text.strip():
        raise ConfigFileError(f"{file.ref.path}: empty prompt")
    return SystemPrompt(text=text, source=file.ref)
