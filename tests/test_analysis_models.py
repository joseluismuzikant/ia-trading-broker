"""Tests for the model catalog.

The catalog is the only place that names a model, so these tests pin what the
rest of the application relies on: the configured models with their labels and
endpoints, the offline simulator, a default that exists, and a strict enough
parser that a typo stops the run instead of asking a model that does not exist.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.domain.analysis_models import (
    DEFAULT_CONFIG_PATH,
    ModelsConfigError,
    get_models,
    load_models_config,
)

EXPECTED = {
    "gpt_6_1_sol": ("GPT-6.1 Sol", "openai", "OPENAI_API_KEY"),
    "gemini_3_8_flash": ("Gemini 3.8 Flash", "google", "GEMINI_API_KEY"),
    "mimo_v2_6_pro": ("MiMo V2.6 Pro", "xiaomi", "MIMO_API_KEY"),
    "deepseek_r1": ("DeepSeek R1", "deepseek", "DEEPSEEK_API_KEY"),
    "grok_4": ("Grok 4", "xai", "XAI_API_KEY"),
    "claude_opus_5_5": ("Claude Opus 5.5", "anthropic", "ANTHROPIC_API_KEY"),
    "fake_simulator": ("Fake simulator (smoke test)", "simulator", ""),
}


# --- The shipped catalog --------------------------------------------------


def test_the_shipped_catalog_holds_the_expected_models() -> None:
    config = load_models_config()

    assert config.keys == tuple(EXPECTED)
    for key, (label, provider, api_key_env) in EXPECTED.items():
        entry = config.find(key)
        assert entry is not None
        assert entry.label == label
        assert entry.provider == provider
        assert entry.api_key_env == api_key_env
        assert entry.model
        if key == "fake_simulator":
            assert entry.kind == "fake"
            assert entry.base_url == ""
        else:
            assert entry.kind == "openai"
            assert entry.base_url.startswith("https://")


def test_the_default_model_is_in_the_catalog() -> None:
    config = load_models_config()

    assert config.default().key == config.default_key
    assert config.default().key == "gpt_6_1_sol"


def test_the_choices_are_what_the_form_shows() -> None:
    choices = load_models_config().choices()

    assert choices == [(key, label) for key, (label, _, _) in EXPECTED.items()]


def test_a_model_is_found_by_key_in_any_case() -> None:
    config = load_models_config()

    assert config.find("GPT_6_1_SOL").label == "GPT-6.1 Sol"
    assert config.find(" gemini_3_8_flash ").label == "Gemini 3.8 Flash"
    assert config.find("nope") is None


def test_the_catalog_is_parsed_once_and_reused() -> None:
    assert get_models() is get_models()
    assert get_models(str(DEFAULT_CONFIG_PATH)) is get_models(str(DEFAULT_CONFIG_PATH))


# --- Validation -----------------------------------------------------------


def write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "models.toml"
    path.write_text(body, encoding="utf-8")
    return path


GOOD = """
[models.one]
label = "One"
model = "one-v1"
provider = "acme"
base_url = "https://api.acme.test/v1"
api_key_env = "ACME_API_KEY"

[models.two]
label = "Two"
model = "two-v1"
provider = "beta"
base_url = "https://api.beta.test/v1"
api_key_env = "BETA_API_KEY"

[defaults]
model = "one"
"""


def test_a_well_formed_file_loads(tmp_path: Path) -> None:
    config = load_models_config(write_config(tmp_path, GOOD))

    assert config.keys == ("one", "two")
    assert config.default().model == "one-v1"
    assert config.find("two").base_url == "https://api.beta.test/v1"


def test_a_missing_file_is_a_configuration_error(tmp_path: Path) -> None:
    with pytest.raises(ModelsConfigError, match="not found"):
        load_models_config(tmp_path / "nope.toml")


def test_invalid_toml_is_a_configuration_error(tmp_path: Path) -> None:
    with pytest.raises(ModelsConfigError, match="not valid TOML"):
        load_models_config(write_config(tmp_path, "[unclosed"))


def test_an_empty_catalog_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ModelsConfigError, match=r"\[models\]"):
        load_models_config(write_config(tmp_path, "[defaults]\nmodel = 'one'\n"))


@pytest.mark.parametrize("field", ["label", "model", "provider", "base_url", "api_key_env"])
def test_a_missing_field_is_rejected(tmp_path: Path, field: str) -> None:
    body = GOOD.replace(f"{field} = ", f"{field}_removed = ")
    with pytest.raises(ModelsConfigError, match=field):
        load_models_config(write_config(tmp_path, body))


def test_an_empty_value_is_rejected(tmp_path: Path) -> None:
    body = GOOD.replace('label = "One"', 'label = ""')
    with pytest.raises(ModelsConfigError, match="empty label"):
        load_models_config(write_config(tmp_path, body))


FAKE = """
[models.sim]
label = "Sim"
model = "sim-v1"
provider = "simulator"
kind = "fake"

