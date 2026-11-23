"""PLAN G15: profile, preferences and prompts are read from the private configuration
directory, validated, and returned with the hash of the file they came from."""

import hashlib
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from apps.shared.space_config import (
    MAX_CONFIG_FILE_BYTES,
    ConfigFileError,
    load_space_config,
    load_system_prompt,
    read_config_file,
)
from core.context.documents import Preferences, SpaceProfile
from core.domain.space import Space

EXAMPLE = Path(__file__).resolve().parents[2] / "config.example"
PROFILE = "name: Alex Example\nlanguages: [en, es]\ntimezone: Europe/Madrid\n"
PREFERENCES = "style:\n  reply_tone: formal\nrules:\n  - Be brief.\n"


def _config(tmp_path: Path, profile: str = PROFILE, preferences: str = PREFERENCES) -> Path:
    folder = tmp_path / "spaces" / "personal"
    folder.mkdir(parents=True)
    (folder / "profile.yaml").write_text(profile)
    (folder / "preferences.yaml").write_text(preferences)
    return tmp_path


@pytest.mark.parametrize("space", list(Space))
def test_the_example_configuration_loads_for_every_space(space: Space) -> None:
    config = load_space_config(EXAMPLE, space)
    assert config.profile.name == "Alex Example"
    assert config.profile.facts
    assert config.preferences.rules
    assert config.profile_source.path == f"spaces/{space.value}/profile.yaml"
    assert config.preferences_source.path == f"spaces/{space.value}/preferences.yaml"
    for ref in (config.profile_source, config.preferences_source):
        assert ref.sha256 == hashlib.sha256((EXAMPLE / ref.path).read_bytes()).hexdigest()


def test_the_file_hash_follows_the_file(tmp_path: Path) -> None:
    root = _config(tmp_path)
    before = load_space_config(root, Space.PERSONAL)
    (root / "spaces/personal/profile.yaml").write_text(PROFILE + "role: Founder\n")
    after = load_space_config(root, Space.PERSONAL)
    assert after.profile.role == "Founder"
    assert after.profile_source.sha256 != before.profile_source.sha256
    assert after.preferences_source == before.preferences_source
    assert after.preferences == Preferences(style={"reply_tone": "formal"}, rules=("Be brief.",))


@pytest.mark.parametrize(
    ("profile", "problem"),
    [
        ("name: Alex\nlanguages: [en]\ntimezone: Mars/Olympus\n", "timezone"),
        ("name: Alex\nlanguages: []\ntimezone: UTC\n", "languages"),
        ("name: Alex\nlanguages: [en, en]\ntimezone: UTC\n", "languages must not repeat"),
        ("name: Alex\nlanguages: [English]\ntimezone: UTC\n", "languages.0"),
        ("languages: [en]\ntimezone: UTC\n", "name: Field required"),
        (PROFILE + "nickname: Al\n", "nickname: Extra inputs are not permitted"),
        (PROFILE + 'facts:\n  - "two\\nlines"\n', "facts.0"),
        (PROFILE + "facts:\n  - ' padded '\n", "facts.0"),
        ("- a list\n", "must be a YAML mapping"),
        ("name: [unclosed\n", "not valid YAML at line"),
        ("", "must be a YAML mapping"),
    ],
)
def test_invalid_profiles_are_rejected_without_echoing_values(
    tmp_path: Path, profile: str, problem: str
) -> None:
    marker = "ZZ-PRIVATE-MARKER-ZZ"
    root = _config(tmp_path, profile=profile.replace("Alex", marker))
    with pytest.raises(ConfigFileError) as error:
        load_space_config(root, Space.PERSONAL)
    assert problem in str(error.value)
    assert "spaces/personal/profile.yaml" in str(error.value)
    assert marker not in str(error.value)
    # The parser's own error, which quotes the input, must not travel with ours into a log.
    assert error.value.__cause__ is None
    assert error.value.__context__ is None or error.value.__suppress_context__


