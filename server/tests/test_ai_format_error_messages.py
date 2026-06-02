import json

from server.app.modules.articles.ai_format import (
    AIFormatConfigurationError,
    _describe_ai_format_error,
)


def test_ai_format_error_message_for_missing_key():
    message = _describe_ai_format_error(
        AIFormatConfigurationError("AI 排版失败：未配置 API Key，请设置 GEO_AI_FORMAT_API_KEY。")
    )
    assert "GEO_AI_FORMAT_API_KEY" in message


def test_ai_format_error_message_for_insufficient_balance():
    message = _describe_ai_format_error(RuntimeError("402 Payment Required: Insufficient Balance"))
    assert "余额不足" in message


def test_ai_format_error_message_for_invalid_model():
    message = _describe_ai_format_error(RuntimeError("404 model deepseek/foo does not exist"))
    assert "GEO_AI_FORMAT_MODEL" in message


def test_ai_format_error_message_for_invalid_json():
    try:
        json.loads("not json")
    except json.JSONDecodeError as exc:
        message = _describe_ai_format_error(exc)
    assert "返回格式异常" in message