[defaults]
model = "sim"
"""


def test_a_fake_entry_needs_no_endpoint_and_no_key(tmp_path: Path) -> None:
    config = load_models_config(write_config(tmp_path, FAKE))
    entry = config.find("sim")

    assert entry.kind == "fake"
    assert entry.base_url == ""
    assert entry.api_key_env == ""


def test_a_fake_entry_that_names_an_endpoint_is_rejected(tmp_path: Path) -> None:
    body = FAKE.replace(
        'kind = "fake"',
        'kind = "fake"\nbase_url = "https://api.acme.test/v1"\napi_key_env = "ACME_API_KEY"',
    )
    with pytest.raises(ModelsConfigError, match="fake"):
        load_models_config(write_config(tmp_path, body))


def test_an_unknown_kind_is_rejected(tmp_path: Path) -> None:
    body = GOOD.replace('provider = "acme"', 'provider = "acme"\nkind = "quantum"')
    with pytest.raises(ModelsConfigError, match="kind"):
        load_models_config(write_config(tmp_path, body))


def test_a_non_https_endpoint_is_rejected(tmp_path: Path) -> None:
    body = GOOD.replace("https://api.acme.test/v1", "http://api.acme.test/v1")
    with pytest.raises(ModelsConfigError, match="https"):
        load_models_config(write_config(tmp_path, body))


def test_a_key_name_that_is_not_an_environment_variable_is_rejected(
    tmp_path: Path,
) -> None:
    body = GOOD.replace("ACME_API_KEY", "not a key name")
    with pytest.raises(ModelsConfigError, match="api_key_env"):
        load_models_config(write_config(tmp_path, body))


def test_an_unknown_default_is_rejected(tmp_path: Path) -> None:
    body = GOOD.replace('model = "one"', 'model = "three"')
    with pytest.raises(ModelsConfigError, match="three"):
        load_models_config(write_config(tmp_path, body))


def test_a_missing_defaults_section_is_rejected(tmp_path: Path) -> None:
    body = GOOD.split("[defaults]")[0]
    with pytest.raises(ModelsConfigError, match="defaults"):
        load_models_config(write_config(tmp_path, body))


def test_a_per_model_timeout_is_read_and_defaults_to_none(tmp_path: Path) -> None:
    body = GOOD.replace(
        'api_key_env = "BETA_API_KEY"', 'api_key_env = "BETA_API_KEY"\ntimeout_seconds = 120'
    )
    config = load_models_config(write_config(tmp_path, body))

    assert config.find("one").timeout_seconds is None
    assert config.find("two").timeout_seconds == 120.0


@pytest.mark.parametrize("value", ["0", "-5", "601", '"fast"', "true"])
def test_a_bad_timeout_is_rejected(tmp_path: Path, value: str) -> None:
    body = GOOD.replace(
        'api_key_env = "ACME_API_KEY"', f'api_key_env = "ACME_API_KEY"\ntimeout_seconds = {value}'
    )
    with pytest.raises(ModelsConfigError, match="timeout_seconds"):
        load_models_config(write_config(tmp_path, body))


def test_the_response_format_defaults_to_a_strict_schema_and_deepseek_uses_json_mode() -> None:
    config = load_models_config()

    assert config.find("gpt_6_1_sol").response_format == "json_schema"
    assert config.find("deepseek_r1").response_format == "json_object"


def test_an_unknown_response_format_is_rejected(tmp_path: Path) -> None:
    body = GOOD.replace(
        'api_key_env = "ACME_API_KEY"', 'api_key_env = "ACME_API_KEY"\nresponse_format = "xml"'
    )
    with pytest.raises(ModelsConfigError, match="response_format"):
        load_models_config(write_config(tmp_path, body))