@pytest.mark.parametrize(
    ("profile", "preferences"),
    [
        ("name: Alex\nlanguages: [en]\ntimezone: MARKER\n", PREFERENCES),
        ("name: Alex\nlanguages: [MARKER]\ntimezone: UTC\n", PREFERENCES),
        ('name: "MARKER\\nsecond line"\nlanguages: [en]\ntimezone: UTC\n', PREFERENCES),
        ("name: [MARKER]\nlanguages: [en]\ntimezone: UTC\n", PREFERENCES),
        ("name: Alex\nlanguages: [en]\ntimezone: UTC\nfacts: MARKER\n", PREFERENCES),
        (PROFILE, "style:\n  reply_tone: ' MARKER '\n"),
        (PROFILE, "rules: MARKER\n"),
        (PROFILE, "rules:\n  - [MARKER]\n"),
        ("name: 'MARKER\n", PREFERENCES),
    ],
)
def test_error_messages_never_contain_the_offending_value(
    tmp_path: Path, profile: str, preferences: str
) -> None:
    marker = "ZZ-PRIVATE-MARKER-ZZ"
    root = _config(
        tmp_path,
        profile=profile.replace("MARKER", marker),
        preferences=preferences.replace("MARKER", marker),
    )
    with pytest.raises(ConfigFileError) as error:
        load_space_config(root, Space.PERSONAL)
    assert marker not in str(error.value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None or error.value.__suppress_context__


@pytest.mark.parametrize(
    "preferences",
    [
        "style:\n  Reply Tone: formal\n",
        "style:\n  reply_tone: ''\n",
        "rules: be brief\n",
        "rules:\n  - ''\n",
        "tone: formal\n",
    ],
)
def test_invalid_preferences_are_rejected(tmp_path: Path, preferences: str) -> None:
    with pytest.raises(ConfigFileError, match=r"preferences\.yaml"):
        load_space_config(_config(tmp_path, preferences=preferences), Space.PERSONAL)


def test_empty_preferences_are_allowed(tmp_path: Path) -> None:
    config = load_space_config(_config(tmp_path, preferences="{}\n"), Space.PERSONAL)
    assert config.preferences == Preferences()


def test_a_space_reads_only_its_own_folder(tmp_path: Path) -> None:
    root = _config(tmp_path)
    with pytest.raises(ConfigFileError, match=r"spaces/synaption/profile.yaml: no such file"):
        load_space_config(root, Space.SYNAPTION)


def test_prompts_are_loaded_with_their_hash(tmp_path: Path) -> None:
    (tmp_path / "prompts").mkdir()
    text = "You are the email agent.\nSé breve.\n"
    (tmp_path / "prompts/email.md").write_bytes(text.encode())
    prompt = load_system_prompt(tmp_path, "prompts/email.md")
    assert prompt.text == text
    assert prompt.source.path == "prompts/email.md"
    assert prompt.source.sha256 == hashlib.sha256(text.encode()).hexdigest()

    (tmp_path / "prompts/blank.md").write_text(" \n")
    with pytest.raises(ConfigFileError, match="empty prompt"):
        load_system_prompt(tmp_path, "prompts/blank.md")
    (tmp_path / "prompts/binary.md").write_bytes(b"\xff\xfe\x00")
    with pytest.raises(ConfigFileError, match="not UTF-8"):
        load_system_prompt(tmp_path, "prompts/binary.md")


@pytest.mark.parametrize(
    "relative",
    ["", ".", "/etc/passwd", "../outside.txt", "prompts/../../outside.txt", "prompts/link.md"],
)
def test_files_outside_the_directory_cannot_be_read(tmp_path: Path, relative: str) -> None:
    config = tmp_path / "config"
    (config / "prompts").mkdir(parents=True)
    (tmp_path / "outside.txt").write_text("outside")
    (config / "prompts" / "link.md").symlink_to(tmp_path / "outside.txt")
    with pytest.raises(ConfigFileError):
        read_config_file(config, relative)


def test_links_inside_the_directory_and_size_limits(tmp_path: Path) -> None:
    (tmp_path / "real.md").write_text("inside")
    (tmp_path / "alias.md").symlink_to(tmp_path / "real.md")
    assert read_config_file(tmp_path, "alias.md").data == b"inside"
    with pytest.raises(ConfigFileError, match="no such file"):
        read_config_file(tmp_path, "missing.md")
    (tmp_path / "folder").mkdir()
    with pytest.raises(ConfigFileError, match="no such file"):
        read_config_file(tmp_path, "folder")
    (tmp_path / "big.md").write_bytes(b"x" * (MAX_CONFIG_FILE_BYTES + 1))
    with pytest.raises(ConfigFileError, match="larger than"):
        read_config_file(tmp_path, "big.md")


@pytest.mark.parametrize(
    "data",
    [
        {"name": "Alex", "languages": ["en"], "timezone": "Europe/Madrid", "facts": ["x"] * 51},
        {"name": "A" * 201, "languages": ["en"], "timezone": "UTC"},
        {"name": "Alex", "role": "", "languages": ["en"], "timezone": "UTC"},
        {"name": "Alex", "languages": ["en"], "timezone": ""},
        {"name": "Alex", "languages": ["en"], "timezone": "../etc/passwd"},
    ],
)
def test_profile_limits(data: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        SpaceProfile.model_validate(data)
