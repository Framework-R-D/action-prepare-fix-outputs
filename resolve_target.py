"""Keep a fix branch destination separate from its caller-approved source SHA."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

CommitLookup = Callable[[str, str], str]


class NoRedirect(HTTPRedirectHandler):
    """Prevent metadata redirects from forwarding an authorization header."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """Keep requests on the runner's configured API endpoint."""
        return None


def github_commit_sha(repository: str, ref: str, environment: Mapping[str, str]) -> str:
    """Resolve one branch through a bounded, authenticated GET request."""
    api = environment.get("GITHUB_API_URL", "https://api.github.com")
    parsed = urlsplit(api)
    token = environment.get("GITHUB_TOKEN", "")
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not token
        or "\r" in token
        or "\n" in token
    ):
        raise ValueError("A trusted HTTPS GitHub API endpoint and token are required")
    request = Request(
        f"{api.rstrip('/')}/repos/{repository}/git/ref/{quote(f'heads/{ref}', safe='/')}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        method="GET",
    )
    try:
        with build_opener(NoRedirect()).open(request, timeout=30) as response:
            body = response.read(8 * 1024 * 1024 + 1)
        if len(body) > 8 * 1024 * 1024:
            raise ValueError("Metadata response is too large")
        return validate_sha(json.loads(body)["object"]["sha"])
    except (URLError, OSError, ValueError, KeyError, TypeError) as error:
        raise ValueError("GitHub commit metadata lookup failed") from error


def validate_sha(value: str) -> str:
    """Require a complete, nonzero source SHA."""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value) or value == "0" * 40:
        raise ValueError("Expected a full immutable source SHA")
    return value


def validate_repository(value: str) -> str:
    """Reject repository values that could change the request route or outputs."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", value or ""):
        raise ValueError("Invalid repository")
    if value.split("/")[1] in {".", ".."}:
        raise ValueError("Invalid repository")
    return value


def validate_branch(value: str) -> str:
    """Require an unambiguous branch name rather than a SHA, tag, or refspec."""
    if (
        not value
        or value.startswith(("refs/", "-"))
        or re.fullmatch(r"[0-9a-f]{40}", value)
        or re.search(r"[\x00-\x20\x7f~^:?*\[\\]", value)
        or ".." in value
        or "@{" in value
        or value.endswith(".")
        or any(
            not part or part.startswith(".") or part.endswith(".lock") for part in value.split("/")
        )
    ):
        raise ValueError("Expected an unambiguous branch push destination")
    return value


def checkout_path(value: str, workspace: str) -> str:
    """Keep every existing checkout ancestor inside the workspace."""
    if (
        not value
        or Path(value).is_absolute()
        or re.search(r"[\x00-\x1f\x7f\\]", value)
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError("Expected a workspace-relative checkout without traversal")
    root = Path(workspace).resolve(strict=True)
    current = root
    for part in value.split("/"):
        current /= part
        try:
            current.lstat()
        except FileNotFoundError:
            break
        current = current.resolve(strict=True)
        if current == root or not current.is_relative_to(root):
            raise ValueError("Checkout escapes workspace")
    return value


def resolve_target(
    inputs: Mapping[str, str], payload: Mapping[str, Any], lookup: CommitLookup
) -> dict[str, str]:
    """Resolve a coherent fix target without authorizing execution or widening approval."""
    original_repo = validate_repository(inputs["GITHUB_REPOSITORY"])
    repository = inputs.get("INPUT_REPO") or original_repo
    ref = inputs.get("INPUT_REF", "")
    sha = inputs.get("INPUT_SHA", "")
    event = inputs["GITHUB_EVENT_NAME"]
    if event == "issue_comment":
        # Metadata lookup cannot turn an authenticated comment into source approval.
        validate_sha(sha)
        if not isinstance(payload["issue"].get("pull_request"), Mapping):
            raise ValueError("Comment is not on a PR")
        if inputs.get("INPUT_REPO") and repository != inputs.get("GET_PR_REPO"):
            raise ValueError("PR repository override mismatch")
        if ref and ref != inputs.get("GET_PR_REF"):
            raise ValueError("PR branch override mismatch")
        if sha != inputs.get("GET_PR_SHA"):
            raise ValueError("Approved PR snapshot has moved")
        repository = inputs.get("GET_PR_REPO", "")
        ref = inputs.get("GET_PR_REF", "")
    elif event == "pull_request":
        head = payload["pull_request"]["head"]
        if not inputs.get("INPUT_REPO"):
            repository = head["repo"]["full_name"]
        if not ref:
            ref = head["ref"]
        if repository == head["repo"]["full_name"] and not inputs.get("INPUT_REF"):
            if sha and sha != head["sha"]:
                raise ValueError("Source SHA differs from PR event snapshot")
            sha = sha or head["sha"]
    elif event == "workflow_dispatch":
        ref = (
            ref
            or payload.get("inputs", {}).get("ref")
            or inputs["GITHUB_REF"].removeprefix("refs/heads/")
        )
    else:
        ref = ref or inputs["GITHUB_REF"].removeprefix("refs/heads/")
        if not inputs.get("INPUT_REPO") and not inputs.get("INPUT_REF"):
            if sha and sha != inputs["GITHUB_SHA"]:
                raise ValueError("Source SHA differs from event snapshot")
            sha = sha or inputs["GITHUB_SHA"]
    if event != "issue_comment" and (event != "pull_request" or inputs.get("INPUT_REF")):
        ref = ref.removeprefix("refs/heads/")
    validate_repository(repository)
    validate_branch(ref)
    if sha:
        validate_sha(sha)
    if not sha or (
        event != "issue_comment"
        and (inputs.get("INPUT_REF") or inputs.get("INPUT_REPO") or event == "workflow_dispatch")
    ):
        if not sha and repository != original_repo:
            raise ValueError("Cross-repository overrides require an approved SHA")
        resolved = lookup(repository, ref)
        if sha and sha != resolved:
            raise ValueError("Explicit snapshot does not match branch")
        sha = resolved
    validate_sha(sha)
    checkout = checkout_path(
        inputs.get("INPUT_CHECKOUT_PATH") or f"{repository.split('/')[1]}-src",
        inputs["GITHUB_WORKSPACE"],
    )
    return {"ref": ref, "repo": repository, "sha": sha, "checkout_path": checkout}


def write_outputs(output_path: Path, outputs: Mapping[str, str]) -> None:
    """Validate all output values before opening the runner output file."""
    for name, value in outputs.items():
        if (
            not re.fullmatch(r"[a-z][a-z0-9_-]*", name)
            or not isinstance(value, str)
            or re.search(r"[\x00-\x1f\x7f]", value)
        ):
            raise ValueError("Invalid action output")
    with output_path.open("a", encoding="utf-8") as output:
        output.write("".join(f"{name}={value}\n" for name, value in outputs.items()))


def main() -> None:
    """Read runner data and publish only the validated immutable target."""
    environment = dict(os.environ)
    payload = json.loads(Path(environment["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    outputs = resolve_target(
        environment, payload, lambda repo, ref: github_commit_sha(repo, ref, environment)
    )
    write_outputs(Path(environment["GITHUB_OUTPUT"]), outputs)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError):
        raise SystemExit(
            "Fix target resolution failed closed; verify snapshot inputs and metadata"
        ) from None
