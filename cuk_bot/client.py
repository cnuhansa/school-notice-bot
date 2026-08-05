"""Building Gemini clients, in the order they should be tried.

Two credential paths with different economics:

  Vertex AI     a GCP service account. Billed, but not subject to the free
                tier's 20 requests/day/model, so it carries the real load.
  AI Studio     an API key. Free tier, and the fallback when Vertex cannot
                serve — missing credentials, expired billing, revoked key.

Order is client-chosen (2026-08-05): Vertex first, AI Studio behind it. The
chain matters because the failure it guards against is silent — if Vertex
stops working and nothing takes over, the bot forwards everything unjudged
and the filtering quietly disappears.

Every run prints which credential is live. Confusing the billed project for
the free key is a mistake that otherwise surfaces on an invoice.
"""

import json
import os

VERTEX = "vertex"
STUDIO = "studio"


class CredentialError(RuntimeError):
    pass


def _service_account_info():
    """Service account from an inline JSON secret, or a path on disk.

    CI has no filesystem to pre-seed, so the JSON is passed whole through a
    secret; a path stays supported for local use.
    """
    raw = os.environ.get("CUK_VERTEX_CREDENTIALS_JSON")
    if raw and raw.strip():
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise CredentialError(f"CUK_VERTEX_CREDENTIALS_JSON 파싱 실패: {exc}")

    path = os.environ.get("CUK_VERTEX_CREDENTIALS")
    if not path:
        return None
    if not os.path.exists(path):
        raise CredentialError(f"서비스 계정 파일 없음: {path}")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _vertex_client():
    from google import genai
    from google.oauth2 import service_account

    info = _service_account_info()
    if not info:
        raise CredentialError("Vertex 자격증명 미설정")
    if info.get("type") != "service_account":
        raise CredentialError(
            f"서비스 계정 JSON이 아닙니다 (type={info.get('type')})")

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


BUILDERS = ((VERTEX, _vertex_client), (STUDIO, _studio_client))


def available_credentials() -> list:
    """Configured credentials, in the order they should be tried."""
    out = []
    if os.environ.get("CUK_VERTEX_CREDENTIALS_JSON") or \
            os.environ.get("CUK_VERTEX_CREDENTIALS"):
        out.append(VERTEX)
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        out.append(STUDIO)
    return out


def build(kind: str):
    for name, builder in BUILDERS:
        if name == kind:
            return builder()
    raise CredentialError(f"알 수 없는 자격증명: {kind}")


def make_client(api_key: str = None):
    """First credential that builds. Kept for callers that want just one."""
    if api_key:
        return _studio_client(api_key)

    errors = []
    for kind in available_credentials():
        try:
            return build(kind)
        except Exception as exc:
            errors.append(f"{kind}: {str(exc)[:80]}")
    raise CredentialError(
        "사용 가능한 자격증명 없음 — " + (" / ".join(errors) or "아무것도 설정되지 않음"))


def label(kind: str) -> str:
    if kind == VERTEX:
        project = os.environ.get("CUK_VERTEX_PROJECT")
        if not project:
            try:
                info = _service_account_info() or {}
                project = info.get("project_id")
            except CredentialError:
                project = None
        return f"Vertex AI · project={project} · 과금됨"
    return "AI Studio API key · 무료 한도"


def describe() -> str:
    """One line naming the credential order, for run logs."""
    kinds = available_credentials()
    if not kinds:
        return "자격증명 없음"
    return " → ".join(label(k) for k in kinds)
