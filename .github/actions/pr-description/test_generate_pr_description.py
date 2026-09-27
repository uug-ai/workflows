import os
import unittest
from io import BytesIO
from urllib.error import HTTPError, URLError
from unittest.mock import patch

import generate_pr_description as generator


def config(**overrides):
    values = {
        "github_api_url": "https://api.github.com",
        "github_repository": "uug-ai/example",
        "github_token": "github-token",
        "pull_request_number": 42,
        "pull_request_url": "",
        "overwrite_description": True,
        "azure_openai_api_key": "azure-key",
        "azure_openai_endpoint": "https://example.openai.azure.com",
        "azure_openai_version": "2024-02-15-preview",
        "azure_openai_deployment": "gpt-4o deployment",
    }
    values.update(overrides)
    return generator.Config(**values)


class GeneratePullRequestDescriptionTests(unittest.TestCase):
    class Response:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def read(self):
            return self.body

    def test_config_does_not_require_api_version_for_v1_endpoint(self):
        environment = {
            "GITHUB_API_URL": "https://api.github.com",
            "GITHUB_REPOSITORY": "uug-ai/example",
            "INPUT_AZURE_OPENAI_API_KEY": "azure-key",
            "INPUT_AZURE_OPENAI_DEPLOYMENT": "model-router",
            "INPUT_AZURE_OPENAI_ENDPOINT": "https://example.openai.azure.com/openai/v1",
            "INPUT_GITHUB_TOKEN": "github-token",
            "INPUT_PULL_REQUEST_NUMBER": "42",
        }

        with patch.dict(os.environ, environment, clear=True):
            loaded = generator.Config.from_environment()

        self.assertEqual(loaded.azure_openai_version, "")

    def test_config_accepts_deprecated_openai_model_input(self):
        environment = {
            "GITHUB_API_URL": "https://api.github.com",
            "GITHUB_REPOSITORY": "uug-ai/example",
            "INPUT_AZURE_OPENAI_API_KEY": "azure-key",
            "INPUT_AZURE_OPENAI_ENDPOINT": "https://example.openai.azure.com",
            "INPUT_AZURE_OPENAI_VERSION": "2024-02-15-preview",
            "INPUT_GITHUB_TOKEN": "github-token",
            "INPUT_OPENAI_MODEL": "legacy-deployment",
            "INPUT_PULL_REQUEST_NUMBER": "42",
        }

        with patch.dict(os.environ, environment, clear=True):
            loaded = generator.Config.from_environment()

        self.assertEqual(loaded.azure_openai_deployment, "legacy-deployment")

    def test_build_prompt_includes_text_patches_and_ignores_binary_files(self):
        prompt = generator.build_prompt(
            "Handle reconnects",
            [
                {
                    "filename": "database/client.go",
                    "status": "modified",
                    "patch": "+retryConnection()",
                },
                {"filename": "testdata/image.png", "status": "modified"},
            ],
        )

        self.assertIn("Pull request title: Handle reconnects", prompt)
        self.assertIn("database/client.go (modified)", prompt)
        self.assertIn("+retryConnection()", prompt)
        self.assertNotIn("image.png", prompt)

    def test_request_json_retries_rate_limit_using_retry_after(self):
        rate_limit_error = HTTPError(
            "https://example.com",
            429,
            "Too Many Requests",
            {"Retry-After": "7"},
            BytesIO(b'{"error":"rate limited"}'),
        )
        response = self.Response(b'{"result":"ok"}')

        with (
            patch.object(
                generator,
                "urlopen",
                side_effect=[rate_limit_error, response],
            ) as urlopen,
            patch.object(generator.time, "sleep") as sleep,
        ):
            result = generator.request_json("https://example.com")

        self.assertEqual(result, {"result": "ok"})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(7)

    def test_request_json_retries_network_errors_with_exponential_backoff(self):
        response = self.Response(b'{"result":"ok"}')

        with (
            patch.object(
                generator,
                "urlopen",
                side_effect=[
                    URLError("connection reset"),
                    TimeoutError("timed out"),
                    response,
                ],
            ) as urlopen,
            patch.object(generator.time, "sleep") as sleep,
        ):
            result = generator.request_json("https://example.com")

        self.assertEqual(result, {"result": "ok"})
        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])

    def test_request_json_does_not_retry_non_transient_http_errors(self):
        bad_request = HTTPError(
            "https://example.com",
            400,
            "Bad Request",
            {},
            BytesIO(b'{"error":"invalid request"}'),
        )

        with (
            patch.object(generator, "urlopen", side_effect=bad_request) as urlopen,
            patch.object(generator.time, "sleep") as sleep,
            self.assertRaises(generator.RequestError),
        ):
            generator.request_json("https://example.com")

        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()

    def test_format_description_adds_live_environment(self):
        body = generator.format_description(
            "## Description\n\nReconnects after transient database failures.",
            "https://pr42.example.com",
        )

        self.assertEqual(
            body,
            "## Live Environment\n\n"
            "Access the pull request environment [here](https://pr42.example.com).\n\n"
            "## Description\n\nReconnects after transient database failures.",
        )

    def test_run_skips_an_existing_description_when_overwrite_is_disabled(self):
        calls = []

        def request(url, **kwargs):
            calls.append((url, kwargs))
            return {"title": "Existing PR", "body": "Already documented"}

        updated = generator.run(config(overwrite_description=False), request)

        self.assertFalse(updated)
        self.assertEqual(len(calls), 1)

    def test_run_generates_and_updates_description(self):
        calls = []

        def request(url, **kwargs):
            calls.append((url, kwargs))
            method = kwargs.get("method", "GET")
            if method == "GET" and url.endswith("/pulls/42"):
                return {"title": "Reconnect database client", "body": ""}
            if method == "GET" and "/pulls/42/files?" in url:
                return [
                    {
                        "filename": "client.go",
                        "status": "modified",
                        "patch": "+reconnect()",
                    }
                ]
            if method == "POST":
                return {
                    "choices": [
                        {"message": {"content": "Handles transient disconnects."}}
                    ]
                }
            if method == "PATCH":
                return {"body": kwargs["payload"]["body"]}
            self.fail(f"Unexpected request: {method} {url}")

        updated = generator.run(config(), request)

        self.assertTrue(updated)
        azure_url, azure_request = next(
            call for call in calls if call[1].get("method") == "POST"
        )
        self.assertIn("gpt-4o%20deployment/chat/completions", azure_url)
        self.assertIn("api-version=2024-02-15-preview", azure_url)
        self.assertIn("+reconnect()", azure_request["payload"]["messages"][1]["content"])

        _, update_request = next(
            call for call in calls if call[1].get("method") == "PATCH"
        )
        self.assertEqual(
            update_request["payload"]["body"],
            "## Description\n\nHandles transient disconnects.",
        )

    def test_v1_endpoint_puts_deployment_in_request_body(self):
        calls = []

        def request(url, **kwargs):
            calls.append((url, kwargs))
            return {
                "choices": [
                    {"message": {"content": "Handles transient disconnects."}}
                ]
            }

        v1_config = config(
            azure_openai_endpoint="https://aihubproduction.openai.azure.com/openai/v1",
            azure_openai_version="",
            azure_openai_deployment="model-router",
        )
        generator.generate_description(v1_config, "Changes", request)

        url, request_options = calls[0]
        self.assertEqual(
            url,
            "https://aihubproduction.openai.azure.com/openai/v1/chat/completions",
        )
        self.assertEqual(request_options["payload"]["model"], "model-router")
        self.assertNotIn("api-version", url)

    def test_empty_completion_is_retried(self):
        calls = []
        responses = [
            {
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"content": None},
                    }
                ]
            },
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": "Handles transient disconnects."},
                    }
                ]
            },
        ]

        def request(url, **kwargs):
            calls.append((url, kwargs["payload"].copy()))
            return responses.pop(0)

        description = generator.generate_description(config(), "Changes", request)

        self.assertEqual(description, "Handles transient disconnects.")
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            calls[0][1]["max_completion_tokens"],
            generator.INITIAL_COMPLETION_TOKENS,
        )
        self.assertEqual(
            calls[1][1]["max_completion_tokens"],
            generator.MAX_COMPLETION_TOKENS,
        )

    def test_content_filtered_completion_is_not_retried(self):
        calls = []

        def request(url, **kwargs):
            calls.append((url, kwargs))
            return {
                "choices": [
                    {
                        "finish_reason": "content_filter",
                        "message": {"content": None},
                    }
                ]
            }

        with self.assertRaisesRegex(
            RuntimeError,
            "filtered the generated pull request description",
        ):
            generator.generate_description(config(), "Changes", request)

        self.assertEqual(len(calls), 1)

    def test_repeated_empty_completions_report_attempts_and_finish_reason(self):
        calls = []

        def request(url, **kwargs):
            calls.append((url, kwargs))
            return {
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"content": ""},
                    }
                ]
            }

        with self.assertRaisesRegex(
            RuntimeError,
            "empty description after 3 attempts \\(finish reason: length\\)",
        ):
            generator.generate_description(config(), "Changes", request)

        self.assertEqual(len(calls), generator.MAX_COMPLETION_ATTEMPTS)


if __name__ == "__main__":
    unittest.main()