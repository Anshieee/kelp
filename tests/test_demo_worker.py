import importlib
import json
import pathlib
import sys


def test_repository_test_subprocess_drops_worker_provider_and_github_secrets(monkeypatch):
    monkeypatch.setenv("DEMO_API_BASE", "https://demo.example.test")
    monkeypatch.setenv("DEMO_WORKER_TOKEN", "worker-test-secret")
    monkeypatch.setenv("DEMO_READ_TOKEN", "read-test-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "github-test-secret")
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/repo")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "github-auth-test-secret")
    monkeypatch.setenv("GIT_ASKPASS", "/tmp/git-askpass")
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -i /tmp/test-key")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/ssh-agent.sock")
    monkeypatch.setenv("OPENROUTER_API_KEY", "provider-test-secret")
    monkeypatch.setenv("KEEP_TEST_SETTING", "retained")
    worker = importlib.import_module("scripts.kelp_demo_worker")
    child_env = worker.child_test_environment()
    assert "DEMO_WORKER_TOKEN" not in child_env
    assert "DEMO_READ_TOKEN" not in child_env
    assert "GITHUB_TOKEN" not in child_env
    assert "GIT_CONFIG_VALUE_0" not in child_env
    assert "GIT_ASKPASS" not in child_env
    assert "GIT_SSH_COMMAND" not in child_env
    assert "SSH_AUTH_SOCK" not in child_env
    assert "OPENROUTER_API_KEY" not in child_env
    assert child_env["KEEP_TEST_SETTING"] == "retained"

    probe = (
        "import json, os; print(json.dumps({key: key in os.environ for key in "
        "['DEMO_WORKER_TOKEN','DEMO_READ_TOKEN','GITHUB_TOKEN','GIT_CONFIG_VALUE_0','GIT_ASKPASS',"
        "'GIT_SSH_COMMAND','SSH_AUTH_SOCK','OPENROUTER_API_KEY']}))"
    )
    result = worker.run([sys.executable, "-c", probe], pathlib.Path.cwd(), isolated_test_env=True)
    assert json.loads(result.stdout) == {
        "DEMO_WORKER_TOKEN": False,
        "DEMO_READ_TOKEN": False,
        "GITHUB_TOKEN": False,
        "GIT_CONFIG_VALUE_0": False,
        "GIT_ASKPASS": False,
        "GIT_SSH_COMMAND": False,
        "SSH_AUTH_SOCK": False,
        "OPENROUTER_API_KEY": False,
    }


def test_model_repository_reader_has_context_and_rejects_secret_paths(monkeypatch):
    monkeypatch.setenv("DEMO_API_BASE", "https://demo.example.test")
    monkeypatch.setenv("DEMO_WORKER_TOKEN", "worker-test-secret")
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/repo")
    worker = importlib.import_module("scripts.kelp_demo_worker")
    root = pathlib.Path.cwd()
    context = worker.repository_context(root)
    assert "README.md" in context
    assert "Repository file index" in context
    assert worker.read_repository_file(root, "README.md") == (root / "README.md").read_text(encoding="utf-8")
    assert worker.read_repository_file(root, ".env.example").startswith("Error:")
    assert worker.read_repository_file(root, "../README.md").startswith("Error:")
