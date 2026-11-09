"""The application half of the activation guard (src/privacy/activation.py).

Pure: the setting is set per test with pytest's monkeypatch. The database half is
tested in tests/test_privacy_schema.py.
"""

import pytest
from pydantic import ValidationError

from src.config import settings
from src.config.settings import Settings
from src.privacy import activation


def test_the_code_key_is_turned_and_the_operator_key_is_off_by_default():
    # P7: the code implements every layer; activation still waits for the operator.
    assert activation.IMPLEMENTED_ENFORCEMENT_VERSION >= activation.REQUIRED_ENFORCEMENT_VERSION
    assert Settings.model_fields["PRIVACY_ENFORCEMENT_VERSION"].default == 0


def test_the_shipped_defaults_refuse_activation(monkeypatch):
    monkeypatch.setattr(settings, "PRIVACY_ENFORCEMENT_VERSION", 0)
    assert not activation.activation_allowed()


def test_raising_the_setting_alone_enables_nothing(monkeypatch):
    monkeypatch.setattr(activation, "IMPLEMENTED_ENFORCEMENT_VERSION", 0)
    monkeypatch.setattr(settings, "PRIVACY_ENFORCEMENT_VERSION", 99)
    assert not activation.activation_allowed()
    with pytest.raises(activation.ActivationNotAllowedError):
        activation.assert_activation_allowed()


def test_code_that_implements_every_layer_still_needs_the_operator(monkeypatch):
    monkeypatch.setattr(activation, "IMPLEMENTED_ENFORCEMENT_VERSION",
                        activation.REQUIRED_ENFORCEMENT_VERSION)
    monkeypatch.setattr(settings, "PRIVACY_ENFORCEMENT_VERSION", 0)
    with pytest.raises(activation.ActivationNotAllowedError):
        activation.assert_activation_allowed()
    monkeypatch.setattr(settings, "PRIVACY_ENFORCEMENT_VERSION",
                        activation.REQUIRED_ENFORCEMENT_VERSION)
    activation.assert_activation_allowed()  # both keys turned: allowed


def test_a_negative_setting_is_refused_at_boot():
    with pytest.raises(ValidationError):
        Settings(PRIVACY_ENFORCEMENT_VERSION=-1)
