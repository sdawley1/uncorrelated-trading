import json
import logging

from uncorrelated_trading.logging_setup import add_redaction, configure_logging


def test_configure_logging_sets_level(caplog):
    configure_logging("DEBUG")
    assert logging.getLogger("uncorrelated_trading").level == logging.DEBUG


def test_redaction_filter_masks_secret(capsys):
    configure_logging("INFO")
    add_redaction(["SKSECRET123"])
    logging.getLogger("uncorrelated_trading.x").info("key is %s", "SKSECRET123")
    err = capsys.readouterr().err
    assert "SKSECRET123" not in err
    assert "key is ***" in err


def test_redaction_masks_secret_in_message_text_and_survives_reconfigure(capsys):
    add_redaction(["", "PKTOKEN999"])
    configure_logging("INFO")
    logging.getLogger("uncorrelated_trading.y").warning("url?apikey=PKTOKEN999&x=1")
    err = capsys.readouterr().err
    assert "PKTOKEN999" not in err
    assert "apikey=***&x=1" in err


def test_json_format_emits_one_object_per_line(capsys):
    configure_logging("INFO", json_format=True)
    logging.getLogger("uncorrelated_trading.z").info("hello %d", 42)
    line = capsys.readouterr().err.strip()
    payload = json.loads(line)
    assert payload["message"] == "hello 42"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "uncorrelated_trading.z"


def test_configure_logging_does_not_stack_handlers():
    configure_logging("INFO")
    configure_logging("INFO")
    assert len(logging.getLogger("uncorrelated_trading").handlers) == 1


def test_redaction_covers_formatted_exceptions(capsys):
    add_redaction(["SKEXCSECRET42"])
    for json_format in (False, True):
        configure_logging("INFO", json_format=json_format)
        try:
            raise RuntimeError("GET https://api.example.com/?apikey=SKEXCSECRET42 failed")
        except RuntimeError:
            logging.getLogger("uncorrelated_trading.exc").exception("request failed")
        err = capsys.readouterr().err
        assert "SKEXCSECRET42" not in err
        assert "apikey=***" in err
        assert "RuntimeError" in err
