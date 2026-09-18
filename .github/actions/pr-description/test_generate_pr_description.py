import unittest

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


if __name__ == "__main__":
    unittest.main()