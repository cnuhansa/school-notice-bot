"""Building the Gemini client for either the free tier or a test project.

Two credential paths, deliberately kept apart:

  production   an AI Studio API key in GEMINI_API_KEY. This is the only
               credential that carries the free tier.
  testing      a GCP service-account JSON in CUK_VERTEX_CREDENTIALS, which
               authenticates to Vertex AI on that project. Vertex is BILLED —
               there is no free tier on this path. It exists so experiments
               do not eat the production key's 20 requests/day.

Selection is explicit: the service account is used only when its path is set,
so a stray environment variable cannot silently move production onto a billed
project.
"""

import json
import os


class CredentialError(RuntimeError):
    pass


def _vertex_client(path: str):
    from google import genai
    from google.oauth2 import service_account

    if not os.path.exists(path):
        raise CredentialError(f"서비스 계정 파일 없음: {path}")

    with open(path, encoding="utf-8") as fh:
        info = json.load(fh)
    if info.get("type") != "service_account":
        raise CredentialError(
            f"{path} 는 서비스 계정 JSON이 아닙니다 (type={info.get('type')})")

    credentials = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/cloud-platform"])

    return genai.Client(
        vertexai=True,
        project=os.environ.get("CUK_VERTEX_PROJECT") or info["project_id"],
        location=os.environ.get("CUK_VERTEX_LOCATION", "global"),
        credentials=credentials,
    )


def _studio_client(api_key: str = None):
    from google import genai

    key = (api_key or os.environ.get("GEMINI_API_KEY")
           or os.environ.get("GOOGLE_API_KEY"))
    if not key:
        raise CredentialError(
            "GEMINI_API_KEY 미설정 — aistudio.google.com 에서 발급하세요")
    return genai.Client(api_key=key)


def make_client(api_key: str = None):
    """Return a client, preferring the billed test project when configured."""
    vertex_path = os.environ.get("CUK_VERTEX_CREDENTIALS")
    if vertex_path and not api_key:
        return _vertex_client(vertex_path)
    return _studio_client(api_key)


def describe() -> str:
    """One line naming which credential is in play, for run logs.

    Worth printing on every run: confusing the billed test project for the
    free production key is a mistake that only shows up on an invoice.
    """
    vertex_path = os.environ.get("CUK_VERTEX_CREDENTIALS")
    if vertex_path:
        project = os.environ.get("CUK_VERTEX_PROJECT")
        if not project and os.path.exists(vertex_path):
            with open(vertex_path, encoding="utf-8") as fh:
                project = json.load(fh).get("project_id")
        return f"Vertex AI · project={project} · 과금됨(테스트용)"
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        return "AI Studio API key · 무료 한도(운영용)"
    return "자격증명 없음"
