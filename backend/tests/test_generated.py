import importlib.util
import io
import os
import tempfile
import threading
import time
import urllib.parse
import uuid
import zipfile

# Isolate the test run's SQLite database from any developer/local crownai.db.
# Must happen before `db` (imported transitively by `main`) reads the env var.
os.environ.setdefault("CROWNAI_DB_PATH", os.path.join(tempfile.mkdtemp(prefix="crownai-tests-"), "crownai.db"))

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from main import app
import db as db_module
import oauth_providers
import routers.auth as auth_module
import routers.pricing as pricing_module
import routers.projects as projects_module
import security
from db import get_conn, init_db, set_user_tier, upsert_user
from generation import STAGE_LABELS, STAGE_ORDER
from integrations.stripe_adapter import StripeNotConfigured
from routers.consent import EU_COUNTRIES
from routers.pricing import PLANS

ALL_STAGES = [s.value for s in STAGE_ORDER]
FREE_LIMIT = 5
MARKETING_BUDGET_S = 2.5
GENERATION_PROGRESS_BUDGET_S = 3.0
BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def client():
    init_db()
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _deterministic_env(monkeypatch):
    # Pin the free-tier cap so results don't depend on the runner's env.
    monkeypatch.setattr(projects_module, "FREE_TIER_DAILY_GENERATION_LIMIT", FREE_LIMIT)


@pytest.fixture
def force_mock(monkeypatch):
    """Dev mock sign-in on, regardless of whether real OAuth creds are set."""
    monkeypatch.setattr(oauth_providers, "use_mock", lambda provider: True)


@pytest.fixture
def force_real_oauth(monkeypatch):
    """Real OAuth flow with fake client credentials (no network)."""
    monkeypatch.setattr(oauth_providers, "use_mock", lambda provider: False)
    monkeypatch.setattr(oauth_providers, "GOOGLE_CLIENT_ID", "g-client-id")
    monkeypatch.setattr(oauth_providers, "GOOGLE_CLIENT_SECRET", "g-secret")
    monkeypatch.setattr(oauth_providers, "MICROSOFT_CLIENT_ID", "ms-client-id")
    monkeypatch.setattr(oauth_providers, "MICROSOFT_CLIENT_SECRET", "ms-secret")


class FakeStripe:
    """Stands in for the live Stripe gateway: records sessions it creates and
    lets tests emit signed 'checkout.session.completed' webhooks."""

    def __init__(self):
        self.sessions = []
        self.payment_status = {}  # session id -> Stripe payment_status
        self.retrieved = []

    def create_checkout_session(self, **kwargs):
        sid = f"cs_live_{uuid.uuid4().hex}"
        self.sessions.append({"id": sid, **kwargs})
        self.payment_status[sid] = "unpaid"
        return {"id": sid, "url": f"https://checkout.stripe.com/c/pay/{sid}"}

    def retrieve_checkout_session(self, session_id):
        self.retrieved.append(session_id)
        return {"id": session_id, "payment_status": self.payment_status.get(session_id, "unpaid")}

    @staticmethod
    def verify_webhook_event(payload, signature):
        import json

        if signature != "valid-signature":
            raise ValueError("bad signature")
        return json.loads(payload)


@pytest.fixture
def fake_stripe(monkeypatch):
    fake = FakeStripe()
    monkeypatch.setattr(pricing_module, "create_checkout_session", fake.create_checkout_session)
    monkeypatch.setattr(pricing_module, "verify_webhook_event", fake.verify_webhook_event)
    monkeypatch.setattr(pricing_module, "retrieve_checkout_session", fake.retrieve_checkout_session)
    return fake


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _unique_email():
    return f"user-{uuid.uuid4().hex}@example.com"


def _session_for(email=None, name="Test User", provider="google"):
    email = email or _unique_email()
    with get_conn() as conn:
        user = upsert_user(conn, email=email, name=name, provider=provider, provider_sub=email)
    token = security.create_access_token(user["id"], user["email"])
    return {"Authorization": f"Bearer {token}"}, dict(user)


def _mock_sign_in(client, provider="google", name="Test User", email=None):
    """Drives the mock OAuth flow the way the UI does (requires force_mock)."""
    email = email or _unique_email()
    login = client.get(f"/auth/{provider}/login", follow_redirects=False)
    assert login.status_code == 302
    parsed = urllib.parse.urlparse(login.headers["location"])
    assert parsed.path == f"/auth/{provider}/mock"
    state = urllib.parse.parse_qs(parsed.query)["state"][0]

    screen = client.get(f"/auth/{provider}/mock", params={"state": state})
    assert screen.status_code == 200
    assert "text/html" in screen.headers["content-type"]

    submit = client.post(
        f"/auth/{provider}/mock",
        data={"state": state, "name": name, "email": email},
        follow_redirects=False,
    )
    assert submit.status_code == 302
    location = submit.headers["location"]
    assert "/crown-ai/callback" in location
    token = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)["token"][0]
    return {"Authorization": f"Bearer {token}"}, email


def _set_tier(headers, client, tier):
    me = client.get("/auth/me", headers=headers).json()
    with get_conn() as conn:
        set_user_tier(conn, me["id"], tier)


def _create_project(client, headers, name="Inventory App", requirements="Track stock levels for a warehouse."):
    resp = client.post("/projects", json={"name": name, "requirements": requirements}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _generate_all(client, headers, project_id, stages=None):
    out = []
    for stage in stages or ALL_STAGES:
        resp = client.post(f"/projects/{project_id}/generate/{stage}", headers=headers)
        assert resp.status_code == 200, (stage, resp.text)
        out.append(resp.json())
    return out


def _lead(client, plan="mid", email=None, **overrides):
    body = {
        "name": "Priya Raman",
        "email": email or _unique_email(),
        "company": "Acme Pvt Ltd",
        "phone": "+91 98765 43210",
        "plan": plan,
    }
    body.update(overrides)
    resp = client.post("/pricing/leads", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _complete_webhook(client, session_id, signature="valid-signature"):
    import json

    event = {"type": "checkout.session.completed", "data": {"object": {"id": session_id}}}
    return client.post(
        "/pricing/webhook",
        content=json.dumps(event).encode(),
        headers={"stripe-signature": signature, "content-type": "application/json"},
    )


# ===========================================================================
# Human-requested coverage (Testing defects pass):
#   1. US-4 create form validation + double submit -- the UI must get BOTH the
#      name and requirements errors from one submit, a rejected submit must
#      leave nothing behind, and a double/rapid submit must never corrupt the
#      workspace or 5xx.
#   2. US-12 case study detail -- static frontend route; the API must never
#      shadow /case-studies or /case-studies/<slug>.
#   3. UI layer skipped because the dev server "failed to start in time"
#      ("Another next dev server is already running"). Backend side: the app
#      must boot fast with no optional config, tolerate a second instance on
#      the same DB, survive repeated start/stop, never bind a port at import,
#      and accept the frontend from whatever localhost port it ends up on.
# ===========================================================================


def _validation_locs(resp):
    return {tuple(e["loc"]) for e in resp.json()["detail"]}


def test_us_4_create_form_both_fields_blank_reports_both_errors_at_once(client):
    headers, _ = _session_for()
    resp = client.post("/projects", json={"name": "", "requirements": ""}, headers=headers)
    assert resp.status_code == 422
    locs = _validation_locs(resp)
    assert ("body", "name") in locs
    assert ("body", "requirements") in locs


def test_us_4_create_form_missing_both_fields_reports_both_errors(client):
    headers, _ = _session_for()
    resp = client.post("/projects", json={}, headers=headers)
    assert resp.status_code == 422
    assert {("body", "name"), ("body", "requirements")} <= _validation_locs(resp)


def test_us_4_create_form_only_requirements_missing_flags_only_requirements(client):
    # Mirrors the failing UI case: name filled, requirements left empty.
    headers, _ = _session_for()
    resp = client.post("/projects", json={"name": "Inventory App", "requirements": ""}, headers=headers)
    assert resp.status_code == 422
    locs = _validation_locs(resp)
    assert ("body", "requirements") in locs
    assert ("body", "name") not in locs


def test_us_4_create_form_whitespace_name_has_human_readable_message(client):
    headers, _ = _session_for()
    resp = client.post("/projects", json={"name": "   ", "requirements": "Build a CRM."}, headers=headers)
    assert resp.status_code == 422
    errors = {tuple(e["loc"]): e["msg"] for e in resp.json()["detail"]}
    assert set(errors) == {("body", "name")}
    assert "blank" in errors[("body", "name")]


def test_us_4_create_form_rejected_submit_creates_nothing(client):
    headers, _ = _session_for()
    for body in ({}, {"name": "", "requirements": ""}, {"name": "x", "requirements": "  "}):
        assert client.post("/projects", json=body, headers=headers).status_code == 422
    assert client.get("/projects", headers=headers).json() == []


def test_us_4_create_form_rejected_then_corrected_submit_succeeds(client):
    # The user fixes the errors shown after the first click and resubmits.
    headers, _ = _session_for()
    assert client.post("/projects", json={"name": "", "requirements": ""}, headers=headers).status_code == 422
    project = _create_project(client, headers, name="Fixed", requirements="Now with requirements.")
    assert [p["id"] for p in client.get("/projects", headers=headers).json()] == [project["id"]]


@pytest.mark.parametrize(
    "payload",
    [b"", b"null", b"[]", b'"just a string"', b"{not json"],
)
def test_us_4_create_form_malformed_body_is_422(client, payload):
    headers, _ = _session_for()
    resp = client.post("/projects", content=payload, headers={**headers, "content-type": "application/json"})
    assert resp.status_code == 422


@pytest.mark.parametrize("body", [{"name": 123, "requirements": "x"}, {"name": "x", "requirements": ["a"]}])
def test_us_4_create_form_wrong_types_are_422(client, body):
    headers, _ = _session_for()
    assert client.post("/projects", json=body, headers=headers).status_code == 422


def test_us_4_create_form_unknown_extra_fields_ignored(client):
    headers, _ = _session_for()
    resp = client.post(
        "/projects",
        json={"name": "Extra", "requirements": "Fine.", "status": "nfr", "user_id": "someone-else"},
        headers=headers,
    )
    assert resp.status_code == 201
    assert resp.json()["status"] == "requirements"
    assert client.get(f"/projects/{resp.json()['id']}", headers=headers).status_code == 200


def test_us_4_create_form_unicode_name_round_trips(client):
    headers, _ = _session_for()
    project = _create_project(client, headers, name="சென்னை கடை 🚀", requirements="தமிழ் requirements.")
    assert project["name"] == "சென்னை கடை 🚀"
    assert client.get(f"/projects/{project['id']}", headers=headers).json()["name"] == "சென்னை கடை 🚀"


def test_us_4_create_form_unauthenticated_invalid_submit_is_401_not_422(client):
    # An expired session must send the user to sign in, not show field errors.
    assert client.post("/projects", json={"name": "", "requirements": ""}).status_code == 401


def test_us_4_create_form_validation_error_readable_cross_origin(client):
    # The browser UI only sees 422 details if CORS headers are on the error.
    headers, _ = _session_for()
    resp = client.post(
        "/projects",
        json={"name": "", "requirements": ""},
        headers={**headers, "Origin": "http://localhost:3020"},
    )
    assert resp.status_code == 422
    assert resp.headers.get("access-control-allow-origin") == "http://localhost:3020"


def test_us_4_create_form_double_submit_sequential_is_consistent(client):
    headers, _ = _session_for()
    body = {"name": "Double", "requirements": "Clicked twice."}
    responses = [client.post("/projects", json=body, headers=headers) for _ in range(2)]
    assert all(r.status_code < 500 for r in responses)
    created = {r.json()["id"] for r in responses if r.status_code == 201}
    assert created, "at least one submit must create the project"
    listed = client.get("/projects", headers=headers).json()
    assert {p["id"] for p in listed} == created
    for pid in created:
        assert client.get(f"/projects/{pid}", headers=headers).json()["status"] == "requirements"


def test_us_4_create_form_concurrent_submits_never_5xx(client):
    headers, _ = _session_for()
    body = {"name": "Rapid", "requirements": "Many clicks at once."}
    results = []
    lock = threading.Lock()

    def submit():
        r = client.post("/projects", json=body, headers=headers)
        with lock:
            results.append(r)

    threads = [threading.Thread(target=submit) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 6
    assert all(r.status_code < 500 for r in results), [r.status_code for r in results]
    created = [r.json()["id"] for r in results if r.status_code == 201]
    assert len(created) == len(set(created))
    assert {p["id"] for p in client.get("/projects", headers=headers).json()} == set(created)


def test_us_4_create_form_new_project_is_immediately_generatable(client):
    # After a successful submit the workspace moves straight to stage 1.
    headers, _ = _session_for()
    project = _create_project(client, headers)
    resp = client.post(f"/projects/{project['id']}/generate/requirements", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["stage"] == "requirements"


@pytest.mark.parametrize(
    "path",
    [
        "/case-studies",
        "/case-studies/regional-nbfc-chennai",
        "/case-studies/regional-nbfc-chennai/",
        "/case-studies/does-not-exist",
    ],
)
def test_us_12_case_study_detail_routes_not_shadowed_by_api(client, path):
    resp = client.get(path)
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/json")


def test_us_12_no_api_route_claims_case_studies_prefix():
    assert not [p for _, p in _route_table(app) if p.startswith("/case-studies")]


def test_us_1_ui_readiness_no_duplicate_route_registrations():
    # Routes are spliced onto app.router directly; doing it twice (e.g. a
    # re-import on restart) would silently register duplicates.
    pairs = []
    for r in app.routes:
        if isinstance(r, APIRoute):
            pairs.extend((m, r.path) for m in r.methods)
    dupes = {p for p in pairs if pairs.count(p) > 1}
    assert not dupes, sorted(dupes)


def test_us_1_ui_readiness_importing_main_never_starts_a_server():
    # A server started at import time would collide with the test runner's
    # own server ("already running") and hang the UI layer.
    with open(os.path.join(BACKEND_DIR, "main.py"), encoding="utf-8") as fh:
        source = fh.read()
    if "uvicorn.run(" in source:
        guard = source.index('if __name__ == "__main__"') if 'if __name__ == "__main__"' in source else -1
        assert guard != -1 and guard < source.index("uvicorn.run("), "uvicorn.run must be behind a __main__ guard"


def test_us_1_ui_readiness_startup_is_fast(fresh_db_path):
    start = time.perf_counter()
    with TestClient(app) as c:
        elapsed = time.perf_counter() - start
        assert c.get("/health").status_code == 200
    assert elapsed < 5.0, f"lifespan startup took {elapsed:.2f}s"


def test_us_1_ui_readiness_boots_without_optional_integrations(fresh_db_path, monkeypatch):
    for name in ("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "GOOGLE_MAPS_API_KEY", "FRONTEND_URL"):
        monkeypatch.delenv(name, raising=False)
    with TestClient(app) as c:
        assert c.get("/health").json() == {"status": "ok"}
        assert c.get("/pricing/plans").status_code == 200
        assert c.get("/consent/policy", params={"country": "IN"}).status_code == 200
        assert c.post("/contact", json={"name": "A", "email": "a@b.co", "message": "Hi"}).status_code == 201


def test_us_1_ui_readiness_second_instance_on_same_db_serves_alongside_first(fresh_db_path):
    # A stale server still running must not stop a new one from working.
    with TestClient(app) as first:
        headers, _ = _session_for()
        project = _create_project(first, headers)
        with TestClient(app) as second:
            assert second.get("/health").json() == {"status": "ok"}
            assert second.get(f"/projects/{project['id']}", headers=headers).status_code == 200
            other = _create_project(second, headers, name="From second")
        ids = {p["id"] for p in first.get("/projects", headers=headers).json()}
        assert ids == {project["id"], other["id"]}


def test_us_1_ui_readiness_repeated_start_stop_cycles_stay_healthy(fresh_db_path):
    headers = None
    for i in range(3):
        with TestClient(app) as c:
            assert c.get("/health").status_code == 200
            if headers is None:
                headers, _ = _session_for()
            _create_project(c, headers, name=f"Cycle {i}")
            assert len(c.get("/projects", headers=headers).json()) == i + 1


@pytest.mark.parametrize("origin", ["http://localhost:3020", "http://localhost:9603", "http://127.0.0.1:3020"])
def test_us_1_ui_readiness_cors_allows_any_dev_server_port(client, origin):
    # Next may fall back to a different port when one is taken.
    resp = client.options(
        "/projects",
        headers={"Origin": origin, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"},
    )
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == origin


def test_us_4_create_form_both_errors_carry_readable_messages(client):
    # The form shows one message per field; each 422 entry needs its own msg.
    headers, _ = _session_for()
    resp = client.post("/projects", json={"name": "  ", "requirements": "\n\t"}, headers=headers)
    assert resp.status_code == 422
    errors = {tuple(e["loc"]): e["msg"] for e in resp.json()["detail"]}
    assert set(errors) == {("body", "name"), ("body", "requirements")}
    assert all(isinstance(m, str) and m for m in errors.values())


def test_us_4_create_form_blank_name_and_oversized_requirements_both_flagged(client):
    headers, _ = _session_for()
    resp = client.post("/projects", json={"name": "", "requirements": "x" * 8001}, headers=headers)
    assert resp.status_code == 422
    assert {("body", "name"), ("body", "requirements")} <= _validation_locs(resp)


def test_us_4_create_form_whitespace_requirements_flags_only_requirements(client):
    headers, _ = _session_for()
    resp = client.post("/projects", json={"name": "Inventory App", "requirements": "   "}, headers=headers)
    assert resp.status_code == 422
    assert _validation_locs(resp) == {("body", "requirements")}


def test_us_4_create_form_rejected_submits_do_not_consume_generation_quota(client):
    headers, _ = _session_for()
    for _ in range(FREE_LIMIT + 2):
        assert client.post("/projects", json={"name": "", "requirements": ""}, headers=headers).status_code == 422
    project = _create_project(client, headers)
    _generate_all(client, headers, project["id"])  # full free allowance still available


def test_us_4_create_form_success_readable_cross_origin_from_dev_port(client):
    headers, _ = _session_for()
    resp = client.post(
        "/projects",
        json={"name": "CORS ok", "requirements": "Created from the UI."},
        headers={**headers, "Origin": "http://localhost:3020"},
    )
    assert resp.status_code == 201
    assert resp.headers.get("access-control-allow-origin") == "http://localhost:3020"
    assert any(p["id"] == resp.json()["id"] for p in client.get("/projects", headers=headers).json())


@pytest.mark.parametrize(
    "path",
    ["/case-studies/regional%20nbfc", "/case-studies/REGIONAL-NBFC-CHENNAI", "/case-studies/a/b"],
)
def test_us_12_case_study_odd_slugs_are_plain_json_404(client, path):
    resp = client.get(path)
    assert resp.status_code == 404
    assert resp.json()["detail"]


def test_us_12_case_study_paths_reject_writes_without_5xx(client):
    for method in ("post", "put", "delete"):
        resp = getattr(client, method)("/case-studies/regional-nbfc-chennai")
        assert resp.status_code in (404, 405)


def test_us_1_ui_readiness_two_instances_initialising_same_fresh_db_concurrently(fresh_db_path):
    # Two servers racing to boot on one DB file (the "already running" case)
    # must both succeed and leave one usable schema.
    errors = []

    def boot():
        try:
            init_db()
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=boot) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    with TestClient(app) as c:
        headers, _ = _session_for()
        _create_project(c, headers)
        assert len(c.get("/projects", headers=headers).json()) == 1


def test_us_1_ui_readiness_lifespan_shutdown_then_restart_serves_requests(fresh_db_path):
    # A stopped instance (e.g. the stale dev server being killed) must not
    # leave the DB locked for the replacement.
    with TestClient(app) as first:
        headers, _ = _session_for()
        _create_project(first, headers)
    with TestClient(app) as second:
        _create_project(second, headers, name="After restart")
        assert len(second.get("/projects", headers=headers).json()) == 2


# ===========================================================================
# Earlier human-requested coverage: "no UI test results recorded".
# The UI suite never produced a result. Root causes it guards against on the
# backend side: an app that boots without routes (wrong import dir), a
# readiness probe that fails, CORS blocking the browser's dev origin, and any
# break in the end-to-end journey the UI tests click through. These tests pin
# every contract the UI suite depends on so it can actually execute.
# ===========================================================================


EXPECTED_ROUTES = {
    ("GET", "/health"),
    ("GET", "/auth/{provider}/login"),
    ("GET", "/auth/{provider}/mock"),
    ("POST", "/auth/{provider}/mock"),
    ("GET", "/auth/{provider}/callback"),
    ("GET", "/auth/me"),
    ("DELETE", "/auth/me"),
    ("GET", "/projects"),
    ("POST", "/projects"),
    ("GET", "/projects/{project_id}"),
    ("DELETE", "/projects/{project_id}"),
    ("POST", "/projects/{project_id}/generate/{stage}"),
    ("GET", "/projects/{project_id}/artifacts"),
    ("POST", "/projects/{project_id}/download"),
    ("GET", "/pricing/plans"),
    ("POST", "/pricing/leads"),
    ("POST", "/pricing/checkout"),
    ("POST", "/pricing/webhook"),
    ("GET", "/pricing/checkout/{session_id}"),
    ("GET", "/pricing/payments/{payment_id}"),
    ("POST", "/contact"),
    ("GET", "/contact/map"),
    ("GET", "/consent/policy"),
}


def _route_table(application):
    table = set()
    for r in application.routes:
        if isinstance(r, APIRoute):
            for m in r.methods:
                table.add((m, r.path))
    return table


def test_us_1_ui_readiness_app_exposes_every_route_the_ui_calls():
    missing = EXPECTED_ROUTES - _route_table(app)
    assert not missing, f"UI suite would hit 404s for: {sorted(missing)}"


def test_us_1_ui_readiness_app_loads_identically_from_repo_root():
    # The UI runner may start `uvicorn backend.main:app` from the repo root;
    # that must not produce a half-imported app without routes.
    spec = importlib.util.spec_from_file_location("backend_main_alias", os.path.join(BACKEND_DIR, "main.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert EXPECTED_ROUTES <= _route_table(module.app)


def test_us_1_ui_readiness_health_probe_is_fast_and_ok(client):
    start = time.perf_counter()
    resp = client.get("/health")
    assert time.perf_counter() - start < MARKETING_BUDGET_S
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_us_1_ui_readiness_openapi_schema_served(client):
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    schema = resp.json()
    assert "/projects" in schema["paths"]
    assert "/pricing/leads" in schema["paths"]


@pytest.mark.parametrize(
    "origin",
    ["http://localhost:3000", "http://localhost:5173", "http://127.0.0.1:4173", "http://localhost"],
)
def test_us_1_ui_readiness_cors_preflight_allows_local_ui_origins(client, origin):
    resp = client.options(
        "/projects",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == origin
    assert resp.headers.get("access-control-allow-credentials") == "true"


def test_us_1_ui_readiness_cors_simple_request_echoes_origin(client):
    resp = client.get("/pricing/plans", headers={"Origin": "http://localhost:3000"})
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == "http://localhost:3000"


@pytest.mark.parametrize("origin", ["http://evil.example.com", "http://localhost.evil.com", "http://127.0.0.1.attacker.io"])
def test_us_1_ui_readiness_cors_rejects_foreign_origins(client, origin):
    resp = client.get("/health", headers={"Origin": origin})
    assert resp.headers.get("access-control-allow-origin") is None


def test_us_1_ui_readiness_errors_are_json_with_detail_for_ui_error_states(client):
    # The UI renders `detail` in its error banners; every failure class must have one.
    responses = [
        client.get("/auth/me"),  # 401
        client.post("/projects", json={}, headers=_session_for()[0]),  # 422
        client.get(f"/projects/{uuid.uuid4().hex}", headers=_session_for()[0]),  # 404
    ]
    for r in responses:
        assert r.headers["content-type"].startswith("application/json")
        assert "detail" in r.json()


def test_us_5_ui_journey_end_to_end_signin_generate_block_upgrade_download(client, force_mock, fake_stripe):
    """The exact click path the UI suite drives, start to finish."""
    headers, email = _mock_sign_in(client, "google", name="Journey User")
    assert client.get("/auth/me", headers=headers).json()["tier"] == "free"

    # Empty workspace state.
    assert client.get("/projects", headers=headers).json() == []

    project = _create_project(client, headers, name="Journey App")
    _generate_all(client, headers, project["id"])

    blocked = client.post(f"/projects/{project['id']}/download", headers=headers)
    assert blocked.status_code == 402
    assert blocked.json()["detail"]["upgrade_url"] == "/pricing"

    plans = client.get("/pricing/plans").json()
    assert any(p["id"] == "mid" for p in plans)

    lead = _lead(client, plan="mid", email=email)
    checkout = client.post("/pricing/checkout", json={"lead_id": lead["id"], "plan": "mid"}, headers=headers)
    assert checkout.status_code == 200, checkout.text
    assert checkout.json()["checkout_url"].startswith("https://")

    assert _complete_webhook(client, checkout.json()["session_id"]).status_code == 200
    assert client.get("/auth/me", headers=headers).json()["tier"] == "mid"

    download = client.post(f"/projects/{project['id']}/download", headers=headers)
    assert download.status_code == 200
    assert download.headers["content-type"] == "application/zip"


def test_us_7_ui_journey_microsoft_anonymous_lead_then_return_from_checkout(client, force_mock, fake_stripe):
    """Second UI path: pay before signing in, return to /pricing?session_id=..,
    then sign in with Microsoft and land in the workspace already upgraded."""
    email = _unique_email()
    lead = _lead(client, plan="large", email=email)
    checkout = client.post("/pricing/checkout", json={"lead_id": lead["id"], "plan": "large"})
    assert checkout.status_code == 200
    sid = checkout.json()["session_id"]

    fake_stripe.payment_status[sid] = "paid"
    confirmed = client.get(f"/pricing/checkout/{sid}")
    assert confirmed.status_code == 200
    assert confirmed.json() == {"status": "succeeded", "plan": "large"}

    headers, _ = _mock_sign_in(client, "microsoft", email=email)
    assert client.get("/auth/me", headers=headers).json()["tier"] == "large"
    project = _create_project(client, headers)
    _generate_all(client, headers, project["id"])
    assert client.post(f"/projects/{project['id']}/download", headers=headers).status_code == 200


def test_us_1_ui_readiness_dev_sign_in_works_out_of_the_box(client, monkeypatch):
    # The UI suite runs with no real OAuth apps registered. If login doesn't
    # land on the mock screen there, the suite can never get past sign-in.
    monkeypatch.delenv("OAUTH_DEV_MOCK", raising=False)
    for name in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "MICROSOFT_CLIENT_ID", "MICROSOFT_CLIENT_SECRET"):
        monkeypatch.setattr(oauth_providers, name, "")
    for provider in ("google", "microsoft"):
        resp = client.get(f"/auth/{provider}/login", follow_redirects=False)
        assert resp.status_code == 302
        assert urllib.parse.urlparse(resp.headers["location"]).path == f"/auth/{provider}/mock"
    headers, _ = _mock_sign_in(client, "google")
    assert client.get("/auth/me", headers=headers).status_code == 200


def test_us_1_ui_readiness_partial_credentials_still_use_mock(client, monkeypatch):
    monkeypatch.delenv("OAUTH_DEV_MOCK", raising=False)
    monkeypatch.setattr(oauth_providers, "GOOGLE_CLIENT_ID", "id-only")
    monkeypatch.setattr(oauth_providers, "GOOGLE_CLIENT_SECRET", "")
    resp = client.get("/auth/google/login", follow_redirects=False)
    assert urllib.parse.urlparse(resp.headers["location"]).path == "/auth/google/mock"


@pytest.mark.parametrize("flag", ["false", "0", "no", "OFF", " False "])
def test_us_1_ui_readiness_dev_mock_can_be_disabled(client, monkeypatch, flag):
    monkeypatch.setenv("OAUTH_DEV_MOCK", flag)
    monkeypatch.setattr(oauth_providers, "GOOGLE_CLIENT_ID", "")
    monkeypatch.setattr(oauth_providers, "GOOGLE_CLIENT_SECRET", "")
    resp = client.get("/auth/google/login", follow_redirects=False)
    assert resp.status_code == 302
    assert urllib.parse.urlparse(resp.headers["location"]).netloc == "accounts.google.com"
    assert client.post("/auth/google/mock", data={"state": "s", "name": "a", "email": "a@b.co"}).status_code == 404


@pytest.mark.parametrize("provider,label", [("google", "Google"), ("microsoft", "Microsoft")])
def test_us_1_ui_readiness_mock_screen_has_fields_the_ui_test_fills(client, force_mock, provider, label):
    login = client.get(f"/auth/{provider}/login", follow_redirects=False)
    state = urllib.parse.parse_qs(urllib.parse.urlparse(login.headers["location"]).query)["state"][0]
    html_page = client.get(f"/auth/{provider}/mock", params={"state": state}).text
    assert f'action="/auth/{provider}/mock"' in html_page
    assert 'method="post"' in html_page
    assert f'name="state" value="{state}"' in html_page
    assert 'id="name" name="name"' in html_page
    assert 'id="email" name="email" type="email"' in html_page
    assert 'type="submit"' in html_page
    assert f"Sign in with {label}" in html_page
    # Mobile-usable and same brand identity as the site.
    assert 'name="viewport"' in html_page
    assert "Crown AI" in html_page
    # Viewing the screen must not consume the state (user may reload it).
    assert client.get(f"/auth/{provider}/mock", params={"state": state}).status_code == 200


def test_us_1_ui_readiness_mock_screen_escapes_state(client, force_mock):
    evil = '"><script>alert(1)</script>'
    auth_module._PENDING_STATES[evil] = "google"
    try:
        page = client.get("/auth/google/mock", params={"state": evil}).text
        assert "<script>alert(1)</script>" not in page
        assert "&quot;&gt;&lt;script&gt;" in page
    finally:
        auth_module._PENDING_STATES.pop(evil, None)


def test_us_1_ui_readiness_callback_redirects_to_configured_frontend(client, force_mock):
    login = client.get("/auth/google/login", follow_redirects=False)
    state = urllib.parse.parse_qs(urllib.parse.urlparse(login.headers["location"]).query)["state"][0]
    email = _unique_email()
    resp = client.post("/auth/google/mock", data={"state": state, "name": "F", "email": email}, follow_redirects=False)
    location = resp.headers["location"]
    assert location.startswith(f"{auth_module.FRONTEND_URL}/crown-ai/callback?token=")
    token = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)["token"][0]
    claims = security.decode_access_token(token)
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).json()
    assert claims["sub"] == me["id"]
    assert claims["email"] == email
    assert claims["exp"] > time.time()


@pytest.mark.parametrize(
    "method,path",
    [("DELETE", "/projects/abc"), ("DELETE", "/auth/me"), ("GET", "/auth/me"), ("POST", "/pricing/checkout")],
)
def test_us_1_ui_readiness_cors_preflight_for_authenticated_calls(client, method, path):
    resp = client.options(
        path,
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == "http://localhost:3000"
    assert method in resp.headers.get("access-control-allow-methods", "")


def test_us_1_ui_readiness_cors_headers_present_on_error_responses(client):
    # Without CORS on 4xx, the browser hides the error and the UI test can't
    # assert on the error state.
    resp = client.get("/auth/me", headers={"Origin": "http://localhost:3000"})
    assert resp.status_code == 401
    assert resp.headers.get("access-control-allow-origin") == "http://localhost:3000"


def test_us_4_ui_readiness_workspace_status_advances_with_each_stage(client):
    # The UI's stage stepper reads project.status after every generate call.
    headers, _ = _session_for()
    project = _create_project(client, headers)
    for stage in ALL_STAGES:
        assert client.post(f"/projects/{project['id']}/generate/{stage}", headers=headers).status_code == 200
        assert client.get(f"/projects/{project['id']}", headers=headers).json()["status"] == stage


def test_us_4_ui_readiness_generation_is_deterministic_for_stable_assertions(client):
    headers, _ = _session_for()
    _set_tier(headers, client, "mid")
    a = _create_project(client, headers, name="Same", requirements="Same input.")
    b = _create_project(client, headers, name="Same", requirements="Same input.")
    out_a = [x["content"] for x in _generate_all(client, headers, a["id"])]
    out_b = [x["content"] for x in _generate_all(client, headers, b["id"])]
    assert out_a == out_b


def test_us_4_ui_readiness_regenerating_a_stage_returns_latest(client):
    headers, _ = _session_for()
    project = _create_project(client, headers)
    _generate_all(client, headers, project["id"], ALL_STAGES[:2])
    again = client.post(f"/projects/{project['id']}/generate/requirements", headers=headers)
    assert again.status_code == 200
    assert len(client.get(f"/projects/{project['id']}/artifacts", headers=headers).json()) == 3
    # The next stage is still reachable after a re-run.
    assert client.post(f"/projects/{project['id']}/generate/code", headers=headers).status_code == 200


def test_us_4_quota_reservation_is_atomic_under_concurrency(client):
    # Parallel UI tabs / scripted clients must never exceed the free cap.
    _, user = _session_for()
    day = "2099-01-01"
    results = []
    lock = threading.Lock()

    def worker():
        with get_conn() as conn:
            ok = db_module.reserve_generation_quota(conn, user["id"], day, FREE_LIMIT)
        with lock:
            results.append(ok)

    threads = [threading.Thread(target=worker) for _ in range(FREE_LIMIT * 3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == FREE_LIMIT
    with get_conn() as conn:
        assert db_module.get_usage(conn, user["id"], day) == FREE_LIMIT


def test_us_4_zero_limit_blocks_all_free_generation(client, monkeypatch):
    monkeypatch.setattr(projects_module, "FREE_TIER_DAILY_GENERATION_LIMIT", 0)
    headers, _ = _session_for()
    project = _create_project(client, headers)
    assert client.post(f"/projects/{project['id']}/generate/requirements", headers=headers).status_code == 429


@pytest.fixture
def fresh_db_path(monkeypatch):
    """Points the app at a brand-new, empty SQLite file -- what a UI test run
    gets on a clean CI machine."""
    path = os.path.join(tempfile.mkdtemp(prefix="crownai-ui-boot-"), "crownai.db")
    monkeypatch.setattr(db_module, "DB_PATH", path)
    return path


def test_us_1_ui_readiness_fresh_database_boots_via_lifespan(fresh_db_path, force_mock):
    # The UI suite starts the server against an empty DB; the lifespan hook
    # alone must create the schema, or every UI call 500s and no result is recorded.
    assert not os.path.exists(fresh_db_path)
    with TestClient(app) as fresh:
        assert os.path.exists(fresh_db_path)
        assert fresh.get("/health").json() == {"status": "ok"}
        headers, _ = _mock_sign_in(fresh, "google")
        assert fresh.get("/projects", headers=headers).json() == []
        project = _create_project(fresh, headers)
        assert fresh.post(f"/projects/{project['id']}/generate/requirements", headers=headers).status_code == 200
        assert fresh.post("/contact", json={"name": "A", "email": "a@b.co", "message": "Hi"}).status_code == 201
        assert fresh.get("/pricing/plans").status_code == 200


def test_us_1_ui_readiness_restart_on_existing_database_keeps_data(fresh_db_path):
    # Re-running the Testing phase restarts the server on the same DB file:
    # init_db must be idempotent and keep earlier sessions/projects intact.
    with TestClient(app) as first:
        headers, _ = _session_for()
        project = _create_project(first, headers)
    init_db()
    with TestClient(app) as second:
        assert second.get(f"/projects/{project['id']}", headers=headers).status_code == 200


def test_us_7_ui_readiness_legacy_payments_schema_migrated_for_anonymous_checkout(fresh_db_path, fake_stripe):
    # An older DB had payments.user_id NOT NULL; without the migration the
    # anonymous pay-before-sign-in UI path fails with an IntegrityError.
    import sqlite3

    conn = sqlite3.connect(fresh_db_path)
    conn.executescript(
        """
        CREATE TABLE users (id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
            provider TEXT NOT NULL, provider_sub TEXT NOT NULL, tier TEXT NOT NULL DEFAULT 'free',
            created_at TEXT NOT NULL);
        CREATE TABLE leads (id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL, company TEXT NOT NULL,
            phone TEXT NOT NULL, plan TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE payments (id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
            lead_id TEXT REFERENCES leads(id), plan TEXT NOT NULL, amount_cents INTEGER NOT NULL,
            currency TEXT NOT NULL, stripe_session_id TEXT, status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL);
        INSERT INTO users VALUES ('u1', 'old@example.com', 'Old', 'google', 'old', 'mid', '2025-01-01');
        INSERT INTO payments VALUES ('p1', 'u1', NULL, 'mid', 100, 'usd', 'cs_old', 'succeeded', '2025-01-01');
        """
    )
    conn.commit()
    conn.close()

    with TestClient(app) as migrated:
        with get_conn() as c:
            cols = {r["name"]: r for r in c.execute("PRAGMA table_info(payments)").fetchall()}
            assert cols["user_id"]["notnull"] == 0
            legacy = db_module.get_payment(c, "p1")
            assert legacy["user_id"] == "u1" and legacy["status"] == "succeeded"
        lead = _lead(migrated)
        resp = migrated.post("/pricing/checkout", json={"lead_id": lead["id"], "plan": "mid"})
        assert resp.status_code == 200, resp.text


def test_us_1_ui_readiness_unknown_route_and_wrong_method_are_json(client):
    # UI error states rely on JSON bodies even for routing failures.
    missing = client.get("/does-not-exist")
    assert missing.status_code == 404
    assert missing.json()["detail"]
    wrong = client.put("/projects")
    assert wrong.status_code == 405
    assert wrong.json()["detail"]


# ===========================================================================
# US-1: Sign in with Google or Microsoft
# ===========================================================================


@pytest.mark.parametrize("provider", ["google", "microsoft"])
def test_us_1_mock_sign_in_creates_session_tied_to_account(client, force_mock, provider):
    email = _unique_email()
    headers, _ = _mock_sign_in(client, provider, name="Asha", email=email)
    me = client.get("/auth/me", headers=headers)
    assert me.status_code == 200
    body = me.json()
    assert body["email"] == email
    assert body["name"] == "Asha"
    assert body["provider"] == provider
    assert body["tier"] == "free"
    assert set(body) == {"id", "email", "name", "provider", "tier"}


def test_us_1_repeat_sign_in_reuses_same_account(client, force_mock):
    email = _unique_email()
    h1, _ = _mock_sign_in(client, "google", email=email)
    h2, _ = _mock_sign_in(client, "google", email=email.upper())
    assert client.get("/auth/me", headers=h1).json()["id"] == client.get("/auth/me", headers=h2).json()["id"]


def test_us_1_mock_sign_in_redirects_to_crown_ai_workspace_callback(client, force_mock):
    login = client.get("/auth/microsoft/login", follow_redirects=False)
    state = urllib.parse.parse_qs(urllib.parse.urlparse(login.headers["location"]).query)["state"][0]
    resp = client.post(
        "/auth/microsoft/mock",
        data={"state": state, "name": "M", "email": _unique_email()},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    loc = urllib.parse.urlparse(resp.headers["location"])
    assert loc.path.endswith("/crown-ai/callback")
    token = urllib.parse.parse_qs(loc.query)["token"][0]
    assert security.decode_access_token(token)["sub"]


@pytest.mark.parametrize("provider", ["facebook", "github", "email"])
def test_us_1_only_google_and_microsoft_providers_allowed(client, provider):
    assert client.get(f"/auth/{provider}/login", follow_redirects=False).status_code == 422


def test_us_1_mock_state_is_single_use(client, force_mock):
    login = client.get("/auth/google/login", follow_redirects=False)
    state = urllib.parse.parse_qs(urllib.parse.urlparse(login.headers["location"]).query)["state"][0]
    data = {"state": state, "name": "X", "email": _unique_email()}
    assert client.post("/auth/google/mock", data=data, follow_redirects=False).status_code == 302
    replay = client.post("/auth/google/mock", data=data, follow_redirects=False)
    assert replay.status_code == 400


def test_us_1_mock_rejects_unknown_state(client, force_mock):
    assert client.get("/auth/google/mock", params={"state": "bogus"}).status_code == 400
    resp = client.post("/auth/google/mock", data={"state": "bogus", "name": "a", "email": "a@b.co"})
    assert resp.status_code == 400


def test_us_1_mock_state_cannot_cross_providers(client, force_mock):
    login = client.get("/auth/google/login", follow_redirects=False)
    state = urllib.parse.parse_qs(urllib.parse.urlparse(login.headers["location"]).query)["state"][0]
    resp = client.post("/auth/microsoft/mock", data={"state": state, "name": "a", "email": "a@b.co"})
    assert resp.status_code == 400


def test_us_1_mock_screen_requires_state_param(client, force_mock):
    assert client.get("/auth/google/mock").status_code == 422


@pytest.mark.parametrize("missing", ["state", "name", "email"])
def test_us_1_mock_submit_missing_field_is_422(client, force_mock, missing):
    data = {"state": "s", "name": "n", "email": "e@x.co"}
    data.pop(missing)
    assert client.post("/auth/google/mock", data=data).status_code == 422


def test_us_1_mock_unavailable_once_real_oauth_configured(client, force_real_oauth):
    assert client.get("/auth/google/mock", params={"state": "x"}).status_code == 404
    resp = client.post("/auth/google/mock", data={"state": "x", "name": "a", "email": "a@b.co"})
    assert resp.status_code == 404


def test_us_1_real_google_login_redirects_to_google(client, force_real_oauth):
    resp = client.get("/auth/google/login", follow_redirects=False)
    assert resp.status_code == 302
    url = urllib.parse.urlparse(resp.headers["location"])
    assert url.netloc == "accounts.google.com"
    q = urllib.parse.parse_qs(url.query)
    assert q["client_id"] == ["g-client-id"]
    assert q["response_type"] == ["code"]
    assert "email" in q["scope"][0]
    assert q["state"][0]


def test_us_1_real_microsoft_login_redirects_to_microsoft(client, force_real_oauth):
    resp = client.get("/auth/microsoft/login", follow_redirects=False)
    url = urllib.parse.urlparse(resp.headers["location"])
    assert url.netloc == "login.microsoftonline.com"
    assert urllib.parse.parse_qs(url.query)["client_id"] == ["ms-client-id"]


@pytest.mark.parametrize("provider", ["google", "microsoft"])
def test_us_1_real_callback_exchanges_code_and_issues_session(client, force_real_oauth, monkeypatch, provider):
    email = _unique_email()

    async def fake_exchange(p, code):
        assert p == provider and code == "auth-code"
        return {"email": email, "name": "Real User", "provider_sub": "sub-123"}

    monkeypatch.setattr(oauth_providers, "exchange_code_for_profile", fake_exchange)
    login = client.get(f"/auth/{provider}/login", follow_redirects=False)
    state = urllib.parse.parse_qs(urllib.parse.urlparse(login.headers["location"]).query)["state"][0]
    cb = client.get(f"/auth/{provider}/callback", params={"code": "auth-code", "state": state}, follow_redirects=False)
    assert cb.status_code == 302
    token = urllib.parse.parse_qs(urllib.parse.urlparse(cb.headers["location"]).query)["token"][0]
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).json()
    assert me["email"] == email and me["provider"] == provider


def test_us_1_callback_rejects_unknown_state_csrf(client, force_real_oauth):
    resp = client.get("/auth/google/callback", params={"code": "c", "state": "forged"})
    assert resp.status_code == 400


def test_us_1_callback_provider_error_is_502(client, force_real_oauth, monkeypatch):
    async def failing(p, code):
        raise RuntimeError("token exchange failed")

    monkeypatch.setattr(oauth_providers, "exchange_code_for_profile", failing)
    login = client.get("/auth/google/login", follow_redirects=False)
    state = urllib.parse.parse_qs(urllib.parse.urlparse(login.headers["location"]).query)["state"][0]
    resp = client.get("/auth/google/callback", params={"code": "c", "state": state})
    assert resp.status_code == 502


@pytest.mark.parametrize("params", [{"state": "s"}, {"code": "c"}, {}])
def test_us_1_callback_missing_params_is_422(client, params):
    assert client.get("/auth/google/callback", params=params).status_code == 422


def test_us_1_me_requires_token(client):
    resp = client.get("/auth/me")
    assert resp.status_code == 401
    assert resp.headers.get("www-authenticate") == "Bearer"


@pytest.mark.parametrize("auth", ["Bearer not-a-jwt", "Bearer ", "Basic abc"])
def test_us_1_me_rejects_bad_tokens(client, auth):
    assert client.get("/auth/me", headers={"Authorization": auth}).status_code == 401


def test_us_1_token_for_deleted_or_unknown_user_is_401(client):
    token = security.create_access_token(uuid.uuid4().hex, "ghost@example.com")
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_us_1_expired_or_tampered_token_is_401(client):
    import jwt

    _, user = _session_for()
    expired = jwt.encode({"sub": user["id"], "exp": int(time.time()) - 10}, security.JWT_SECRET_KEY, algorithm="HS256")
    forged = jwt.encode({"sub": user["id"], "exp": int(time.time()) + 999}, "wrong-secret", algorithm="HS256")
    for t in (expired, forged):
        assert client.get("/auth/me", headers={"Authorization": f"Bearer {t}"}).status_code == 401


# ===========================================================================
# US-2 / US-3: marketing site + unified branding (backend-facing parts)
# ===========================================================================


def test_us_2_api_metadata_reflects_crownwright_branding(client):
    schema = client.get("/openapi.json").json()
    assert "Crownwright Technologies Pvt Ltd" in schema["info"]["description"]
    assert "Ishanvi" not in schema["info"]["description"]
    assert "Crown AI" in schema["info"]["description"]


def test_us_2_pricing_page_data_loads_within_budget(client):
    start = time.perf_counter()
    resp = client.get("/pricing/plans")
    assert time.perf_counter() - start < MARKETING_BUDGET_S
    assert resp.status_code == 200
    ids = [p["id"] for p in resp.json()]
    assert ids == ["mid", "large", "global"]
    for p in resp.json():
        assert {"id", "name", "amount_cents", "currency", "description", "features"} <= set(p)
        assert isinstance(p["features"], list) and p["features"]


def test_us_7_pricing_plans_have_setup_fee_plus_monthly_and_annual_usd(client):
    plans = {p["id"]: p for p in client.get("/pricing/plans").json()}
    expected = {
        "mid": (50, "shared platform", 7500_00, 90000_00),
        "large": (250, "dedicated", 22500_00, 270000_00),
        "global": (1000, "HA + DR", 60000_00, 720000_00),
    }
    assert set(plans) == set(expected)
    for plan_id, (seats, hosting, monthly, annual) in expected.items():
        p = plans[plan_id]
        assert p["currency"] == "usd"
        assert (p["seats"], p["hosting"]) == (seats, hosting)
        assert p["setup_fee_cents"] == 5000_00
        assert p["monthly_cents"] == monthly
        assert p["annual_cents"] == annual
        assert p["amount_cents"] == 5000_00 + monthly


def test_us_2_contact_map_points_at_chennai_office(client, monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-key")
    resp = client.get("/contact/map")
    assert resp.status_code == 200
    url = urllib.parse.unquote_plus(resp.json()["map_image_url"])
    assert "Maraimalar Nagar, Chennai, Tamil Nadu, India" in url


def test_us_2_contact_map_unconfigured_is_clear_503(client, monkeypatch):
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    resp = client.get("/contact/map")
    assert resp.status_code == 503
    assert "GOOGLE_MAPS_API_KEY" in resp.json()["detail"]


def test_us_3_crown_ai_is_presented_as_company_product(client):
    info = client.get("/openapi.json").json()["info"]
    assert "CrownAI" in info["title"]
    assert "Crownwright" in info["description"]


# ===========================================================================
# US-4: Create project and generate SDLC/STLC artifacts
# ===========================================================================


def test_us_4_create_project_returns_201_with_initial_status(client):
    headers, _ = _session_for()
    project = _create_project(client, headers, name="  Payroll  ", requirements="  Pay staff monthly.  ")
    assert project["name"] == "Payroll"
    assert project["requirements"] == "Pay staff monthly."
    assert project["status"] == "requirements"
    assert project["id"] and project["created_at"]


def test_us_4_full_pipeline_generates_every_stage_in_order(client):
    headers, _ = _session_for()
    project = _create_project(client, headers, name="Fleet Tracker", requirements="Track delivery vans in real time.")
    artifacts = _generate_all(client, headers, project["id"])
    assert [a["stage"] for a in artifacts] == ALL_STAGES
    for a in artifacts:
        assert a["project_id"] == project["id"]
        assert a["stage_label"] == STAGE_LABELS[STAGE_ORDER[ALL_STAGES.index(a["stage"])]]
        assert "Fleet Tracker" in a["content"]
    assert "Track delivery vans in real time." in artifacts[0]["content"]
    assert "class Service" in artifacts[2]["content"]

    detail = client.get(f"/projects/{project['id']}", headers=headers).json()
    assert detail["status"] == "nfr"
    assert [a["stage"] for a in detail["artifacts"]] == ALL_STAGES
    listed = client.get(f"/projects/{project['id']}/artifacts", headers=headers).json()
    assert [a["stage"] for a in listed] == ALL_STAGES


def test_us_4_generation_responds_within_progress_budget(client):
    headers, _ = _session_for()
    project = _create_project(client, headers)
    start = time.perf_counter()
    resp = client.post(f"/projects/{project['id']}/generate/requirements", headers=headers)
    assert time.perf_counter() - start < GENERATION_PROGRESS_BUDGET_S
    assert resp.status_code == 200


@pytest.mark.parametrize("stage", ALL_STAGES[1:])
def test_us_4_stage_out_of_order_is_409(client, stage):
    headers, _ = _session_for()
    project = _create_project(client, headers)
    resp = client.post(f"/projects/{project['id']}/generate/{stage}", headers=headers)
    assert resp.status_code == 409
    assert ALL_STAGES[ALL_STAGES.index(stage) - 1] in resp.json()["detail"]


def test_us_4_out_of_order_attempt_does_not_consume_quota(client):
    headers, _ = _session_for()
    project = _create_project(client, headers)
    for _ in range(FREE_LIMIT + 2):
        assert client.post(f"/projects/{project['id']}/generate/code", headers=headers).status_code == 409
    _generate_all(client, headers, project["id"])  # all 5 slots still available


def test_us_4_unknown_stage_is_422(client):
    headers, _ = _session_for()
    project = _create_project(client, headers)
    assert client.post(f"/projects/{project['id']}/generate/deploy", headers=headers).status_code == 422


def test_us_4_list_projects_only_shows_own_projects_newest_first(client):
    h1, _ = _session_for()
    h2, _ = _session_for()
    a = _create_project(client, h1, name="First")
    time.sleep(0.01)
    b = _create_project(client, h1, name="Second")
    _create_project(client, h2, name="Other User")
    listed = client.get("/projects", headers=h1).json()
    assert [p["id"] for p in listed] == [b["id"], a["id"]]


def test_us_4_sessions_are_independent_other_user_gets_403(client):
    owner, _ = _session_for()
    intruder, _ = _session_for()
    project = _create_project(client, owner)
    pid = project["id"]
    assert client.get(f"/projects/{pid}", headers=intruder).status_code == 403
    assert client.get(f"/projects/{pid}/artifacts", headers=intruder).status_code == 403
    assert client.post(f"/projects/{pid}/generate/requirements", headers=intruder).status_code == 403
    assert client.post(f"/projects/{pid}/download", headers=intruder).status_code == 403
    assert client.delete(f"/projects/{pid}", headers=intruder).status_code == 403
    assert client.get(f"/projects/{pid}", headers=owner).status_code == 200


def test_us_4_unknown_project_id_is_404(client):
    headers, _ = _session_for()
    bogus = uuid.uuid4().hex
    assert client.get(f"/projects/{bogus}", headers=headers).status_code == 404
    assert client.get(f"/projects/{bogus}/artifacts", headers=headers).status_code == 404
    assert client.post(f"/projects/{bogus}/generate/requirements", headers=headers).status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"name": "Only name"},
        {"requirements": "Only requirements"},
        {"name": "", "requirements": "x"},
        {"name": "x", "requirements": ""},
        {"name": "   ", "requirements": "x"},
        {"name": "x", "requirements": "\n\t "},
        {"name": "x" * 201, "requirements": "x"},
        {"name": "x", "requirements": "x" * 8001},
        {"name": None, "requirements": "x"},
    ],
)
def test_us_4_create_project_validation_422(client, body):
    headers, _ = _session_for()
    assert client.post("/projects", json=body, headers=headers).status_code == 422


def test_us_4_create_project_boundary_lengths_accepted(client):
    headers, _ = _session_for()
    resp = client.post("/projects", json={"name": "x" * 200, "requirements": "y" * 8000}, headers=headers)
    assert resp.status_code == 201


def test_us_4_project_endpoints_require_auth(client):
    pid = uuid.uuid4().hex
    assert client.get("/projects").status_code == 401
    assert client.post("/projects", json={"name": "a", "requirements": "b"}).status_code == 401
    assert client.get(f"/projects/{pid}").status_code == 401
    assert client.post(f"/projects/{pid}/generate/requirements").status_code == 401
    assert client.post(f"/projects/{pid}/download").status_code == 401
    assert client.delete(f"/projects/{pid}").status_code == 401


def test_us_4_free_tier_daily_quota_enforced_across_projects(client):
    headers, _ = _session_for()
    p1 = _create_project(client, headers, name="P1")
    _generate_all(client, headers, p1["id"], ALL_STAGES[:3])
    p2 = _create_project(client, headers, name="P2")
    _generate_all(client, headers, p2["id"], ALL_STAGES[:2])
    blocked = client.post(f"/projects/{p1['id']}/generate/tests", headers=headers)
    assert blocked.status_code == 429
    assert "Pricing" in blocked.json()["detail"]
    # Nothing was written for the blocked attempt.
    assert len(client.get(f"/projects/{p1['id']}/artifacts", headers=headers).json()) == 3


def test_us_4_quota_is_per_user(client):
    h1, _ = _session_for()
    h2, _ = _session_for()
    p1 = _create_project(client, h1)
    _generate_all(client, h1, p1["id"])
    assert client.post(f"/projects/{p1['id']}/generate/requirements", headers=h1).status_code == 429
    p2 = _create_project(client, h2)
    assert client.post(f"/projects/{p2['id']}/generate/requirements", headers=h2).status_code == 200


def test_us_4_paid_tier_not_capped(client):
    headers, _ = _session_for()
    _set_tier(headers, client, "mid")
    project = _create_project(client, headers)
    _generate_all(client, headers, project["id"])
    for _ in range(FREE_LIMIT):
        assert client.post(f"/projects/{project['id']}/generate/requirements", headers=headers).status_code == 200


# ===========================================================================
# US-5: Free tier blocks download
# ===========================================================================


def test_us_5_free_user_download_blocked_with_pricing_redirect(client):
    headers, _ = _session_for()
    project = _create_project(client, headers)
    _generate_all(client, headers, project["id"], ALL_STAGES[:3])
    resp = client.post(f"/projects/{project['id']}/download", headers=headers)
    assert resp.status_code == 402
    detail = resp.json()["detail"]
    assert detail["upgrade_url"] == "/pricing"
    assert "paid plan" in detail["message"]
    assert resp.headers["content-type"].startswith("application/json")


def test_us_5_free_user_blocked_even_without_artifacts(client):
    headers, _ = _session_for()
    project = _create_project(client, headers)
    assert client.post(f"/projects/{project['id']}/download", headers=headers).status_code == 402


def test_us_5_download_unknown_project_is_404_not_402(client):
    headers, _ = _session_for()
    assert client.post(f"/projects/{uuid.uuid4().hex}/download", headers=headers).status_code == 404


def test_us_5_paid_user_download_returns_zip_of_all_artifacts(client):
    headers, _ = _session_for()
    _set_tier(headers, client, "mid")
    project = _create_project(client, headers, name="My Shop")
    _generate_all(client, headers, project["id"])
    resp = client.post(f"/projects/{project['id']}/download", headers=headers)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert 'filename="My_Shop_crownai_export.zip"' in resp.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = set(zf.namelist())
        assert names == {"requirements.md", "design.md", "code.py", "tests.md", "nfr.md"}
        assert "class Service" in zf.read("code.py").decode()


def test_us_5_paid_user_download_with_no_artifacts_is_404(client):
    headers, _ = _session_for()
    _set_tier(headers, client, "mid")
    project = _create_project(client, headers)
    assert client.post(f"/projects/{project['id']}/download", headers=headers).status_code == 404


def test_us_5_download_reflects_tier_change_without_reauth(client):
    headers, _ = _session_for()
    project = _create_project(client, headers)
    _generate_all(client, headers, project["id"], ALL_STAGES[:1])
    assert client.post(f"/projects/{project['id']}/download", headers=headers).status_code == 402
    _set_tier(headers, client, "mid")
    assert client.post(f"/projects/{project['id']}/download", headers=headers).status_code == 200


# ===========================================================================
# US-6: Lead capture before checkout
# ===========================================================================


@pytest.mark.parametrize("plan", ["mid", "large", "global"])
def test_us_6_lead_capture_for_paid_plan(client, plan):
    lead = _lead(client, plan=plan, email="Mixed.Case@Example.COM", name="  Ravi  ", company="  Crownwright  ")
    assert lead["plan"] == plan
    assert lead["email"] == "mixed.case@example.com"
    assert lead["name"] == "Ravi"
    assert lead["company"] == "Crownwright"
    assert lead["phone"] == "+91 98765 43210"
    assert lead["id"]


def test_us_6_lead_capture_does_not_require_sign_in(client):
    _lead(client)


@pytest.mark.parametrize("missing", ["name", "email", "company", "phone", "plan"])
def test_us_6_lead_missing_field_is_422(client, missing):
    body = {"name": "A", "email": "a@b.co", "company": "C", "phone": "9876543210", "plan": "mid"}
    body.pop(missing)
    assert client.post("/pricing/leads", json=body).status_code == 422


@pytest.mark.parametrize(
    "override",
    [
        {"plan": "free"},
        {"plan": "platinum"},
        {"email": "not-an-email"},
        {"phone": "abc"},
        {"phone": "123"},
        {"phone": "1" * 31},
        {"name": ""},
        {"company": ""},
        {"name": "x" * 201},
    ],
)
def test_us_6_lead_invalid_values_are_422(client, override):
    body = {"name": "A", "email": "a@b.co", "company": "C", "phone": "9876543210", "plan": "mid"}
    body.update(override)
    assert client.post("/pricing/leads", json=body).status_code == 422


@pytest.mark.parametrize("phone", ["9876543210", "+1 (555) 123-4567", "044-2745 1234"])
def test_us_6_lead_accepts_common_phone_formats(client, phone):
    _lead(client, phone=phone)


def test_us_6_checkout_without_lead_is_rejected(client, fake_stripe):
    resp = client.post("/pricing/checkout", json={"lead_id": uuid.uuid4().hex, "plan": "mid"})
    assert resp.status_code == 404
    assert "lead" in resp.json()["detail"].lower()
    assert fake_stripe.sessions == []


@pytest.mark.parametrize("body", [{}, {"plan": "mid"}, {"lead_id": "x"}, {"lead_id": "x", "plan": "free"}])
def test_us_6_checkout_validation_422(client, body):
    assert client.post("/pricing/checkout", json=body).status_code == 422


def test_us_6_checkout_plan_must_match_lead(client, fake_stripe):
    lead = _lead(client, plan="mid")
    resp = client.post("/pricing/checkout", json={"lead_id": lead["id"], "plan": "large"})
    assert resp.status_code == 400
    assert fake_stripe.sessions == []


# ===========================================================================
# US-7: Live plan purchase
# ===========================================================================


def test_us_7_checkout_creates_live_session_with_plan_amount(client, fake_stripe):
    headers, user = _session_for()
    lead = _lead(client, plan="large", email=user["email"])
    resp = client.post("/pricing/checkout", json={"lead_id": lead["id"], "plan": "large"}, headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["checkout_url"].startswith("https://checkout.stripe.com/")
    sent = fake_stripe.sessions[-1]
    assert sent["id"] == body["session_id"]
    assert sent["amount_cents"] == PLANS["large"]["amount_cents"] == 5000_00 + 22500_00
    assert sent["currency"] == "usd"
    assert sent["customer_email"] == user["email"]
    assert sent["client_reference_id"] == user["id"]
    assert sent["metadata"] == {"lead_id": lead["id"], "plan": "large", "user_id": user["id"]}
    assert "checkout=success" in sent["success_url"] and "checkout=cancelled" in sent["cancel_url"]

    with get_conn() as conn:
        payment = db_module.get_payment_by_session(conn, body["session_id"])
    assert payment["status"] == "pending"
    assert payment["user_id"] == user["id"]


def test_us_7_no_raw_card_data_accepted_by_api(client, fake_stripe):
    lead = _lead(client)
    resp = client.post(
        "/pricing/checkout",
        json={"lead_id": lead["id"], "plan": "mid", "card_number": "4242424242424242", "cvc": "123"},
    )
    assert resp.status_code == 200
    sent = fake_stripe.sessions[-1]
    assert "4242424242424242" not in repr(sent)


def test_us_7_webhook_upgrades_signed_in_buyer(client, fake_stripe):
    headers, user = _session_for()
    project = _create_project(client, headers)
    _generate_all(client, headers, project["id"], ALL_STAGES[:1])
    lead = _lead(client, email=user["email"])
    session_id = client.post("/pricing/checkout", json={"lead_id": lead["id"], "plan": "mid"}, headers=headers).json()[
        "session_id"
    ]
    assert client.get("/auth/me", headers=headers).json()["tier"] == "free"  # not before payment completes

    resp = _complete_webhook(client, session_id)
    assert resp.status_code == 200 and resp.json() == {"received": True}
    assert client.get("/auth/me", headers=headers).json()["tier"] == "mid"
    assert client.post(f"/projects/{project['id']}/download", headers=headers).status_code == 200

    with get_conn() as conn:
        payment = db_module.get_payment_by_session(conn, session_id)
    detail = client.get(f"/pricing/payments/{payment['id']}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["status"] == "succeeded"
    assert detail.json()["plan"] == "mid"


def test_us_7_anonymous_checkout_for_existing_account_upgrades_via_webhook(client, fake_stripe):
    headers, user = _session_for()
    lead = _lead(client, email=user["email"].upper())
    resp = client.post("/pricing/checkout", json={"lead_id": lead["id"], "plan": "mid"})
    assert resp.status_code == 200
    assert fake_stripe.sessions[-1]["client_reference_id"] == f"lead_{lead['id']}"
    _complete_webhook(client, resp.json()["session_id"])
    assert client.get("/auth/me", headers=headers).json()["tier"] == "mid"


def test_us_7_anonymous_paid_lead_claimed_at_first_sign_in(client, fake_stripe, force_mock):
    email = _unique_email()
    lead = _lead(client, email=email)
    session_id = client.post("/pricing/checkout", json={"lead_id": lead["id"], "plan": "mid"}).json()["session_id"]
    _complete_webhook(client, session_id)
    headers, _ = _mock_sign_in(client, "microsoft", email=email)
    assert client.get("/auth/me", headers=headers).json()["tier"] == "mid"


def test_us_7_unpaid_anonymous_checkout_does_not_upgrade(client, fake_stripe, force_mock):
    email = _unique_email()
    lead = _lead(client, email=email)
    client.post("/pricing/checkout", json={"lead_id": lead["id"], "plan": "mid"})
    headers, _ = _mock_sign_in(client, "google", email=email)
    assert client.get("/auth/me", headers=headers).json()["tier"] == "free"


def test_us_7_webhook_bad_signature_rejected_and_no_upgrade(client, fake_stripe):
    headers, user = _session_for()
    lead = _lead(client, email=user["email"])
    sid = client.post("/pricing/checkout", json={"lead_id": lead["id"], "plan": "mid"}, headers=headers).json()[
        "session_id"
    ]
    assert _complete_webhook(client, sid, signature="forged").status_code == 400
    assert client.get("/auth/me", headers=headers).json()["tier"] == "free"


def test_us_7_webhook_unknown_session_and_other_events_are_ignored(client, fake_stripe):
    import json

    assert _complete_webhook(client, "cs_unknown").status_code == 200
    other = {"type": "payment_intent.created", "data": {"object": {"id": "pi_1"}}}
    resp = client.post("/pricing/webhook", content=json.dumps(other), headers={"stripe-signature": "valid-signature"})
    assert resp.status_code == 200


def test_us_7_checkout_without_stripe_configured_is_503(client, monkeypatch):
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    lead = _lead(client)
    resp = client.post("/pricing/checkout", json={"lead_id": lead["id"], "plan": "mid"})
    assert resp.status_code == 503
    assert "STRIPE_SECRET_KEY" in resp.json()["detail"]


def test_us_7_webhook_without_stripe_configured_is_503(client, monkeypatch):
    def unconfigured(payload, sig):
        raise StripeNotConfigured()

    monkeypatch.setattr(pricing_module, "verify_webhook_event", unconfigured)
    assert client.post("/pricing/webhook", content=b"{}").status_code == 503


def test_us_7_checkout_with_invalid_token_is_401_not_anonymous(client, fake_stripe):
    lead = _lead(client)
    resp = client.post(
        "/pricing/checkout",
        json={"lead_id": lead["id"], "plan": "mid"},
        headers={"Authorization": "Bearer garbage"},
    )
    assert resp.status_code == 401
    assert fake_stripe.sessions == []


def test_us_7_payment_detail_access_control(client, fake_stripe):
    headers, user = _session_for()
    other, _ = _session_for()
    lead = _lead(client, email=user["email"])
    sid = client.post("/pricing/checkout", json={"lead_id": lead["id"], "plan": "mid"}, headers=headers).json()[
        "session_id"
    ]
    with get_conn() as conn:
        pid = db_module.get_payment_by_session(conn, sid)["id"]
    assert client.get(f"/pricing/payments/{pid}", headers=other).status_code == 403
    assert client.get(f"/pricing/payments/{pid}").status_code == 401
    assert client.get(f"/pricing/payments/{uuid.uuid4().hex}", headers=headers).status_code == 404


def _signed_in_checkout(client, plan="mid"):
    headers, user = _session_for()
    lead = _lead(client, plan=plan, email=user["email"])
    resp = client.post("/pricing/checkout", json={"lead_id": lead["id"], "plan": plan}, headers=headers)
    assert resp.status_code == 200, resp.text
    return headers, user, resp.json()["session_id"]


@pytest.mark.parametrize("stripe_status", ["paid", "no_payment_required"])
def test_us_7_return_from_checkout_confirms_and_upgrades(client, fake_stripe, stripe_status):
    headers, _, sid = _signed_in_checkout(client)
    fake_stripe.payment_status[sid] = stripe_status
    resp = client.get(f"/pricing/checkout/{sid}", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"status": "succeeded", "plan": "mid"}
    assert client.get("/auth/me", headers=headers).json()["tier"] == "mid"


def test_us_7_return_from_checkout_unpaid_stays_pending(client, fake_stripe):
    headers, _, sid = _signed_in_checkout(client)
    resp = client.get(f"/pricing/checkout/{sid}", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"status": "pending", "plan": "mid"}
    assert client.get("/auth/me", headers=headers).json()["tier"] == "free"


def test_us_7_confirm_after_webhook_does_not_recall_stripe(client, fake_stripe):
    headers, _, sid = _signed_in_checkout(client)
    _complete_webhook(client, sid)
    resp = client.get(f"/pricing/checkout/{sid}", headers=headers)
    assert resp.json()["status"] == "succeeded"
    assert sid not in fake_stripe.retrieved


def test_us_7_webhook_and_confirmation_are_idempotent(client, fake_stripe):
    headers, user, sid = _signed_in_checkout(client)
    fake_stripe.payment_status[sid] = "paid"
    assert client.get(f"/pricing/checkout/{sid}", headers=headers).json()["status"] == "succeeded"
    assert _complete_webhook(client, sid).status_code == 200
    assert _complete_webhook(client, sid).status_code == 200
    assert client.get("/auth/me", headers=headers).json()["tier"] == "mid"
    with get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM payments WHERE stripe_session_id = ?", (sid,)).fetchone()["n"]
    assert n == 1


def test_us_7_purchase_never_downgrades_tier(client, fake_stripe):
    headers, _, sid = _signed_in_checkout(client, plan="mid")
    _set_tier(headers, client, "large")
    _complete_webhook(client, sid)
    assert client.get("/auth/me", headers=headers).json()["tier"] == "large"


def test_us_7_confirm_unknown_session_is_404(client, fake_stripe):
    assert client.get("/pricing/checkout/cs_does_not_exist").status_code == 404


def test_us_7_confirm_other_users_checkout_is_403(client, fake_stripe):
    _, _, sid = _signed_in_checkout(client)
    other, _ = _session_for()
    fake_stripe.payment_status[sid] = "paid"
    assert client.get(f"/pricing/checkout/{sid}", headers=other).status_code == 403
    assert sid not in fake_stripe.retrieved


def test_us_7_confirm_with_invalid_token_is_401(client, fake_stripe):
    _, _, sid = _signed_in_checkout(client)
    assert client.get(f"/pricing/checkout/{sid}", headers={"Authorization": "Bearer junk"}).status_code == 401


def test_us_7_confirm_stripe_unreachable_is_502_and_no_upgrade(client, fake_stripe, monkeypatch):
    headers, _, sid = _signed_in_checkout(client)

    def boom(session_id):
        raise ConnectionError("network down")

    monkeypatch.setattr(pricing_module, "retrieve_checkout_session", boom)
    resp = client.get(f"/pricing/checkout/{sid}", headers=headers)
    assert resp.status_code == 502
    assert "retry" in resp.json()["detail"].lower()
    assert client.get("/auth/me", headers=headers).json()["tier"] == "free"


def test_us_7_confirm_stripe_not_configured_is_503(client, fake_stripe, monkeypatch):
    headers, _, sid = _signed_in_checkout(client)

    def unconfigured(session_id):
        raise StripeNotConfigured()

    monkeypatch.setattr(pricing_module, "retrieve_checkout_session", unconfigured)
    assert client.get(f"/pricing/checkout/{sid}", headers=headers).status_code == 503


# ===========================================================================
# US-8: Geo-based privacy compliance
# ===========================================================================


@pytest.mark.parametrize(
    "country,regime,opt_in",
    [("IN", "DPDP", True), ("DE", "GDPR", True), ("FR", "GDPR", True), ("NO", "GDPR", True), ("US", "CCPA", False), ("BR", "GENERIC", False)],
)
def test_us_8_policy_by_country_param(client, country, regime, opt_in):
    resp = client.get("/consent/policy", params={"country": country})
    assert resp.status_code == 200
    body = resp.json()
    assert body["regime"] == regime
    assert body["requires_opt_in"] is opt_in
    assert body["country"] == country
    assert body["banner_text"] and body["rights"]


def test_us_8_every_eu_country_gets_gdpr(client):
    for c in sorted(EU_COUNTRIES):
        assert client.get("/consent/policy", params={"country": c}).json()["regime"] == "GDPR"


def test_us_8_dpdp_and_gdpr_include_erasure_rights(client):
    assert "erasure" in client.get("/consent/policy", params={"country": "IN"}).json()["rights"]
    assert "erasure" in client.get("/consent/policy", params={"country": "IE"}).json()["rights"]


def test_us_8_country_param_case_insensitive(client):
    body = client.get("/consent/policy", params={"country": "in"}).json()
    assert body["regime"] == "DPDP" and body["country"] == "IN"


@pytest.mark.parametrize(
    "header", ["cf-ipcountry", "x-country-code", "cloudfront-viewer-country", "x-vercel-ip-country", "x-appengine-country", "fastly-geo-country-code"]
)
def test_us_8_location_detected_from_geo_headers(client, header):
    body = client.get("/consent/policy", headers={header: "in"}).json()
    assert body["regime"] == "DPDP"
    assert body["country"] == "IN"


def test_us_8_explicit_country_overrides_detected_header(client):
    body = client.get("/consent/policy", params={"country": "DE"}, headers={"cf-ipcountry": "IN"}).json()
    assert body["regime"] == "GDPR"


def test_us_8_header_precedence_order(client):
    body = client.get("/consent/policy", headers={"cf-ipcountry": "IN", "x-country-code": "DE"}).json()
    assert body["country"] == "IN"


@pytest.mark.parametrize("value", ["XX", "", "IND", "1N", "  "])
def test_us_8_unknown_geo_markers_fall_back_to_default(client, value):
    body = client.get("/consent/policy", headers={"cf-ipcountry": value}).json()
    assert body["country"] == "US"
    assert body["regime"] == "CCPA"


def test_us_8_invalid_header_skipped_for_next_valid_one(client):
    body = client.get("/consent/policy", headers={"cf-ipcountry": "XX", "x-country-code": "FR"}).json()
    assert body["regime"] == "GDPR"


@pytest.mark.parametrize("country", ["I", "IND", "Germany"])
def test_us_8_bad_country_param_is_422(client, country):
    assert client.get("/consent/policy", params={"country": country}).status_code == 422


# ===========================================================================
# US-9: Contact form
# ===========================================================================


def test_us_9_contact_inquiry_captured(client):
    resp = client.post("/contact", json={"name": "  Kiran  ", "email": "kiran@example.com", "message": "  Need a quote.  "})
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "Kiran" and body["message"] == "Need a quote."
    assert body["id"] and body["created_at"]
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM contact_inquiries WHERE id = ?", (body["id"],)).fetchone()
    assert row["email"] == "kiran@example.com"


@pytest.mark.parametrize("missing", ["name", "email", "message"])
def test_us_9_contact_missing_field_is_422(client, missing):
    body = {"name": "A", "email": "a@b.co", "message": "Hi"}
    body.pop(missing)
    assert client.post("/contact", json=body).status_code == 422


@pytest.mark.parametrize(
    "override",
    [{"email": "nope"}, {"name": ""}, {"name": "   "}, {"message": ""}, {"message": " \n "}, {"message": "x" * 5001}, {"name": "x" * 201}],
)
def test_us_9_contact_invalid_values_are_422(client, override):
    body = {"name": "A", "email": "a@b.co", "message": "Hi"}
    body.update(override)
    assert client.post("/contact", json=body).status_code == 422


def test_us_9_contact_does_not_require_sign_in(client):
    assert client.post("/contact", json={"name": "A", "email": "a@b.co", "message": "Hi"}).status_code == 201


def test_us_9_contact_non_json_body_is_422(client):
    assert client.post("/contact", content=b"not json", headers={"content-type": "application/json"}).status_code == 422


# ===========================================================================
# US-10: User deletes own project data
# ===========================================================================


def test_us_10_delete_project_removes_project_and_artifacts(client):
    headers, _ = _session_for()
    project = _create_project(client, headers)
    _generate_all(client, headers, project["id"], ALL_STAGES[:2])
    resp = client.delete(f"/projects/{project['id']}", headers=headers)
    assert resp.status_code == 204
    assert resp.content == b""
    assert client.get(f"/projects/{project['id']}", headers=headers).status_code == 404
    assert client.get(f"/projects/{project['id']}/artifacts", headers=headers).status_code == 404
    assert all(p["id"] != project["id"] for p in client.get("/projects", headers=headers).json())
    with get_conn() as conn:
        left = conn.execute("SELECT COUNT(*) AS n FROM artifacts WHERE project_id = ?", (project["id"],)).fetchone()
    assert left["n"] == 0


def test_us_10_delete_twice_is_404(client):
    headers, _ = _session_for()
    project = _create_project(client, headers)
    assert client.delete(f"/projects/{project['id']}", headers=headers).status_code == 204
    assert client.delete(f"/projects/{project['id']}", headers=headers).status_code == 404


def test_us_10_delete_unknown_project_is_404(client):
    headers, _ = _session_for()
    assert client.delete(f"/projects/{uuid.uuid4().hex}", headers=headers).status_code == 404


def test_us_10_delete_only_affects_selected_project(client):
    headers, _ = _session_for()
    keep = _create_project(client, headers, name="Keep")
    drop = _create_project(client, headers, name="Drop")
    _generate_all(client, headers, keep["id"], ALL_STAGES[:1])
    client.delete(f"/projects/{drop['id']}", headers=headers)
    assert [p["id"] for p in client.get("/projects", headers=headers).json()] == [keep["id"]]
    assert len(client.get(f"/projects/{keep['id']}/artifacts", headers=headers).json()) == 1


def test_us_10_other_user_cannot_delete(client):
    owner, _ = _session_for()
    other, _ = _session_for()
    project = _create_project(client, owner)
    assert client.delete(f"/projects/{project['id']}", headers=other).status_code == 403
    assert client.get(f"/projects/{project['id']}", headers=owner).status_code == 200


def test_us_10_delete_requires_auth(client):
    headers, _ = _session_for()
    project = _create_project(client, headers)
    assert client.delete(f"/projects/{project['id']}").status_code == 401
    assert client.get(f"/projects/{project['id']}", headers=headers).status_code == 200


def test_us_10_account_erasure_removes_all_projects_and_invalidates_token(client, fake_stripe):
    headers, user = _session_for()
    p1 = _create_project(client, headers, name="A")
    p2 = _create_project(client, headers, name="B")
    _generate_all(client, headers, p1["id"], ALL_STAGES[:2])
    lead = _lead(client, email=user["email"])
    sid = client.post("/pricing/checkout", json={"lead_id": lead["id"], "plan": "mid"}, headers=headers).json()[
        "session_id"
    ]

    resp = client.delete("/auth/me", headers=headers)
    assert resp.status_code == 204
    assert resp.content == b""
    assert client.get("/auth/me", headers=headers).status_code == 401
    assert client.get("/projects", headers=headers).status_code == 401

    with get_conn() as conn:
        for pid in (p1["id"], p2["id"]):
            assert db_module.get_project(conn, pid) is None
            assert conn.execute("SELECT COUNT(*) AS n FROM artifacts WHERE project_id = ?", (pid,)).fetchone()["n"] == 0
        assert db_module.get_user(conn, user["id"]) is None
        # Payment record kept for accounting but detached from the erased account.
        payment = db_module.get_payment_by_session(conn, sid)
        assert payment is not None and payment["user_id"] is None


def test_us_10_account_erasure_leaves_other_users_untouched(client):
    h1, _ = _session_for()
    h2, _ = _session_for()
    keep = _create_project(client, h2)
    assert client.delete("/auth/me", headers=h1).status_code == 204
    assert client.get(f"/projects/{keep['id']}", headers=h2).status_code == 200


def test_us_10_account_erasure_requires_auth(client):
    assert client.delete("/auth/me").status_code == 401


def test_us_10_account_erasure_twice_is_401(client):
    headers, _ = _session_for()
    assert client.delete("/auth/me", headers=headers).status_code == 204
    assert client.delete("/auth/me", headers=headers).status_code == 401


def test_us_10_sign_in_after_erasure_starts_fresh_free_account(client, force_mock):
    email = _unique_email()
    headers, _ = _mock_sign_in(client, "google", email=email)
    _set_tier(headers, client, "mid")
    _create_project(client, headers)
    old_id = client.get("/auth/me", headers=headers).json()["id"]
    client.delete("/auth/me", headers=headers)

    new_headers, _ = _mock_sign_in(client, "google", email=email)
    me = client.get("/auth/me", headers=new_headers).json()
    assert me["id"] != old_id
    assert me["tier"] == "free"
    assert client.get("/projects", headers=new_headers).json() == []


# ===========================================================================
# US-11 / US-12: Blog, Careers, Case Studies are static frontend pages; the
# backend must simply not interfere (no stray routes, unknown paths -> 404).
# ===========================================================================


@pytest.mark.parametrize("path", ["/blog", "/careers"])
def test_us_11_blog_and_careers_not_shadowed_by_api(client, path):
    resp = client.get(path)
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/json")


def test_us_12_case_studies_not_shadowed_by_api(client):
    assert client.get("/case-studies").status_code == 404


# ===========================================================================
# Human-requested coverage (Testing defects pass, item 3): concurrent boots of
# init_db on one fresh SQLite file. The in-process threading.Lock alone would
# make the 4-thread test above pass trivially, so these tests also exercise the
# paths the lock does NOT cover: separate processes, schema creation racing
# without the lock, a boot waiting on another live writer, the WAL switch
# retrying on "database is locked", and the legacy payments migration running
# atomically inside the boot's write transaction.
# ===========================================================================

import sqlite3  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
from contextlib import contextmanager  # noqa: E402

_LEGACY_SCHEMA = """
    CREATE TABLE users (id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
        provider TEXT NOT NULL, provider_sub TEXT NOT NULL, tier TEXT NOT NULL DEFAULT 'free',
        created_at TEXT NOT NULL);
    CREATE TABLE leads (id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL, company TEXT NOT NULL,
        phone TEXT NOT NULL, plan TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE payments (id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
        lead_id TEXT REFERENCES leads(id), plan TEXT NOT NULL, amount_cents INTEGER NOT NULL,
        currency TEXT NOT NULL, stripe_session_id TEXT, status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL);
    INSERT INTO users VALUES ('u1', 'old@example.com', 'Old', 'google', 'old', 'mid', '2025-01-01');
    INSERT INTO payments VALUES ('p1', 'u1', NULL, 'mid', 100, 'usd', 'cs_old', 'succeeded', '2025-01-01');
"""

_EXPECTED_TABLES = {"users", "projects", "artifacts", "leads", "payments", "contact_inquiries", "usage_counters"}


def _write_legacy_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(_LEGACY_SCHEMA)
    conn.commit()
    conn.close()


def _schema_state(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        cols = {r["name"]: r["notnull"] for r in conn.execute("PRAGMA table_info(payments)")}
        return tables, str(mode).lower(), cols
    finally:
        conn.close()


def _run_concurrently(fn, n):
    barrier = threading.Barrier(n)
    errors = []

    def worker():
        try:
            barrier.wait(timeout=10)
            fn()
        except Exception as exc:  # reported by the caller
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    return errors


def test_us_1_ui_readiness_init_db_fresh_file_ends_in_wal_with_full_schema(fresh_db_path):
    init_db()
    tables, mode, cols = _schema_state(fresh_db_path)
    assert _EXPECTED_TABLES <= tables
    assert mode == "wal"
    assert cols["user_id"] == 0
    assert "payments_new" not in tables


def test_us_1_ui_readiness_init_db_releases_write_lock_when_done(fresh_db_path):
    # A boot that left its BEGIN IMMEDIATE open would block every request.
    init_db()
    other = sqlite3.connect(fresh_db_path, timeout=0)
    try:
        other.execute("BEGIN IMMEDIATE")
        other.execute("INSERT INTO contact_inquiries VALUES ('c1', 'A', 'a@b.co', 'Hi', '2026-01-01')")
        other.commit()
    finally:
        other.close()


def test_us_1_ui_readiness_concurrent_schema_creation_without_process_lock(fresh_db_path):
    # Separate processes don't share _init_lock; BEGIN IMMEDIATE + busy timeout
    # must serialise them on its own.
    db_module._enable_wal()
    errors = _run_concurrently(db_module._create_schema, 6)
    assert not errors, errors
    tables, mode, _ = _schema_state(fresh_db_path)
    assert _EXPECTED_TABLES <= tables
    assert mode == "wal"


def test_us_1_ui_readiness_concurrent_wal_switch_on_fresh_file(fresh_db_path):
    errors = _run_concurrently(db_module._enable_wal, 6)
    assert not errors, errors
    assert _schema_state(fresh_db_path)[1] == "wal"


def test_us_1_ui_readiness_concurrent_boots_in_separate_processes(fresh_db_path):
    # The real "two servers starting at once" case: distinct interpreters.
    start_at = time.time() + 3.0
    script = (
        "import time, db\n"
        f"time.sleep(max(0, {start_at!r} - time.time()))\n"
        "db.init_db()\n"
        "print('ok')\n"
    )
    env = {**os.environ, "CROWNAI_DB_PATH": fresh_db_path}
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", script],
            cwd=BACKEND_DIR,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(4)
    ]
    results = [p.communicate(timeout=90) for p in procs]
    for p, (out, err) in zip(procs, results):
        assert p.returncode == 0, err
        assert "ok" in out
        assert "database is locked" not in err
    tables, mode, cols = _schema_state(fresh_db_path)
    assert _EXPECTED_TABLES <= tables
    assert mode == "wal"
    assert cols["user_id"] == 0


def test_us_1_ui_readiness_boot_waits_for_live_writer_instead_of_failing(fresh_db_path):
    # Another instance mid-write while this one boots: busy timeout must apply.
    init_db()
    holder = sqlite3.connect(fresh_db_path, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO contact_inquiries VALUES ('held', 'A', 'a@b.co', 'Hi', '2026-01-01')")

    def release():
        holder.commit()
        holder.close()

    timer = threading.Timer(0.5, release)
    timer.start()
    try:
        started = time.perf_counter()
        init_db()
        assert time.perf_counter() - started >= 0.3, "boot did not wait for the writer"
    finally:
        timer.join()
    with get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM contact_inquiries WHERE id='held'").fetchone()["n"] == 1


def test_us_1_ui_readiness_boot_on_fresh_file_waits_out_exclusive_lock(fresh_db_path):
    # Non-WAL file exclusively locked by another process during the WAL switch.
    conn = sqlite3.connect(fresh_db_path)
    conn.execute("CREATE TABLE warmup (x INTEGER)")
    conn.commit()
    conn.close()
    holder = sqlite3.connect(fresh_db_path, check_same_thread=False)
    holder.execute("BEGIN EXCLUSIVE")

    def release():
        holder.rollback()
        holder.close()

    timer = threading.Timer(0.5, release)
    timer.start()
    try:
        init_db()
    finally:
        timer.join()
    tables, mode, _ = _schema_state(fresh_db_path)
    assert mode == "wal"
    assert _EXPECTED_TABLES <= tables


def test_us_1_ui_readiness_enable_wal_retries_on_database_locked(fresh_db_path, monkeypatch):
    real_get_conn = db_module.get_conn
    calls = {"n": 0}

    @contextmanager
    def flaky():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise sqlite3.OperationalError("database is locked")
        with real_get_conn() as c:
            yield c

    monkeypatch.setattr(db_module, "get_conn", flaky)
    db_module._enable_wal(attempts=5, delay=0)
    assert calls["n"] == 3
    assert _schema_state(fresh_db_path)[1] == "wal"


def test_us_1_ui_readiness_enable_wal_does_not_swallow_other_errors(fresh_db_path, monkeypatch):
    @contextmanager
    def broken():
        raise sqlite3.OperationalError("disk I/O error")
        yield  # pragma: no cover

    monkeypatch.setattr(db_module, "get_conn", broken)
    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        db_module._enable_wal(attempts=3, delay=0)


def test_us_1_ui_readiness_concurrent_boots_migrate_legacy_db_exactly_once(fresh_db_path):
    _write_legacy_db(fresh_db_path)
    db_module._enable_wal()
    errors = _run_concurrently(db_module._create_schema, 4)
    assert not errors, errors
    tables, _, cols = _schema_state(fresh_db_path)
    assert cols["user_id"] == 0
    assert "payments_new" not in tables
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM payments").fetchall()
    assert [(r["id"], r["user_id"], r["status"]) for r in rows] == [("p1", "u1", "succeeded")]


def test_us_1_ui_readiness_legacy_migration_is_atomic_with_caller_transaction(fresh_db_path):
    # The migration must not COMMIT on its own: rolling the boot back must
    # leave the legacy table exactly as it was (no half-migrated payments).
    _write_legacy_db(fresh_db_path)
    conn = sqlite3.connect(fresh_db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        db_module._migrate_payments_user_id_nullable(conn)
        assert conn.in_transaction
        conn.rollback()
    finally:
        conn.close()
    tables, _, cols = _schema_state(fresh_db_path)
    assert cols["user_id"] == 1
    assert "payments_new" not in tables


def test_us_1_ui_readiness_legacy_migration_is_noop_on_current_schema(fresh_db_path):
    init_db()
    with get_conn() as conn:
        before = conn.execute("SELECT sql FROM sqlite_master WHERE name='payments'").fetchone()["sql"]
        db_module._migrate_payments_user_id_nullable(conn)
        after = conn.execute("SELECT sql FROM sqlite_master WHERE name='payments'").fetchone()["sql"]
    assert before == after


def test_us_1_ui_readiness_reboot_during_live_traffic_never_fails_requests(fresh_db_path):
    # A second instance booting while the first serves the UI must not make
    # either side see "database is locked".
    with TestClient(app) as c:
        headers, _ = _session_for()
        responses = []
        lock = threading.Lock()

        def create():
            r = c.post("/projects", json={"name": "Live", "requirements": "During reboot."}, headers=headers)
            with lock:
                responses.append(r.status_code)

        barrier = threading.Barrier(8)
        errors = []

        def run(fn):
            try:
                barrier.wait(timeout=10)
                fn()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(db_module._create_schema,)) for _ in range(3)]
        threads += [threading.Thread(target=run, args=(create,)) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        assert not errors, errors
        assert responses == [201] * 5
        assert len(c.get("/projects", headers=headers).json()) == 5


def test_us_1_ui_readiness_many_sequential_boots_are_idempotent(fresh_db_path):
    for _ in range(5):
        init_db()
    with TestClient(app) as c:
        headers, _ = _session_for()
        _create_project(c, headers)
        assert len(c.get("/projects", headers=headers).json()) == 1
    tables, mode, _ = _schema_state(fresh_db_path)
    assert mode == "wal" and _EXPECTED_TABLES <= tables


# ===========================================================================
# Human-requested coverage (Testing defects pass), remaining gaps:
#   1. US-4: once the click reaches "Generate Project", onCreate shows both
#      errors and focuses #proj-name -- the API must report the name error
#      first and the requirements error second, so the field order matches.
#   2. US-12: other request shapes on case-study paths still get a plain 404.
#   3. Startup: loading main again (stale or second server, reloader) must not
#      duplicate routes or sys.path entries, and the FRONTEND_URL origin
#      handling must work for both a deployed URL and a non-default dev port.
# ===========================================================================


def _load_main_alias(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(BACKEND_DIR, "main.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "body",
    [
        {"name": "", "requirements": ""},
        {"name": "   ", "requirements": "  "},
        {},
        {"requirements": "", "name": ""},  # key order in the payload must not matter
    ],
)
def test_us_4_create_form_name_error_reported_before_requirements_error(client, body):
    headers, _ = _session_for()
    resp = client.post("/projects", json=body, headers=headers)
    assert resp.status_code == 422
    locs = [tuple(e["loc"]) for e in resp.json()["detail"]]
    assert locs.index(("body", "name")) < locs.index(("body", "requirements"))


def test_us_4_create_form_each_field_reported_exactly_once(client):
    # One message per field: a field must not show two stacked errors.
    headers, _ = _session_for()
    resp = client.post("/projects", json={"name": "", "requirements": ""}, headers=headers)
    locs = [tuple(e["loc"]) for e in resp.json()["detail"]]
    assert locs.count(("body", "name")) == 1
    assert locs.count(("body", "requirements")) == 1


def test_us_4_create_form_valid_name_with_blank_requirements_after_retyping(client):
    # The failing UI sequence: autofocused name filled, requirements left
    # blank, then submit. Only requirements is flagged; then fixing it works.
    headers, _ = _session_for()
    first = client.post("/projects", json={"name": "Inventory App", "requirements": ""}, headers=headers)
    assert first.status_code == 422
    assert _validation_locs(first) == {("body", "requirements")}
    second = client.post(
        "/projects", json={"name": "Inventory App", "requirements": "Describe what you want to build."}, headers=headers
    )
    assert second.status_code == 201
    assert [p["name"] for p in client.get("/projects", headers=headers).json()] == ["Inventory App"]


def test_us_4_create_form_unknown_project_after_create_is_404(client):
    headers, _ = _session_for()
    _create_project(client, headers)
    assert client.get(f"/projects/{uuid.uuid4().hex}", headers=headers).status_code == 404


@pytest.mark.parametrize("method", ["head", "options"])
def test_us_12_case_study_detail_other_methods_never_5xx(client, method):
    resp = getattr(client, method)("/case-studies/regional-nbfc-chennai")
    assert resp.status_code in (404, 405)


def test_us_12_case_study_detail_with_query_string_is_404(client):
    resp = client.get("/case-studies/regional-nbfc-chennai", params={"ref": "list"})
    assert resp.status_code == 404
    assert resp.json()["detail"]


def test_us_1_ui_readiness_reloading_main_does_not_touch_running_app():
    # A second load of main.py (reloader, second server process) builds its
    # own app; the already-running app keeps exactly its original routes.
    before = sorted(_route_table(app))
    count_before = len(app.routes)
    _load_main_alias("backend_main_reload_check")
    assert sorted(_route_table(app)) == before
    assert len(app.routes) == count_before


def test_us_1_ui_readiness_reloading_main_does_not_duplicate_sys_path():
    import sys as _sys

    # main.py inserts its own directory only when that exact entry is absent.
    _load_main_alias("backend_main_syspath_check")
    _load_main_alias("backend_main_syspath_check_2")
    assert _sys.path.count(BACKEND_DIR) == 1


def test_us_1_ui_readiness_reloaded_app_has_no_duplicate_routes():
    module = _load_main_alias("backend_main_dupes_check")
    pairs = [(m, r.path) for r in module.app.routes if isinstance(r, APIRoute) for m in r.methods]
    assert len(pairs) == len(set(pairs))


def test_us_1_ui_readiness_deployed_frontend_url_is_allowlisted(monkeypatch):
    monkeypatch.setenv("FRONTEND_URL", "https://www.crownwright.example")
    module = _load_main_alias("backend_main_deployed_origin")
    c = TestClient(module.app)  # no lifespan needed for CORS checks
    ok = c.get("/health", headers={"Origin": "https://www.crownwright.example"})
    assert ok.headers.get("access-control-allow-origin") == "https://www.crownwright.example"
    # Allowlisting the deployed site must not open it up to other origins.
    bad = c.get("/health", headers={"Origin": "https://evil.example"})
    assert bad.headers.get("access-control-allow-origin") is None
    # Local dev servers on any port keep working alongside it.
    local = c.get("/health", headers={"Origin": "http://localhost:9603"})
    assert local.headers.get("access-control-allow-origin") == "http://localhost:9603"


@pytest.mark.parametrize("frontend_url", ["http://localhost:3020", "http://127.0.0.1:9603", ""])
def test_us_1_ui_readiness_local_frontend_url_still_allows_every_dev_port(monkeypatch, frontend_url):
    # Next may land on 3020 or reuse an existing server on 9603; whatever
    # FRONTEND_URL says, all local ports stay allowed.
    monkeypatch.setenv("FRONTEND_URL", frontend_url)
    module = _load_main_alias(f"backend_main_local_origin_{abs(hash(frontend_url))}")
    c = TestClient(module.app)
    for origin in ("http://localhost:3000", "http://localhost:3020", "http://localhost:9603", "http://127.0.0.1:3020"):
        resp = c.options(
            "/projects",
            headers={"Origin": origin, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"},
        )
        assert resp.status_code == 200
        assert resp.headers.get("access-control-allow-origin") == origin


def test_us_1_ui_readiness_health_does_not_need_database(monkeypatch):
    # A readiness probe against an instance whose DB isn't reachable yet must
    # still answer, so the runner can tell "server up" from "server missing".
    module = _load_main_alias("backend_main_health_no_db")
    monkeypatch.setattr(db_module, "DB_PATH", os.path.join(tempfile.mkdtemp(), "missing-dir", "x.db"))
    c = TestClient(module.app)  # no lifespan: DB is never opened
    assert c.get("/health").json() == {"status": "ok"}


# ===========================================================================
# Human-requested coverage (Testing defects pass, follow-up gaps):
#   1. US-4: when the click finally lands, onCreate gets one 422 per bad field;
#      single-field failures of every kind must flag only that field, the
#      error must reach the reused dev server's origin (:9603), and a fixed
#      form submitted twice in quick succession must leave a usable workspace.
#   2. US-12: slugs that carry the client name's characters (spaces, colon,
#      parentheses) -- what a mis-built link would request -- are a plain 404.
#   3. Startup: two app instances (stale + new server) with overlapping
#      lifespans on one DB, readiness probes hammered while booting, and no
#      dev ports pinned in main.py.
# ===========================================================================


@pytest.mark.parametrize(
    "body,bad_field",
    [
        ({"name": "x" * 201, "requirements": "Fine."}, "name"),
        ({"name": None, "requirements": "Fine."}, "name"),
        ({"name": "Fine", "requirements": None}, "requirements"),
        ({"name": "Fine", "requirements": "y" * 8001}, "requirements"),
        ({"requirements": "Fine."}, "name"),
        ({"name": "Fine"}, "requirements"),
    ],
)
def test_us_4_create_form_single_bad_field_flags_only_that_field(client, body, bad_field):
    headers, _ = _session_for()
    resp = client.post("/projects", json=body, headers=headers)
    assert resp.status_code == 422
    assert _validation_locs(resp) == {("body", bad_field)}
    assert client.get("/projects", headers=headers).json() == []


def test_us_4_create_form_both_errors_readable_from_reused_dev_server_port(client):
    # The UI may run on the already-running Next server (:9603), not :3020.
    headers, _ = _session_for()
    resp = client.post(
        "/projects",
        json={"name": "", "requirements": ""},
        headers={**headers, "Origin": "http://localhost:9603"},
    )
    assert resp.status_code == 422
    assert resp.headers.get("access-control-allow-origin") == "http://localhost:9603"
    assert {("body", "name"), ("body", "requirements")} <= _validation_locs(resp)


def test_us_4_create_form_fixed_then_double_submitted_projects_are_each_usable(client):
    headers, _ = _session_for()
    assert client.post("/projects", json={"name": "", "requirements": ""}, headers=headers).status_code == 422
    body = {"name": "Inventory App", "requirements": "Track stock levels."}
    first = client.post("/projects", json=body, headers=headers)
    second = client.post("/projects", json=body, headers=headers)
    assert first.status_code == 201 and second.status_code == 201
    ids = {first.json()["id"], second.json()["id"]}
    assert len(ids) == 2
    for pid in ids:
        gen = client.post(f"/projects/{pid}/generate/requirements", headers=headers)
        assert gen.status_code == 200
        assert gen.json()["project_id"] == pid
        # Each project only holds its own artifact; double submit never cross-wires them.
        assert len(client.get(f"/projects/{pid}/artifacts", headers=headers).json()) == 1


def test_us_4_create_form_validation_error_detail_is_list_of_field_entries(client):
    # The form maps each detail entry onto a field; the shape must be stable.
    headers, _ = _session_for()
    resp = client.post("/projects", json={"name": "", "requirements": ""}, headers=headers)
    detail = resp.json()["detail"]
    assert isinstance(detail, list) and len(detail) == 2
    for entry in detail:
        assert {"loc", "msg", "type"} <= set(entry)
        assert entry["loc"][0] == "body"


def test_us_4_create_form_generate_on_unknown_project_is_404_not_409(client):
    # A stale id after a failed create must surface as "not found", not a stage error.
    headers, _ = _session_for()
    resp = client.post(f"/projects/{uuid.uuid4().hex}/generate/design", headers=headers)
    assert resp.status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/case-studies/Regional%20NBFC%20(Chennai)",
        "/case-studies/View%20case%20study%3A%20Regional%20NBFC%20(Chennai)",
        "/case-studies/regional-nbfc-chennai%20",
        "/case-studies/%E2%86%92",
    ],
)
def test_us_12_case_study_slugs_with_client_name_characters_are_json_404(client, path):
    resp = client.get(path)
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json()["detail"]


def test_us_12_case_study_404_readable_cross_origin(client):
    resp = client.get("/case-studies/regional-nbfc-chennai", headers={"Origin": "http://localhost:3020"})
    assert resp.status_code == 404
    assert resp.headers.get("access-control-allow-origin") == "http://localhost:3020"


def test_us_1_ui_readiness_stale_and_new_app_instances_overlap_on_same_db(fresh_db_path):
    # "Another next dev server is already running": an old and a new server
    # alive at once against one DB must both serve the same user's data.
    other = _load_main_alias("backend_main_overlap_instance")
    with TestClient(app) as stale:
        headers, _ = _session_for()
        a = _create_project(stale, headers, name="From stale")
        with TestClient(other.app) as new:
            assert new.get("/health").json() == {"status": "ok"}
            b = _create_project(new, headers, name="From new")
            assert {p["id"] for p in new.get("/projects", headers=headers).json()} == {a["id"], b["id"]}
        # Stopping the new instance must not break the stale one.
        assert {p["id"] for p in stale.get("/projects", headers=headers).json()} == {a["id"], b["id"]}


def test_us_1_ui_readiness_concurrent_health_probes_all_succeed_fast(client):
    # The UI runner polls readiness; parallel probes must all answer quickly.
    results = []
    lock = threading.Lock()

    def probe():
        start = time.perf_counter()
        r = client.get("/health")
        with lock:
            results.append((r.status_code, time.perf_counter() - start))

    threads = [threading.Thread(target=probe) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert len(results) == 10
    assert all(code == 200 for code, _ in results)
    assert max(elapsed for _, elapsed in results) < MARKETING_BUDGET_S


def test_us_1_ui_readiness_main_does_not_pin_dev_server_ports():
    # Port collisions are solved by the any-localhost-port CORS regex, not by
    # hard-coding whichever port the dev server happened to land on.
    with open(os.path.join(BACKEND_DIR, "main.py"), encoding="utf-8") as fh:
        source = fh.read()
    for port in ("3000", "3020", "9603"):
        assert f":{port}" not in source, f"main.py hard-codes dev port {port}"


# ===========================================================================
# Human-requested coverage (Testing defects pass, remaining backend gaps):
#   1. US-4: the form now marks fields touched on change and trims input like
#      JS String.trim(); the API must agree on what "blank" means, answer a
#      rejected submit fast (no layout-shift race window), and stay clean when
#      an invalid form is double-submitted.
#   2. US-12: list-page URL variants the fixed link/nav may produce are plain
#      JSON 404s from the API, never redirects into a backend path.
#   3. Startup: when Next reuses an existing dev server on another port, the
#      configured FRONTEND_URL is read per request so sign-in and checkout
#      return to the server that is actually running.
# ===========================================================================


@pytest.mark.parametrize("blank", [" ", "　", "  ", "\r\n"])
def test_us_4_create_form_unicode_whitespace_name_is_blank_like_js_trim(client, blank):
    headers, _ = _session_for()
    resp = client.post("/projects", json={"name": blank, "requirements": "Build a CRM."}, headers=headers)
    assert resp.status_code == 422
    assert _validation_locs(resp) == {("body", "name")}


def test_us_4_create_form_unicode_whitespace_requirements_is_blank(client):
    headers, _ = _session_for()
    resp = client.post("/projects", json={"name": "CRM", "requirements": " 　"}, headers=headers)
    assert resp.status_code == 422
    assert _validation_locs(resp) == {("body", "requirements")}


def test_us_4_create_form_rejected_submit_answers_fast(client):
    # onCreate shows both errors from this response; it must not lag the click.
    headers, _ = _session_for()
    start = time.perf_counter()
    resp = client.post("/projects", json={"name": "", "requirements": ""}, headers=headers)
    assert time.perf_counter() - start < GENERATION_PROGRESS_BUDGET_S
    assert resp.status_code == 422


def test_us_4_create_form_concurrent_invalid_submits_create_nothing(client):
    headers, _ = _session_for()
    codes = []
    lock = threading.Lock()

    def submit():
        r = client.post("/projects", json={"name": "", "requirements": ""}, headers=headers)
        with lock:
            codes.append(r.status_code)

    threads = [threading.Thread(target=submit) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert codes == [422] * 5
    assert client.get("/projects", headers=headers).json() == []


def test_us_4_create_form_rejected_submit_leaves_existing_projects_untouched(client):
    headers, _ = _session_for()
    existing = _create_project(client, headers, name="Existing")
    _generate_all(client, headers, existing["id"], ALL_STAGES[:1])
    assert client.post("/projects", json={"name": "Second", "requirements": ""}, headers=headers).status_code == 422
    listed = client.get("/projects", headers=headers).json()
    assert [p["id"] for p in listed] == [existing["id"]]
    assert listed[0]["status"] == "requirements"
    assert len(client.get(f"/projects/{existing['id']}/artifacts", headers=headers).json()) == 1


@pytest.mark.parametrize("path", ["/case-studies/", "/case-studies?featured=1", "/case-studies/#regional"])
def test_us_12_case_studies_list_variants_are_json_404_without_redirect(client, path):
    resp = client.get(path, follow_redirects=False)
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/json")


@pytest.mark.parametrize("frontend", ["http://localhost:9603", "http://localhost:3020"])
def test_us_1_ui_readiness_sign_in_returns_to_configured_frontend_port(client, force_mock, monkeypatch, frontend):
    monkeypatch.setattr(auth_module, "FRONTEND_URL", frontend)
    login = client.get("/auth/google/login", follow_redirects=False)
    state = urllib.parse.parse_qs(urllib.parse.urlparse(login.headers["location"]).query)["state"][0]
    resp = client.post(
        "/auth/google/mock", data={"state": state, "name": "P", "email": _unique_email()}, follow_redirects=False
    )
    assert resp.status_code == 302
    assert resp.headers["location"].startswith(f"{frontend}/crown-ai/callback?token=")


def test_us_7_ui_readiness_checkout_returns_to_configured_frontend_port(client, fake_stripe, monkeypatch):
    monkeypatch.setattr(pricing_module, "FRONTEND_URL", "http://localhost:9603")
    lead = _lead(client)
    assert client.post("/pricing/checkout", json={"lead_id": lead["id"], "plan": "mid"}).status_code == 200
    sent = fake_stripe.sessions[-1]
    assert sent["success_url"].startswith("http://localhost:9603/pricing?checkout=success")
    assert sent["cancel_url"] == "http://localhost:9603/pricing?checkout=cancelled"


# ===========================================================================
# Human-requested coverage (Testing defects pass: form race, case-study link
# name, "Compiling / ..." startup hang, npm-audit sharp / source-map-js):
#   1. US-4: once the click lands, a name-only form gets exactly the
#      requirements error; an untouched form gets both; the very first submit
#      after sign-in behaves the same as later ones.
#   2. US-12: the accessible name is a frontend concern; the API must keep
#      every case-study shape out of its routes (covered above) and the OpenAPI
#      schema must not advertise one.
#   3. Startup: the home page compiled forever. Data the marketing pages fetch
#      during SSR must come back fast from a cold, offline backend -- no
#      outbound network call (Stripe, Google, Maps) may happen at boot or on
#      those requests.
#   4. Security: sharp and source-map-js must resolve outside their published
#      vulnerable ranges (checked with the lockfile and, when available,
#      `npm audit`).
# ===========================================================================

import json as _json  # noqa: E402
import re as _re  # noqa: E402
import shutil  # noqa: E402
import socket  # noqa: E402

FRONTEND_DIR = os.path.join(os.path.dirname(BACKEND_DIR), "frontend")
# npm audit flags sharp < 0.35.5 and source-map-js 1.0.0 - 1.2.1.
SHARP_MIN_SAFE = (0, 35, 5)
SOURCE_MAP_JS_MIN_SAFE = (1, 2, 2)
MIN_SAFE = {"sharp": SHARP_MIN_SAFE, "source-map-js": SOURCE_MAP_JS_MIN_SAFE}
AUDITED_PACKAGES = ("sharp", "source-map-js")


@pytest.fixture
def no_network(monkeypatch):
    """Fails any outbound socket use, so a hidden network call at boot or on
    a marketing request shows up as an error instead of a hang."""
    calls = []
    loopback = {"127.0.0.1", "::1", "localhost", None, ""}
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_create_connection = socket.create_connection
    real_getaddrinfo = socket.getaddrinfo

    def _host(address):
        return address[0] if isinstance(address, tuple) and address else address

    def _guard(host, what):
        # Loopback stays allowed: asyncio's self-pipe (socketpair) uses it on Windows.
        if host not in loopback:
            calls.append((what, host))
            raise OSError(f"network access blocked in test: {what} {host}")

    def connect(self, address):
        _guard(_host(address), "connect")
        return real_connect(self, address)

    def connect_ex(self, address):
        _guard(_host(address), "connect_ex")
        return real_connect_ex(self, address)

    def create_connection(address, *args, **kwargs):
        _guard(_host(address), "create_connection")
        return real_create_connection(address, *args, **kwargs)

    def getaddrinfo(host, *args, **kwargs):
        _guard(host, "getaddrinfo")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "create_connection", create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return calls


def test_us_4_create_form_first_submit_after_sign_in_reports_both_errors(client, force_mock):
    # The failing UI test: sign in, land on the autofocused form, click submit.
    headers, _ = _mock_sign_in(client, "google")
    resp = client.post("/projects", json={"name": "", "requirements": ""}, headers=headers)
    assert resp.status_code == 422
    locs = [tuple(e["loc"]) for e in resp.json()["detail"]]
    assert locs == [("body", "name"), ("body", "requirements")]
    assert client.get("/projects", headers=headers).json() == []


def test_us_4_create_form_name_typed_requirements_untouched_flags_requirements_only(client):
    # Name typed (touched via onChange), requirements never touched: the submit
    # must still flag requirements -- formSubmitted, not touched, drives it.
    headers, _ = _session_for()
    for body in ({"name": "Inventory App"}, {"name": "Inventory App", "requirements": ""}):
        resp = client.post("/projects", json=body, headers=headers)
        assert resp.status_code == 422
        assert _validation_locs(resp) == {("body", "requirements")}


def test_us_4_create_form_max_length_name_with_blank_requirements(client):
    headers, _ = _session_for()
    resp = client.post("/projects", json={"name": "x" * 200, "requirements": ""}, headers=headers)
    assert resp.status_code == 422
    assert _validation_locs(resp) == {("body", "requirements")}


def test_us_4_create_form_submit_after_errors_shown_then_stage_one_works(client):
    headers, _ = _session_for()
    assert client.post("/projects", json={"name": "", "requirements": ""}, headers=headers).status_code == 422
    assert client.post("/projects", json={"name": "Shop", "requirements": ""}, headers=headers).status_code == 422
    project = _create_project(client, headers, name="Shop", requirements="Sell sarees online.")
    gen = client.post(f"/projects/{project['id']}/generate/requirements", headers=headers)
    assert gen.status_code == 200
    assert "Sell sarees online." in gen.json()["content"]


def test_us_4_create_form_validation_without_content_type_is_422(client):
    headers, _ = _session_for()
    resp = client.post("/projects", content=b'{"name": "", "requirements": ""}', headers=headers)
    assert resp.status_code == 422


def test_us_12_openapi_schema_has_no_case_study_paths(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert not [p for p in paths if "case-stud" in p.lower()]


def test_us_2_cold_offline_boot_serves_marketing_data(fresh_db_path, no_network, monkeypatch):
    # Pages that SSR-fetch plans / consent policy must never wait on the network.
    for name in ("STRIPE_SECRET_KEY", "GOOGLE_MAPS_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    start = time.perf_counter()
    with TestClient(app) as c:
        assert c.get("/health").json() == {"status": "ok"}
        assert [p["id"] for p in c.get("/pricing/plans").json()] == ["mid", "large", "global"]
        assert c.get("/consent/policy").json()["country"] == "US"
        assert c.get("/consent/policy", params={"country": "IN"}).json()["regime"] == "DPDP"
        assert c.post("/contact", json={"name": "A", "email": "a@b.co", "message": "Hi"}).status_code == 201
        assert c.get("/contact/map").status_code == 503  # clear error, not a hang
    assert time.perf_counter() - start < 5 * MARKETING_BUDGET_S
    assert no_network == [], "boot or a marketing request tried to reach the network"


def test_us_2_first_requests_after_cold_boot_within_page_budget(fresh_db_path):
    with TestClient(app) as c:
        for path, params in (("/health", None), ("/pricing/plans", None), ("/consent/policy", {"country": "DE"})):
            start = time.perf_counter()
            resp = c.get(path, params=params)
            assert time.perf_counter() - start < MARKETING_BUDGET_S, path
            assert resp.status_code == 200


def test_us_1_ui_readiness_offline_mock_sign_in_and_workspace(fresh_db_path, no_network, force_mock):
    # The UI suite runs offline: sign-in and project creation must not touch the network.
    with TestClient(app) as c:
        headers, _ = _mock_sign_in(c, "microsoft")
        project = _create_project(c, headers)
        assert c.post(f"/projects/{project['id']}/generate/requirements", headers=headers).status_code == 200
    assert no_network == []


# ---------- frontend dependency audit (sharp, source-map-js) ----------


def _parse_version(text):
    m = _re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(x) for x in m.groups()) if m else None


def _load_json(path):
    with open(path, encoding="utf-8") as fh:
        return _json.load(fh)


def _lockfile():
    path = os.path.join(FRONTEND_DIR, "package-lock.json")
    if not os.path.exists(path):
        pytest.skip("frontend/package-lock.json not present")
    return _load_json(path)


def _locked_versions(lock, package):
    """Every resolved version of `package` anywhere in the lockfile tree."""
    versions = []
    for key, meta in (lock.get("packages") or {}).items():
        if key == f"node_modules/{package}" or key.endswith(f"/node_modules/{package}"):
            if meta.get("version"):
                versions.append(meta["version"])
    return versions


def test_us_2_frontend_package_json_is_valid_and_pins_sharp_safely():
    path = os.path.join(FRONTEND_DIR, "package.json")
    if not os.path.exists(path):
        pytest.skip("frontend/package.json not present")
    pkg = _load_json(path)
    declared = {}
    for section in ("dependencies", "devDependencies", "optionalDependencies", "overrides", "resolutions"):
        for name, spec in (pkg.get(section) or {}).items():
            if isinstance(spec, str):
                declared.setdefault(name, []).append(spec)
    for spec in declared.get("sharp", []):
        version = _parse_version(spec)
        assert version is not None, f"sharp spec {spec!r} has no concrete version"
        assert version >= SHARP_MIN_SAFE, f"sharp {spec} is inside the vulnerable range"
    for spec in declared.get("source-map-js", []):
        version = _parse_version(spec)
        assert version is not None, f"source-map-js spec {spec!r} has no concrete version"
        assert version >= SOURCE_MAP_JS_MIN_SAFE, f"source-map-js {spec} is inside the vulnerable range"


def test_us_2_frontend_lockfile_resolves_sharp_outside_vulnerable_range():
    lock = _lockfile()
    for version in _locked_versions(lock, "sharp"):
        assert _parse_version(version) >= SHARP_MIN_SAFE, f"lockfile still resolves sharp {version}"


def test_us_2_frontend_lockfile_matches_package_json_pins():
    # A bumped package.json with a stale lockfile still installs the old version.
    lock = _lockfile()
    pkg = _load_json(os.path.join(FRONTEND_DIR, "package.json"))
    for name in AUDITED_PACKAGES:
        for section in ("dependencies", "devDependencies"):
            spec = (pkg.get(section) or {}).get(name)
            if not spec:
                continue
            wanted = _parse_version(spec)
            locked = (lock.get("packages") or {}).get(f"node_modules/{name}", {}).get("version")
            assert locked, f"{name} declared in package.json but missing from package-lock.json"
            if wanted is None:
                continue
            if spec.lstrip("=v")[0:1].isdigit():  # exact pin
                assert _parse_version(locked) == wanted, f"{name}: package.json {spec} vs lockfile {locked}"
            else:
                assert _parse_version(locked) >= wanted, f"{name}: lockfile {locked} below {spec}"


def test_us_2_frontend_npm_audit_reports_no_sharp_or_source_map_js_advisory():
    npm = shutil.which("npm")
    if not npm or not os.path.exists(os.path.join(FRONTEND_DIR, "package-lock.json")):
        pytest.skip("npm or frontend/package-lock.json not available")
    try:
        proc = subprocess.run(
            [npm, "audit", "--json", "--package-lock-only"],
            cwd=FRONTEND_DIR,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except subprocess.TimeoutExpired:
        pytest.skip("npm audit timed out (registry unreachable)")
    try:
        report = _json.loads(proc.stdout or "{}")
    except ValueError:
        pytest.skip(f"npm audit produced no JSON: {proc.stderr[:200]}")
    if "error" in report and "vulnerabilities" not in report:
        pytest.skip(f"npm audit could not run: {report['error']}")
    vulns = report.get("vulnerabilities") or {}
    flagged = {name: vulns[name].get("range") for name in AUDITED_PACKAGES if name in vulns}
    assert not flagged, f"still vulnerable: {flagged}"
    # A fix must not merely move the problem to a package that depends on them.
    via = {
        name
        for name, v in vulns.items()
        for src in (v.get("via") or [])
        if (src if isinstance(src, str) else src.get("name")) in AUDITED_PACKAGES
    }
    assert not via, f"packages still vulnerable through sharp/source-map-js: {sorted(via)}"


# ===========================================================================
# Human-requested coverage (Testing defects pass: blur-shifted submit button,
# case-study link accessible name, "Compiling / ..." startup, npm-audit
# sharp < 0.35.5 / source-map-js <= 1.2.1):
#   - frontend/package.json must carry an `overrides` block forcing the
#     patched versions, without conflicting with direct deps (EOVERRIDE);
#   - every copy of sharp / source-map-js in package-lock.json (including
#     nested node_modules) must resolve to a patched version that still
#     satisfies what next / postcss declare, so the bump can't break them;
#   - the workspace form must not mark fields touched on blur (or must reserve
#     the error slot) and onCreate must flag submit and refocus #proj-name;
#   - the case-study link's accessible name must be exactly
#     "View case study: <client>" (aria-label, no block-level sr-only span).
# Frontend sources are read statically; tests skip when they are absent.
# ===========================================================================


def _package_json():
    path = os.path.join(FRONTEND_DIR, "package.json")
    if not os.path.exists(path):
        pytest.skip("frontend/package.json not present")
    return _load_json(path)


def _frontend_source(*parts):
    path = os.path.join(FRONTEND_DIR, *parts)
    if not os.path.exists(path):
        pytest.skip(f"frontend/{'/'.join(parts)} not present")
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _override_specs(overrides, package):
    """Every override spec for `package`, at any nesting level."""
    found = []
    for key, value in (overrides or {}).items():
        name = key.split("@", 1)[0] if not key.startswith("@") else "@" + key[1:].split("@", 1)[0]
        if isinstance(value, str):
            if name == package:
                found.append(value)
        elif isinstance(value, dict):
            if name == package and isinstance(value.get("."), str):
                found.append(value["."])
            found.extend(_override_specs(value, package))
    return found


def _satisfies(version, spec):
    """Minimal semver check for the range shapes npm lockfiles use here.
    Unknown shapes return True (not this test's concern)."""
    v = _parse_version(version)
    if v is None:
        return False
    for part in spec.split("||"):
        part = part.strip()
        base = _parse_version(part)
        if part in ("", "*", "latest") or base is None:
            return True
        if part.startswith("^"):
            if base[0] > 0:
                ok = v[0] == base[0] and v >= base
            elif base[1] > 0:
                ok = v[:2] == base[:2] and v >= base
            else:
                ok = v == base
        elif part.startswith("~"):
            ok = v[:2] == base[:2] and v >= base
        elif part.startswith(">="):
            ok = v >= base
        elif part[0].isdigit() or part[0] in "=v":
            ok = v == base
        else:
            return True
        if ok:
            return True
    return False


def _dependents(lock, package):
    """(lock key, declared spec) for every lockfile entry that depends on `package`."""
    out = []
    for key, meta in (lock.get("packages") or {}).items():
        for section in ("dependencies", "optionalDependencies", "peerDependencies"):
            spec = (meta.get(section) or {}).get(package)
            if spec:
                out.append((key, section, spec))
    return out


def _resolved_for(lock, dependent_key, package):
    """The copy of `package` node's resolution would pick for `dependent_key`."""
    packages = lock.get("packages") or {}
    base = dependent_key
    while True:
        candidate = f"{base}/node_modules/{package}" if base else f"node_modules/{package}"
        if candidate in packages:
            return packages[candidate].get("version")
        if not base:
            return None
        idx = base.rfind("/node_modules/")
        base = base[:idx] if idx != -1 else ""


@pytest.mark.parametrize("package", AUDITED_PACKAGES)
def test_us_2_frontend_package_json_overrides_force_patched_version(package):
    pkg = _package_json()
    overrides = pkg.get("overrides")
    assert isinstance(overrides, dict) and overrides, "frontend/package.json has no `overrides` block"
    specs = _override_specs(overrides, package)
    assert specs, f"`overrides` does not pin {package}"
    for spec in specs:
        if spec.startswith("$"):
            # "$sharp" defers to the direct dependency spec.
            spec = (pkg.get("dependencies") or {}).get(package) or (pkg.get("devDependencies") or {}).get(package)
            assert spec, f"override ${package} references a dependency that is not declared"
        lower = _parse_version(spec)
        assert lower is not None, f"{package} override {spec!r} has no concrete lower bound (e.g. '*' allows vulnerable)"
        assert lower >= MIN_SAFE[package], f"{package} override {spec} still allows a vulnerable version"
        assert not spec.strip().startswith("<"), f"{package} override {spec} caps instead of raising the floor"


def test_us_2_frontend_overrides_do_not_conflict_with_direct_dependencies():
    # npm refuses to install (EOVERRIDE) when a top-level override differs
    # from the same package's direct dependency spec, unless it uses "$name".
    pkg = _package_json()
    overrides = pkg.get("overrides") or {}
    for section in ("dependencies", "devDependencies", "optionalDependencies"):
        for name, spec in (pkg.get(section) or {}).items():
            override = overrides.get(name)
            if isinstance(override, str) and not override.startswith("$"):
                assert override == spec, f"{name}: override {override!r} conflicts with {section} {spec!r} (EOVERRIDE)"


def test_us_2_frontend_package_json_still_valid_after_overrides_edit():
    # A hand-edited overrides block (trailing comma, clobbered keys) breaks
    # `npm install` and the dev server never starts.
    pkg = _package_json()  # strict json.load: fails on trailing commas
    assert isinstance(pkg, dict)
    assert pkg.get("name") and (pkg.get("scripts") or {}).get("dev"), "package.json lost its name/dev script"
    assert "next" in (pkg.get("dependencies") or {}), "next must remain a dependency"


@pytest.mark.parametrize("package", AUDITED_PACKAGES)
def test_us_2_frontend_lockfile_resolves_every_copy_outside_vulnerable_range(package):
    lock = _lockfile()
    versions = _locked_versions(lock, package)
    for version in versions:
        parsed = _parse_version(version)
        assert parsed is not None, f"{package} locked to unparseable version {version!r}"
        assert parsed >= MIN_SAFE[package], f"lockfile still resolves {package} {version}"


@pytest.mark.parametrize("package", AUDITED_PACKAGES)
def test_us_2_frontend_lockfile_patched_versions_still_satisfy_dependents(package):
    # The override must stay within what next (sharp ^0.35.x) and postcss
    # (source-map-js ^1.2.x) accept -- a major jump would break the build.
    lock = _lockfile()
    for key, section, spec in _dependents(lock, package):
        resolved = _resolved_for(lock, key, package)
        if resolved is None:
            # Optional deps (sharp under next) may be absent on some platforms.
            assert section in ("optionalDependencies", "peerDependencies"), f"{key or 'root'} needs {package} but lockfile has none"
            continue
        assert _satisfies(resolved, spec), f"{key or 'root'} wants {package} {spec} but lockfile gives {resolved}"
        assert _parse_version(resolved) >= MIN_SAFE[package]


def test_us_2_frontend_lockfile_is_regenerated_format():
    # `npm install --package-lock-only` writes lockfileVersion 2/3 with a
    # `packages` map; a hand-edited v1 lockfile would bypass these checks.
    lock = _lockfile()
    assert lock.get("lockfileVersion", 1) >= 2
    assert isinstance(lock.get("packages"), dict) and "" in lock["packages"]


def test_us_2_frontend_lockfile_root_matches_package_json_dependencies():
    # A stale lockfile (package.json edited, lock not regenerated) makes
    # `npm ci` fail in CI before the UI suite can even start.
    lock = _lockfile()
    pkg = _package_json()
    root = lock["packages"].get("", {})
    for section in ("dependencies", "devDependencies"):
        declared = pkg.get(section) or {}
        locked = root.get(section) or {}
        assert declared == locked, f"package-lock.json root {section} out of sync with package.json"


def test_us_2_frontend_lockfile_sharp_entries_have_integrity():
    # Regenerated entries come with resolved + integrity; hand-edited version
    # strings without them are not a real fix.
    lock = _lockfile()
    for key, meta in (lock.get("packages") or {}).items():
        if any(key == f"node_modules/{p}" or key.endswith(f"/node_modules/{p}") for p in AUDITED_PACKAGES):
            if meta.get("link") or meta.get("bundled"):
                continue
            assert meta.get("resolved") and meta.get("integrity"), f"{key} lacks resolved/integrity"
            assert meta["version"] in meta["resolved"], f"{key}: version {meta['version']} != tarball {meta['resolved']}"


# ---------- US-4: workspace create form (blur must not move the button) ----------


def _crown_ai_page():
    return _frontend_source("app", "crown-ai", "page.tsx")


def test_us_4_create_form_blur_does_not_shift_submit_button():
    source = _crown_ai_page()
    blur_touches = [h for h in _re.findall(r"onBlur=\{[^\n]*", source) if "ouched" in h]
    if blur_touches:
        # Allowed only if the error slots reserve their space up front.
        assert _re.search(r"min-h-", source), (
            "onBlur marks the field touched and the error <p> has no reserved min-height: "
            "the submit button moves between mousedown and mouseup"
        )


def test_us_4_create_form_has_name_and_requirements_fields():
    source = _crown_ai_page()
    assert 'id="proj-name"' in source or "id='proj-name'" in source
    assert 'id="proj-reqs"' in source or "id='proj-reqs'" in source


def test_us_4_create_form_submit_sets_submitted_and_refocuses_name():
    source = _crown_ai_page()
    assert _re.search(r"setFormSubmitted\(\s*true\s*\)", source), "onCreate must set formSubmitted"
    assert "proj-name" in source and ".focus(" in source, "onCreate must move focus back to #proj-name"


def test_us_4_create_form_both_error_messages_present():
    source = _crown_ai_page()
    assert "Give your project a name." in source
    assert "Describe what you want to build" in source


def test_us_4_create_form_errors_driven_by_submit_not_only_touch():
    # The requirements error must appear after a submit even if the field
    # was never touched (it has no autofocus and the user never visits it).
    source = _crown_ai_page()
    assert _re.search(r"formSubmitted\s*\|\|\s*formTouched|formTouched[^\n]*\|\|\s*formSubmitted", source), (
        "error visibility should be `formSubmitted || formTouched.<field>`"
    )


def test_us_4_create_form_submit_button_is_type_submit():
    source = _crown_ai_page()
    # Clicking it must submit the form so onCreate runs.
    assert "Generate Project" in source
    assert 'type="submit"' in source
    assert _re.search(r"onSubmit=\{", source), "the create form must submit through onSubmit"


# ---------- US-12: case-study link accessible name ----------


def _case_studies_page():
    return _frontend_source("app", "case-studies", "page.tsx")


def test_us_12_case_study_link_has_exact_aria_label():
    source = _case_studies_page()
    assert _re.search(r"aria-label=\{?\s*[`\"']View case study: (\$\{[^}]+\}|\{)", source), (
        'case-study <Link> needs aria-label="View case study: <client>"'
    )


def test_us_12_case_study_link_has_no_padded_sr_only_name():
    # A block-level sr-only span puts a space before the colon in the name.
    source = _case_studies_page()
    assert not _re.search(r'<span className="sr-only">\s*:', source), (
        "sr-only ': <client>' span still produces 'View case study : <client>'"
    )


def test_us_12_case_study_link_keeps_visible_text_and_slug_href():
    source = _case_studies_page()
    assert "View case study" in source
    assert _re.search(r"href=\{`/case-studies/\$\{[^}]+\}`\}", source), "link must point to /case-studies/<slug>"


def test_us_12_case_study_aria_label_has_no_space_before_colon():
    source = _case_studies_page()
    assert "View case study :" not in source
    assert "View case study  " not in source


# ===========================================================================
# Gap coverage for the same defects pass:
#   - US-4: the fix must not trade the layout shift for a disabled button
#     (a disabled submit also stops onCreate, so no errors and no refocus),
#     the autofocused name field must not be marked touched by blur alone,
#     and onCreate must preventDefault and guard against a double submit.
#   - US-12: the aria-label must name the same client the card shows, and
#     the slug it links to must have a detail page.
#   - npm audit: sharp's platform binaries (@img/sharp-*) must move with it,
#     and override floors must stay inside what next / postcss accept.
#   - Startup: the parallel burst of fetches a cold "Compiling / ..." makes.
# ===========================================================================


def _submit_button_tag(source):
    m = _re.search(r"<button\b((?:(?!</button>).)*?)>(?:(?!</button>).)*Generate Project", source, _re.S)
    if not m:
        pytest.skip("Generate Project button not found in crown-ai/page.tsx")
    return m.group(1)


def _input_tag(source, element_id):
    m = _re.search(r"<(?:input|textarea)\b(?:(?!/>)[^<])*?id=[\"']" + element_id + r"[\"'](?:(?!/>)[^<])*/?>", source, _re.S)
    if not m:
        pytest.skip(f"#{element_id} not found in crown-ai/page.tsx")
    return m.group(0)


def test_us_4_create_form_submit_not_disabled_by_empty_fields():
    # If the button is disabled while fields are empty, the click never
    # reaches onCreate: no formSubmitted, no requirements error, no refocus.
    tag = _submit_button_tag(_crown_ai_page())
    disabled = _re.search(r"disabled=\{([^}]*)\}", tag)
    if disabled:
        expr = disabled.group(1)
        assert not _re.search(r"trim\(|\.length|!\s*(form\.)?(name|requirements|reqs)\b", expr), (
            f"submit is disabled on field content ({expr}); errors would never show"
        )


def test_us_4_create_form_autofocused_name_not_touched_on_blur_without_reserved_slot():
    source = _crown_ai_page()
    tag = _input_tag(source, "proj-name")
    blur = _re.search(r"onBlur=\{([^\n]*)", tag)
    if blur and "ouched" in blur.group(1):
        # Touch-on-blur is only safe if both error slots reserve their height.
        assert len(_re.findall(r"min-h-", source)) >= 2, "both error slots need a reserved min-height"


def test_us_4_create_form_touched_set_on_change_when_not_on_blur():
    source = _crown_ai_page()
    if "setFormTouched" not in source:
        return  # touched state removed entirely: submit drives errors
    name_tag = _input_tag(source, "proj-name")
    on_change_touches = "ouched" in (_re.search(r"onChange=\{([^\n]*)", name_tag) or [""])[0] or _re.search(
        r"setFormTouched\([^)]*name", source
    )
    assert on_change_touches, "name field never becomes touched"


def test_us_4_create_form_on_create_prevents_default_and_guards_double_submit():
    source = _crown_ai_page()
    assert "preventDefault()" in source, "onCreate must stop the native form post"
    tag = _submit_button_tag(source)
    guarded = _re.search(r"disabled=\{[^}]+\}", tag) or _re.search(r"if\s*\(\s*(creating|submitting|busy|loading)", source)
    assert guarded, "a second click while creating must not submit again"


def test_us_4_create_form_requirements_error_wired_to_textarea():
    # The UI test checks the textbox is [invalid] with its error: aria-invalid
    # must be on both fields so a failed submit is announced.
    source = _crown_ai_page()
    for field in ("proj-name", "proj-reqs"):
        assert "aria-invalid" in _input_tag(source, field), f"#{field} lacks aria-invalid"


def test_us_4_create_form_backend_errors_one_per_field_in_form_order(client):
    # The page maps 422 entries to its two slots in order name -> requirements.
    headers, _ = _session_for()
    resp = client.post("/projects", json={"name": " ", "requirements": " "}, headers=headers)
    assert resp.status_code == 422
    locs = [tuple(e["loc"]) for e in resp.json()["detail"]]
    assert locs == [("body", "name"), ("body", "requirements")]


def test_us_12_case_study_aria_label_names_rendered_client():
    source = _case_studies_page()
    m = _re.search(r"aria-label=\{`View case study: \$\{([^}]+)\}`\}", source)
    if not m:
        pytest.skip("aria-label not written as a template literal")
    expr = m.group(1).strip()
    assert expr.endswith("client"), f"aria-label uses {expr}, not the client name"
    assert "{" + expr + "}" in source, "aria-label client must match the client shown on the card"


def test_us_12_case_study_detail_page_exists_for_linked_slugs():
    detail = os.path.join(FRONTEND_DIR, "app", "case-studies", "[slug]", "page.tsx")
    if not os.path.isdir(os.path.join(FRONTEND_DIR, "app", "case-studies")):
        pytest.skip("frontend case-studies route not present")
    assert os.path.exists(detail), "list links to /case-studies/<slug> but there is no [slug] page"


def test_us_12_case_study_regional_nbfc_client_is_defined():
    if not os.path.isdir(FRONTEND_DIR):
        pytest.skip("frontend not present")
    for root, dirs, files in os.walk(FRONTEND_DIR):
        dirs[:] = [d for d in dirs if d not in ("node_modules", ".next", ".git")]
        for f in files:
            if f.endswith((".ts", ".tsx", ".js", ".json", ".md", ".mdx")):
                with open(os.path.join(root, f), encoding="utf-8", errors="ignore") as fh:
                    if "Regional NBFC (Chennai)" in fh.read():
                        return
    pytest.fail("no case study for 'Regional NBFC (Chennai)' -- the UI test's link target")


def test_us_2_frontend_sharp_platform_binaries_match_patched_sharp():
    # Hand-bumping sharp leaves @img/sharp-<platform> at 0.35.4; only a real
    # `npm install --package-lock-only` moves them together.
    lock = _lockfile()
    packages = lock.get("packages") or {}
    sharp = packages.get("node_modules/sharp")
    if not sharp:
        pytest.skip("sharp not in lockfile (optional on this platform)")
    for dep, spec in (sharp.get("optionalDependencies") or {}).items():
        meta = packages.get(f"node_modules/{dep}")
        if meta and meta.get("version"):
            assert _satisfies(meta["version"], spec), f"{dep} {meta['version']} does not satisfy sharp's {spec}"


@pytest.mark.parametrize("package", AUDITED_PACKAGES)
def test_us_2_frontend_override_floor_accepted_by_every_dependent(package):
    # e.g. "sharp": "^1.0.0" would clear the audit but break next's ^0.35.x.
    pkg = _package_json()
    specs = [s for s in _override_specs(pkg.get("overrides"), package) if not s.startswith("$")]
    if not specs:
        pytest.skip(f"no literal override for {package}")
    lock = _lockfile()
    for spec in specs:
        floor = ".".join(str(x) for x in _parse_version(spec))
        for key, _section, dep_spec in _dependents(lock, package):
            assert _satisfies(floor, dep_spec), f"override {package} {spec} outside {key or 'root'}'s {dep_spec}"


@pytest.mark.parametrize("version,ok", [("0.35.4", False), ("0.35.5", True), ("0.36.0", True)])
def test_us_2_sharp_vulnerable_boundary(version, ok):
    assert (_parse_version(version) >= SHARP_MIN_SAFE) is ok


@pytest.mark.parametrize("version,ok", [("1.0.0", False), ("1.2.1", False), ("1.2.2", True)])
def test_us_2_source_map_js_vulnerable_boundary(version, ok):
    assert (_parse_version(version) >= SOURCE_MAP_JS_MIN_SAFE) is ok


def test_us_2_cold_boot_parallel_page_fetches_within_budget(fresh_db_path):
    # "Compiling / ..." fires the home/pricing/consent fetches together.
    with TestClient(app) as c:
        results, lock = [], threading.Lock()

        def fetch(path, params):
            start = time.perf_counter()
            r = c.get(path, params=params)
            with lock:
                results.append((path, r.status_code, time.perf_counter() - start))

        jobs = [("/health", None), ("/pricing/plans", None), ("/consent/policy", {"country": "IN"})] * 4
        threads = [threading.Thread(target=fetch, args=j) for j in jobs]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert len(results) == len(jobs)
    assert all(code == 200 for _, code, _ in results), results
    assert max(d for _, _, d in results) < MARKETING_BUDGET_S


# ===========================================================================
# Human-requested coverage (Testing defects pass: dev server stuck on
# "Compiling / ..." so the ui and performance layers were skipped):
#   The fix asks for the real startup error -- a missing file, a bad import,
#   or config the app expects. Statically check the frontend sources for the
#   errors that make Next's first compile of "/" fail or never finish:
#   - app/page and the root layout (with <html>/<body>) must exist;
#   - every relative and "@/" import must resolve to a real file;
#   - "use client" modules must not export metadata (a Next build error);
#   - app/ pages and layouts that call React hooks must be client components.
#   Also re-pin the two UI fixes this pass ships with (both form errors from
#   one submit, aria-label on every case-study link).
#   Frontend sources are read statically; tests skip when they are absent.
# ===========================================================================

_SOURCE_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs")
_RESOLVE_SUFFIXES = ("", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".json", ".css", "/index.ts", "/index.tsx", "/index.js")
_IMPORT_RE = _re.compile(r"""(?:\bfrom\s+|\bimport\s*\(\s*|^\s*import\s+)["']([^"']+)["']""", _re.M)
_USE_CLIENT_RE = _re.compile(r"""\A(?:\s|//[^\n]*\n|/\*.*?\*/)*["']use client["']""", _re.S)
_HOOK_RE = _re.compile(r"\buse(State|Effect|Ref|Reducer|Router|SearchParams|Pathname|LayoutEffect)\s*\(")


def _frontend_files(*subdirs):
    if not os.path.isdir(FRONTEND_DIR):
        pytest.skip("frontend not present")
    found = []
    for sub in subdirs:
        base = os.path.join(FRONTEND_DIR, sub)
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in ("node_modules", ".next", ".git")]
            found.extend(os.path.join(root, f) for f in files if f.endswith(_SOURCE_EXTS))
    return found


def _app_dir():
    for candidate in (os.path.join(FRONTEND_DIR, "app"), os.path.join(FRONTEND_DIR, "src", "app")):
        if os.path.isdir(candidate):
            return candidate
    pytest.skip("frontend app/ directory not present")


def _alias_bases():
    """Directories the "@/" alias may point at (from tsconfig/jsconfig paths)."""
    bases = []
    for name in ("tsconfig.json", "jsconfig.json"):
        path = os.path.join(FRONTEND_DIR, name)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        # tsconfig allows comments and trailing commas; strip them before parsing.
        text = _re.sub(r"/\*.*?\*/", "", text, flags=_re.S)
        text = _re.sub(r"(^|[^:\"])//[^\n]*", r"\1", text)
        text = _re.sub(r",(\s*[}\]])", r"\1", text)
        try:
            options = _json.loads(text).get("compilerOptions") or {}
        except ValueError:
            continue
        base_url = os.path.join(FRONTEND_DIR, options.get("baseUrl", "."))
        for target in (options.get("paths") or {}).get("@/*", []):
            bases.append(os.path.normpath(os.path.join(base_url, target.rstrip("*"))))
    return bases or [FRONTEND_DIR, os.path.join(FRONTEND_DIR, "src")]


def _resolves(path):
    return any(os.path.isfile(path + suffix) for suffix in _RESOLVE_SUFFIXES)


def _read(path):
    with open(path, encoding="utf-8", errors="ignore") as fh:
        return fh.read()


def _first_existing(directory, stem):
    for ext in (".tsx", ".jsx", ".js", ".ts"):
        path = os.path.join(directory, stem + ext)
        if os.path.exists(path):
            return path
    return None


def test_us_2_frontend_home_page_exists_for_compiling_root():
    # "Compiling / ..." is the compile of app/page; without it "/" can't render.
    app_dir = _app_dir()
    assert _first_existing(app_dir, "page"), "frontend has no app/page -- '/' never finishes compiling"


def test_us_2_frontend_root_layout_renders_html_and_body():
    app_dir = _app_dir()
    layout = _first_existing(app_dir, "layout")
    assert layout, "frontend has no root app/layout"
    source = _read(layout)
    assert "<html" in source and "<body" in source, "root layout must render <html> and <body>"
    assert "export default" in source, "root layout must have a default export"


def test_us_2_frontend_every_route_page_has_default_export():
    for path in _frontend_files("app", os.path.join("src", "app")):
        if os.path.splitext(os.path.basename(path))[0] in ("page", "layout"):
            assert "export default" in _read(path), f"{os.path.relpath(path, FRONTEND_DIR)} has no default export"


def test_us_2_frontend_relative_and_alias_imports_resolve():
    # A missing file behind an import makes the first compile of the route fail.
    alias_bases = _alias_bases()
    broken = []
    for path in _frontend_files("app", "components", "lib", "src"):
        for spec in _IMPORT_RE.findall(_read(path)):
            if spec.startswith(("./", "../")):
                target = os.path.normpath(os.path.join(os.path.dirname(path), spec))
                ok = _resolves(target)
            elif spec.startswith("@/"):
                ok = any(_resolves(os.path.join(base, spec[2:])) for base in alias_bases)
            else:
                continue
            if not ok:
                broken.append(f"{os.path.relpath(path, FRONTEND_DIR)} -> {spec}")
    assert not broken, "unresolved imports: " + "; ".join(broken)


def test_us_2_frontend_bare_imports_are_declared_dependencies():
    # An import of a package that isn't in package.json is a "Module not found".
    pkg = _package_json()
    declared = set()
    for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        declared |= set(pkg.get(section) or {})
    missing = set()
    for path in _frontend_files("app", "components", "lib", "src"):
        for spec in _IMPORT_RE.findall(_read(path)):
            if spec.startswith((".", "@/", "/", "node:")) or ":" in spec:
                continue
            parts = spec.split("/")
            name = "/".join(parts[:2]) if spec.startswith("@") else parts[0]
            if name not in declared and name not in ("react", "react-dom"):
                missing.add(f"{os.path.relpath(path, FRONTEND_DIR)} -> {name}")
    assert not missing, "imports of undeclared packages: " + "; ".join(sorted(missing))


def test_us_2_frontend_client_components_do_not_export_metadata():
    # Next refuses to compile a "use client" module that exports metadata.
    offenders = []
    for path in _frontend_files("app", os.path.join("src", "app")):
        source = _read(path)
        if _USE_CLIENT_RE.match(source) and _re.search(
            r"export\s+(const\s+metadata\b|(async\s+)?function\s+generateMetadata\b)", source
        ):
            offenders.append(os.path.relpath(path, FRONTEND_DIR))
    assert not offenders, f"'use client' files exporting metadata: {offenders}"


def test_us_2_frontend_pages_using_hooks_are_client_components():
    # A server page or layout calling useState/useEffect fails when compiled.
    offenders = []
    for path in _frontend_files("app", os.path.join("src", "app")):
        if os.path.splitext(os.path.basename(path))[0] not in ("page", "layout", "template"):
            continue
        source = _read(path)
        if _HOOK_RE.search(source) and not _USE_CLIENT_RE.match(source):
            offenders.append(os.path.relpath(path, FRONTEND_DIR))
    assert not offenders, f"hooks used without 'use client': {offenders}"


def test_us_2_frontend_next_config_present_and_dev_script_targets_next():
    pkg = _package_json()
    assert "next dev" in (pkg.get("scripts") or {}).get("dev", ""), "dev script must run `next dev`"
    configs = [f for f in ("next.config.js", "next.config.mjs", "next.config.ts") if os.path.exists(os.path.join(FRONTEND_DIR, f))]
    assert len(configs) <= 1, f"multiple Next configs make startup ambiguous: {configs}"
    for name in configs:
        source = _read(os.path.join(FRONTEND_DIR, name))
        assert "module.exports" in source or "export default" in source, f"{name} exports no config"


def test_us_4_create_form_onblur_does_not_mark_touched_on_either_field():
    # The chosen fix for the blur race: no touch-on-blur on name or requirements,
    # unless every error slot reserves its height up front.
    source = _crown_ai_page()
    reserved = len(_re.findall(r"min-h-", source)) >= 2
    for field in ("proj-name", "proj-reqs"):
        blur = _re.search(r"onBlur=\{([^\n]*)", _input_tag(source, field))
        if blur and "ouched" in blur.group(1):
            assert reserved, f"#{field} is marked touched on blur with no reserved error slot"


def test_us_4_create_form_one_submit_yields_both_field_errors_then_success(client):
    # Backend half of the fixed flow: click -> both errors -> fix -> one project.
    headers, _ = _session_for()
    first = client.post("/projects", json={"name": "", "requirements": ""}, headers=headers)
    assert first.status_code == 422
    assert [tuple(e["loc"]) for e in first.json()["detail"]] == [("body", "name"), ("body", "requirements")]
    created = client.post("/projects", json={"name": "Inventory App", "requirements": "Track stock."}, headers=headers)
    assert created.status_code == 201
    assert [p["id"] for p in client.get("/projects", headers=headers).json()] == [created.json()["id"]]


def test_us_12_every_case_study_link_carries_aria_label():
    # Every <Link> to a case-study detail must have the exact accessible name,
    # not only the first card.
    source = _case_studies_page()
    links = _re.findall(r"<Link\b[^>]*href=\{`/case-studies/\$\{[^}]+\}`\}[^>]*>", source, _re.S)
    if not links:
        pytest.skip("no templated case-study links found")
    for tag in links:
        assert "aria-label" in tag, f"case-study link without aria-label: {tag}"


def test_us_12_case_study_unknown_slug_is_404_from_api(client):
    resp = client.get(f"/case-studies/{uuid.uuid4().hex}")
    assert resp.status_code == 404


# ===========================================================================
# Human-requested coverage (Testing defects pass: blur race on the create
# form, padded case-study link name, ui + performance layers skipped while
# `next dev` sat on "Compiling / ..."):
#   - US-4: the fix moves "touched" to onChange or reserves the error slots;
#     both error <p>s must stay wired to their fields, and the requirements
#     error must not depend on the field ever being visited.
#   - US-12: the case-study link's whole accessible name comes from
#     aria-label (no sr-only span left inside), and the detail page it
#     opens must render under Next 16 (async params, notFound for bad slugs).
#   - Startup: the common causes of a first compile of "/" that never
#     finishes or errors -- a compile-time Google Fonts download, PostCSS /
#     Tailwind plugins that aren't installed or mismatched, a middleware/proxy
#     file with no handler, sync `params` access (removed in Next 16), and a
#     server render that fetches the API with no timeout.
#   - Performance: the load the skipped k6 run would have applied, so the
#     backend half of the NFRs (pages < 2.5s, generation progress < 3s,
#     free-tier caps enforced under load) is still measured.
# ===========================================================================


def _case_study_link_blocks(source):
    return _re.findall(r"<Link\b[^>]*href=\{`/case-studies/\$\{[^}]+\}`\}[^>]*>.*?</Link>", source, _re.S)


def _route_pages_with_dynamic_segments():
    app_dir = _app_dir()
    pages = []
    for root, dirs, files in os.walk(app_dir):
        dirs[:] = [d for d in dirs if d not in ("node_modules", ".next")]
        if "[" not in os.path.relpath(root, app_dir):
            continue
        for f in files:
            if os.path.splitext(f)[0] in ("page", "layout") and f.endswith(_SOURCE_EXTS):
                pages.append(os.path.join(root, f))
    return pages


def _declared_packages():
    pkg = _package_json()
    declared = {}
    for section in ("dependencies", "devDependencies", "optionalDependencies"):
        declared.update(pkg.get(section) or {})
    return declared


def _major(spec):
    m = _re.search(r"(\d+)", spec or "")
    return int(m.group(1)) if m else None


def _percentile(values, pct):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(pct / 100 * (len(ordered) - 1))))]


def _run_parallel(fn, args_list):
    results, errors, lock = [], [], threading.Lock()

    def wrapper(args):
        try:
            out = fn(*args)
            with lock:
                results.append(out)
        except Exception as exc:  # pragma: no cover - reported by the caller
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=wrapper, args=(a,)) for a in args_list]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    return results


# ---------- US-4: create form (static, frontend/app/crown-ai/page.tsx) ----------


def test_us_4_create_form_both_error_paragraphs_are_wired_by_aria_describedby():
    # Each field points at its own error so "[invalid]" + message is announced.
    source = _crown_ai_page()
    for field in ("proj-name", "proj-reqs"):
        tag = _input_tag(source, field)
        m = _re.search(r"aria-describedby=(?:\"([\w-]+)\"|\{[^}]*?[\"'`]([\w-]+)[\"'`])", tag)
        if not m:
            pytest.skip(f"#{field} has no literal aria-describedby id")
        ref = m.group(1) or m.group(2)
        assert f'id="{ref}"' in source or f"id='{ref}'" in source, (
            f"#{field} describes itself with #{ref}, which is never rendered"
        )


def test_us_4_create_form_requirements_textarea_has_no_autofocus():
    # Only the name field autofocuses; two autofocus targets would steal the
    # focus the test expects onCreate to return to #proj-name.
    source = _crown_ai_page()
    assert "autoFocus" not in _input_tag(source, "proj-reqs")


def test_us_4_create_form_requirements_error_not_gated_on_touch_only():
    # The UI test never visits the requirements field, so a guard of
    # `formTouched.requirements && ...` alone would hide the error forever.
    source = _crown_ai_page()
    for m in _re.finditer(r"\{([^{}]*?)&&\s*\(?\s*<p\b[^>]*>[^<]*Describe what you want to build", source, _re.S):
        guard = m.group(1)
        if "ouched" in guard:
            assert "ubmitted" in guard, f"requirements error only shown when {guard.strip()}"


def test_us_4_create_form_name_error_not_gated_on_blur_touch_only():
    source = _crown_ai_page()
    for m in _re.finditer(r"\{([^{}]*?)&&\s*\(?\s*<p\b[^>]*>[^<]*Give your project a name", source, _re.S):
        guard = m.group(1)
        if "ouched" in guard:
            assert "ubmitted" in guard, f"name error only shown when {guard.strip()}"


def test_us_4_create_form_reserved_error_slots_wrap_both_messages():
    # If the slot route was chosen, the min-height must sit on the element
    # around each error, not somewhere unrelated in the page.
    source = _crown_ai_page()
    if not _re.search(r"onBlur=\{[^\n]*ouched", source):
        pytest.skip("touched is not set on blur; reserved slots not required")
    for msg in ("Give your project a name", "Describe what you want to build"):
        idx = source.find(msg)
        assert idx != -1, f"missing error text: {msg}"
        window = source[max(0, idx - 400) : idx]
        assert "min-h-" in window, f"error '{msg}' is not inside a reserved min-height slot"


# ---------- US-4: create form (backend half of the same flow) ----------


@pytest.mark.parametrize(
    "body,expected",
    [
        ({"name": "x" * 200, "requirements": "ok"}, 201),
        ({"name": "x" * 201, "requirements": "ok"}, 422),
        ({"name": "ok", "requirements": "y" * 8000}, 201),
        ({"name": "ok", "requirements": "y" * 8001}, 422),
        ({"name": "a", "requirements": "b"}, 201),
    ],
)
def test_us_4_create_form_length_boundaries(client, body, expected):
    headers, _ = _session_for()
    assert client.post("/projects", json=body, headers=headers).status_code == expected


def test_us_4_create_form_name_only_typed_then_submitted_keeps_name_value_on_retry(client):
    # The user typed a name, submitted, saw the requirements error, then filled
    # it: the saved project keeps the trimmed name they typed first.
    headers, _ = _session_for()
    first = client.post("/projects", json={"name": "  Ledger  ", "requirements": ""}, headers=headers)
    assert first.status_code == 422
    assert _validation_locs(first) == {("body", "requirements")}
    project = _create_project(client, headers, name="  Ledger  ", requirements="Double-entry books.")
    assert project["name"] == "Ledger"
    assert project["requirements"] == "Double-entry books."


def test_us_4_create_form_double_submit_then_generate_each_independently(client):
    headers, _ = _session_for()
    _set_tier(headers, client, "mid")
    a = _create_project(client, headers, name="Twice")
    b = _create_project(client, headers, name="Twice")
    assert a["id"] != b["id"]
    _generate_all(client, headers, a["id"], ALL_STAGES[:1])
    assert client.get(f"/projects/{b['id']}/artifacts", headers=headers).json() == []


@pytest.mark.parametrize("path", ["/projects/{pid}", "/projects/{pid}/artifacts"])
def test_us_4_create_form_unknown_project_reads_are_404(client, path):
    headers, _ = _session_for()
    assert client.get(path.format(pid=uuid.uuid4().hex), headers=headers).status_code == 404


def test_us_4_create_form_unknown_project_download_is_404(client):
    headers, _ = _session_for()
    assert client.post(f"/projects/{uuid.uuid4().hex}/download", headers=headers).status_code == 404


# ---------- US-12: case-study link name and detail page ----------


def test_us_12_case_study_link_contains_no_sr_only_span():
    blocks = _case_study_link_blocks(_case_studies_page())
    if not blocks:
        pytest.skip("no templated case-study links found")
    for block in blocks:
        assert "sr-only" not in block, "sr-only span inside the link pads the accessible name"


def test_us_12_case_study_aria_label_has_nothing_after_client():
    # "View case study: Regional NBFC (Chennai)" -- no arrow, no trailing space.
    source = _case_studies_page()
    labels = _re.findall(r"aria-label=\{`(View case study:[^`]*)`\}", source)
    if not labels:
        pytest.skip("aria-label not written as a template literal")
    for label in labels:
        assert _re.fullmatch(r"View case study: \$\{[^}]+\}", label), f"unexpected label shape: {label!r}"


def test_us_12_case_study_label_matches_ui_test_regex_for_featured_client():
    label = "View case study: {client}".format(client="Regional NBFC (Chennai)")
    assert _re.search(r"View case study: Regional NBFC \(Chennai\)", label)
    padded = "View case study : Regional NBFC (Chennai) →"
    assert not _re.search(r"View case study: Regional NBFC \(Chennai\)", padded)


def test_us_12_case_study_visible_arrow_does_not_leak_into_label():
    blocks = _case_study_link_blocks(_case_studies_page())
    if not blocks:
        pytest.skip("no templated case-study links found")
    for block in blocks:
        assert "aria-label" in block or 'aria-hidden="true"' in block, (
            "the visible arrow becomes part of the name unless aria-label or aria-hidden covers it"
        )


def _case_study_detail_page():
    app_dir = _app_dir()
    path = _first_existing(os.path.join(app_dir, "case-studies", "[slug]"), "page")
    if not path:
        pytest.skip("case-study detail page not present")
    return _read(path)


def test_us_12_case_study_detail_unknown_slug_calls_not_found():
    source = _case_study_detail_page()
    assert "notFound(" in source, "an unknown slug must render the 404 page, not crash"


def test_us_12_case_study_detail_shows_client_story_and_sample_output():
    source = _case_study_detail_page()
    assert _re.search(r"\.client\b", source), "detail page never renders the client name"
    assert _re.search(r"(?i)sample|output", source), "detail page has no sample output section"


# ---------- Startup: what makes "Compiling / ..." hang or fail ----------


def test_us_2_frontend_root_layout_does_not_download_google_fonts_at_compile():
    # next/font/google fetches from fonts.googleapis.com during the first
    # compile; offline or on a slow link "Compiling / ..." never completes.
    app_dir = _app_dir()
    for stem in ("layout", "page"):
        path = _first_existing(app_dir, stem)
        if path:
            assert "next/font/google" not in _read(path), (
                f"app/{os.path.basename(path)} downloads Google Fonts at compile time; "
                "use next/font/local or a system font stack"
            )


def test_us_2_frontend_dynamic_route_pages_await_params():
    # Next 16 removed synchronous `params.x` access in pages/layouts.
    offenders = []
    for path in _route_pages_with_dynamic_segments():
        source = _read(path)
        if _re.search(r"\bparams\.\w+", source) and not _re.search(r"await\s+params|use\(\s*params\s*\)", source):
            offenders.append(os.path.relpath(path, FRONTEND_DIR))
    assert not offenders, f"sync params access (Next 16 error): {offenders}"


def test_us_2_frontend_postcss_plugins_are_installed():
    configs = [
        os.path.join(FRONTEND_DIR, f)
        for f in ("postcss.config.js", "postcss.config.mjs", "postcss.config.cjs")
        if os.path.exists(os.path.join(FRONTEND_DIR, f))
    ]
    if not configs:
        pytest.skip("no PostCSS config")
    declared = _declared_packages()
    for path in configs:
        names = _re.findall(r"[\"']((?:@[\w.-]+/)?[\w.-]+)[\"']\s*:", _read(path))
        names += _re.findall(r"require\(\s*[\"']([^\"']+)[\"']\s*\)", _read(path))
        missing = [n for n in names if n not in ("plugins",) and n not in declared]
        assert not missing, f"{os.path.basename(path)} uses plugins not in package.json: {missing}"


def test_us_2_frontend_tailwind_css_entry_matches_installed_major():
    declared = _declared_packages()
    if "tailwindcss" not in declared:
        pytest.skip("tailwindcss not used")
    major = _major(declared["tailwindcss"])
    css_files = []
    for sub in ("app", "styles", os.path.join("src", "app")):
        base = os.path.join(FRONTEND_DIR, sub)
        if os.path.isdir(base):
            css_files += [os.path.join(base, f) for f in os.listdir(base) if f.endswith(".css")]
    if not css_files:
        pytest.skip("no global CSS file")
    for path in css_files:
        css = _read(path)
        if major is not None and major >= 4:
            assert "@tailwind base" not in css, f"{os.path.basename(path)} uses v3 directives with tailwindcss v{major}"
        elif major == 3:
            assert '@import "tailwindcss"' not in css, f"{os.path.basename(path)} uses v4 import with tailwindcss v3"
    if major is not None and major >= 4:
        assert "@tailwindcss/postcss" in declared, "tailwindcss v4 needs @tailwindcss/postcss installed"


def test_us_2_frontend_middleware_or_proxy_exports_a_handler():
    found = False
    for base in (FRONTEND_DIR, os.path.join(FRONTEND_DIR, "src")):
        for stem in ("middleware", "proxy"):
            for ext in (".ts", ".js"):
                path = os.path.join(base, stem + ext)
                if os.path.exists(path):
                    found = True
                    source = _read(path)
                    assert _re.search(
                        rf"export\s+(default|(async\s+)?function\s+{stem}\b|const\s+{stem}\b)", source
                    ), f"{os.path.relpath(path, FRONTEND_DIR)} exports no {stem} handler"
    if not found:
        pytest.skip("no middleware/proxy file")


def test_us_2_frontend_server_root_render_does_not_block_on_api_without_timeout():
    # A server-rendered "/" that awaits the backend with no timeout sits on
    # "Compiling / ..." whenever the API is slow or not up yet.
    app_dir = _app_dir()
    for stem in ("layout", "page"):
        path = _first_existing(app_dir, stem)
        if not path:
            continue
        source = _read(path)
        if _USE_CLIENT_RE.match(source) or "fetch(" not in source:
            continue
        assert _re.search(r"signal|timeout|AbortSignal", source), (
            f"app/{os.path.basename(path)} fetches during server render with no timeout"
        )


# ---------- Performance: the load the skipped k6 run would have applied ----------


def test_us_2_perf_sustained_marketing_load_p95_within_budget(client):
    jobs = [("/pricing/plans", None), ("/consent/policy", {"country": "DE"}), ("/health", None)] * 20

    def fetch(path, params):
        start = time.perf_counter()
        r = client.get(path, params=params)
        return r.status_code, time.perf_counter() - start

    results = _run_parallel(fetch, jobs)
    assert len(results) == len(jobs)
    assert all(code == 200 for code, _ in results), [c for c, _ in results if c != 200]
    assert _percentile([d for _, d in results], 95) < MARKETING_BUDGET_S


def test_us_9_perf_concurrent_contact_submissions_all_captured(client):
    def submit(i):
        r = client.post("/contact", json={"name": f"Visitor {i}", "email": f"v{i}@example.com", "message": "Hello"})
        return r.status_code, r.json().get("id")

    results = _run_parallel(submit, [(i,) for i in range(15)])
    assert all(code == 201 for code, _ in results), results
    assert len({rid for _, rid in results}) == 15


def test_us_4_perf_concurrent_generation_shows_progress_within_budget(client):
    # Several paid users each start stage 1 at once; every one gets its
    # artifact back inside the 3s progress budget.
    sessions = []
    for _ in range(5):
        headers, _ = _session_for()
        _set_tier(headers, client, "mid")
        sessions.append((headers, _create_project(client, headers)["id"]))

    def gen(headers, pid):
        start = time.perf_counter()
        r = client.post(f"/projects/{pid}/generate/requirements", headers=headers)
        return r.status_code, time.perf_counter() - start

    results = _run_parallel(gen, sessions)
    assert all(code == 200 for code, _ in results), results
    assert max(d for _, d in results) < GENERATION_PROGRESS_BUDGET_S


def test_us_4_perf_free_tier_cap_holds_under_burst_with_no_5xx(client):
    headers, _ = _session_for()
    pid = _create_project(client, headers)["id"]

    def gen():
        return client.post(f"/projects/{pid}/generate/requirements", headers=headers).status_code

    codes = _run_parallel(gen, [()] * (FREE_LIMIT * 2))
    assert all(c < 500 for c in codes), codes
    assert codes.count(200) == FREE_LIMIT
    assert codes.count(429) == FREE_LIMIT
    assert len(client.get(f"/projects/{pid}/artifacts", headers=headers).json()) == FREE_LIMIT


def test_us_5_perf_blocked_downloads_under_load_stay_402(client):
    headers, _ = _session_for()
    pid = _create_project(client, headers)["id"]
    _generate_all(client, headers, pid, ALL_STAGES[:1])

    def download():
        r = client.post(f"/projects/{pid}/download", headers=headers)
        return r.status_code, r.json()["detail"]["upgrade_url"] if r.status_code == 402 else None

    results = _run_parallel(download, [()] * 10)
    assert results == [(402, "/pricing")] * 10


# ===========================================================================
# Human-requested coverage (Testing defects pass: blur race on the create
# form, padded case-study link name, `next dev` printed "Ready", then
# "Slow filesystem detected", then sat on "Compiling / ..." until the ui
# layer timed out):
#   - US-4: onCreate must set formSubmitted BEFORE it bails out on invalid
#     input (otherwise the click lands but still shows no requirements error),
#     and must refocus #proj-name on that invalid path.
#   - US-12: the link's aria-label and href must come from the same card.
#   - Startup: missing files the first compile of "/" needs (public assets,
#     local fonts, CSS @imports), a next.config the loader can't evaluate
#     (ESM/CJS mismatch), a webpack hook Turbopack won't run, remote <Image>
#     with no allowlist, and a build cache that ends up committed. On the
#     backend side, a slow disk (the 352ms benchmark) must not push boot or
#     the first workspace calls past their budgets: connections per boot and
#     per request stay small and bounded.
# ===========================================================================

SLOW_FS_CONNECT_DELAY_S = 0.35  # the slow-filesystem benchmark from the log


def _on_create_body(source):
    m = _re.search(r"(?:function\s+onCreate\s*\(|const\s+onCreate\s*=)", source)
    if not m:
        pytest.skip("onCreate not found in crown-ai/page.tsx")
    return source[m.start() : m.start() + 3000]


def _validation_bailout(body):
    """First `if (<invalid input>) { ... return` in onCreate -- skipping the
    double-submit guard (`if (creating) return`), which may run first."""
    for m in _re.finditer(r"if\s*\(([^)]*)\)\s*\{?", body):
        cond = m.group(1)
        if _re.search(r"creating|submitting|busy|loading|pending", cond):
            continue
        if not _re.search(r"trim|name|req|valid|[eE]rror", cond):
            continue
        ret = body.find("return", m.end())
        if ret != -1:
            return m.start(), ret
    return None


def test_us_4_create_form_submitted_flag_set_before_invalid_input_bailout():
    # The click now reaches the button; if onCreate returns on the empty
    # requirements before setFormSubmitted(true), the error still never shows.
    body = _on_create_body(_crown_ai_page())
    bailout = _validation_bailout(body)
    if not bailout:
        pytest.skip("onCreate has no early return on invalid input")
    submitted = _re.search(r"setFormSubmitted\(\s*true\s*\)", body)
    assert submitted, "onCreate never sets formSubmitted"
    assert submitted.start() < bailout[0], "formSubmitted is set only after the invalid-input return"


def test_us_4_create_form_invalid_path_refocuses_name_before_returning():
    body = _on_create_body(_crown_ai_page())
    bailout = _validation_bailout(body)
    if not bailout:
        pytest.skip("onCreate has no early return on invalid input")
    branch = body[bailout[0] : bailout[1]]
    assert ".focus(" in branch or "proj-name" in branch, "invalid submit must move focus back to #proj-name"


def test_us_4_create_form_double_submit_guard_does_not_swallow_first_invalid_click():
    # A busy guard is fine, but it must not be set before validation --
    # otherwise the first (invalid) click leaves the form stuck as "creating".
    body = _on_create_body(_crown_ai_page())
    bailout = _validation_bailout(body)
    set_busy = _re.search(r"set(Creating|Submitting|Busy|Loading|Pending)\(\s*true\s*\)", body)
    if not bailout or not set_busy:
        pytest.skip("no busy flag or no validation bailout in onCreate")
    assert set_busy.start() > bailout[1], "busy flag is set before validation and never cleared on the invalid path"


def test_us_4_create_form_invalid_click_then_valid_click_creates_exactly_one(client):
    # Backend half: invalid submit, then the corrected submit, then an
    # accidental second click -- the workspace shows the projects that were
    # actually accepted and nothing from the rejected one.
    headers, _ = _session_for()
    assert client.post("/projects", json={"name": "Shop", "requirements": ""}, headers=headers).status_code == 422
    ok = [client.post("/projects", json={"name": "Shop", "requirements": "Sell things."}, headers=headers) for _ in range(2)]
    assert [r.status_code for r in ok] == [201, 201]
    listed = client.get("/projects", headers=headers).json()
    assert {p["id"] for p in listed} == {r.json()["id"] for r in ok}
    assert all(p["requirements"] == "Sell things." for p in listed)


def test_us_12_case_study_aria_label_and_href_come_from_same_card():
    for block in _case_study_link_blocks(_case_studies_page()) or [pytest.skip("no templated case-study links")]:
        href = _re.search(r"href=\{`/case-studies/\$\{\s*(\w+)\.", block)
        label = _re.search(r"aria-label=\{`View case study: \$\{\s*(\w+)\.", block)
        if not (href and label):
            pytest.skip("href/aria-label not written as member templates")
        assert href.group(1) == label.group(1), "label names a different item than the link opens"


def test_us_12_case_study_link_not_hidden_from_accessibility_tree():
    for block in _case_study_link_blocks(_case_studies_page()) or [pytest.skip("no templated case-study links")]:
        open_tag = block.split(">", 1)[0]
        assert 'aria-hidden="true"' not in open_tag and "aria-hidden={true}" not in open_tag
        assert "aria-labelledby" not in open_tag, "aria-labelledby would override the aria-label"


# ---------- Startup: missing files / config the first compile of "/" needs ----------


_ASSET_LITERAL_RE = _re.compile(r"""["'](/[\w./-]+\.(?:svg|png|jpe?g|webp|gif|ico|avif))["']""")


def test_us_3_frontend_referenced_public_assets_exist():
    # The shared logo (US-3) and every other root-relative asset must exist,
    # or "/" renders broken images / the metadata icon 404s.
    missing = set()
    for path in _frontend_files("app", "components", "lib", "src"):
        for ref in _ASSET_LITERAL_RE.findall(_read(path)):
            if ref.startswith("//"):
                continue  # protocol-relative URL, not a public/ file
            rel = ref.lstrip("/")
            candidates = [os.path.join(FRONTEND_DIR, "public", rel), os.path.join(_app_dir(), rel)]
            if not any(os.path.exists(c) for c in candidates):
                missing.add(f"{os.path.relpath(path, FRONTEND_DIR)} -> {ref}")
    assert not missing, "assets referenced but not present: " + "; ".join(sorted(missing))


def test_us_2_frontend_local_font_files_exist():
    missing = []
    for path in _frontend_files("app", "components", "lib", "src"):
        source = _read(path)
        if "next/font/local" not in source:
            continue
        for ref in _re.findall(r"""(?:src|path)\s*:\s*["'](\.{1,2}/[^"']+\.(?:woff2?|ttf|otf))["']""", source):
            if not os.path.exists(os.path.normpath(os.path.join(os.path.dirname(path), ref))):
                missing.append(f"{os.path.relpath(path, FRONTEND_DIR)} -> {ref}")
    assert not missing, f"next/font/local files missing (compile error): {missing}"


def test_us_2_frontend_css_relative_imports_resolve():
    if not os.path.isdir(FRONTEND_DIR):
        pytest.skip("frontend not present")
    broken = []
    for root, dirs, files in os.walk(FRONTEND_DIR):
        dirs[:] = [d for d in dirs if d not in ("node_modules", ".next", ".git")]
        for f in files:
            if not f.endswith(".css"):
                continue
            path = os.path.join(root, f)
            for ref in _re.findall(r"""@import\s+(?:url\()?["'](\.{1,2}/[^"']+)["']""", _read(path)):
                if not os.path.exists(os.path.normpath(os.path.join(root, ref))):
                    broken.append(f"{os.path.relpath(path, FRONTEND_DIR)} -> {ref}")
    assert not broken, f"CSS @import targets missing: {broken}"


def _next_config():
    for name in ("next.config.js", "next.config.mjs", "next.config.ts", "next.config.cjs"):
        path = os.path.join(FRONTEND_DIR, name)
        if os.path.exists(path):
            return name, _read(path)
    pytest.skip("no next.config")


def test_us_2_frontend_next_config_module_format_matches_package_type():
    # `export default` in a CJS next.config.js (or module.exports in .mjs)
    # fails while loading config, before "/" can compile.
    name, source = _next_config()
    is_esm_pkg = _package_json().get("type") == "module"
    if name == "next.config.mjs" or (name == "next.config.js" and is_esm_pkg):
        assert "module.exports" not in source, f"{name} is ESM but uses module.exports"
    if name == "next.config.cjs" or (name == "next.config.js" and not is_esm_pkg):
        assert not _re.search(r"^\s*export\s+default\b", source, _re.M), f"{name} is CommonJS but uses export default"
        assert not _re.search(r"^\s*import\s+\S", source, _re.M), f"{name} is CommonJS but uses import"


def test_us_2_frontend_custom_webpack_config_not_silently_skipped_by_turbopack():
    # Next 16 dev runs Turbopack; a `webpack:` hook is not applied there, so
    # whatever it set up (aliases, loaders) is missing on the first compile.
    _, source = _next_config()
    if not _re.search(r"\bwebpack\s*[:(]", source):
        return
    dev = (_package_json().get("scripts") or {}).get("dev", "")
    assert "--webpack" in dev, "next.config has a webpack hook but `next dev` uses Turbopack"


def test_us_2_frontend_remote_images_are_allowlisted():
    # next/image throws at render for a remote host not in images config.
    remote = []
    for path in _frontend_files("app", "components", "src"):
        source = _read(path)
        if "next/image" in source and _re.search(r"<Image\b[^>]*src=[\"']https?://", source, _re.S):
            remote.append(os.path.relpath(path, FRONTEND_DIR))
    if not remote:
        return
    _, source = _next_config()
    assert _re.search(r"remotePatterns|domains|unoptimized", source), f"remote <Image> with no allowlist in {remote}"


def test_us_2_frontend_next_config_dist_dir_stays_inside_project():
    # A distDir on another drive/UNC path is exactly the "slow filesystem"
    # Next warns about and makes the first compile crawl.
    _, source = _next_config()
    m = _re.search(r"""distDir\s*:\s*["']([^"']+)["']""", source)
    if m:
        value = m.group(1)
        assert not (os.path.isabs(value) or value.startswith(("\\\\", "//", ".."))), f"distDir points outside: {value}"


def test_us_2_frontend_build_cache_is_gitignored():
    # A committed .next/ (or node_modules/) is stale state the dev server
    # has to reconcile on every start.
    path = os.path.join(FRONTEND_DIR, ".gitignore")
    root_path = os.path.join(os.path.dirname(BACKEND_DIR), ".gitignore")
    ignores = "".join(_read(p) for p in (path, root_path) if os.path.exists(p))
    if not ignores:
        pytest.skip("no .gitignore")
    assert _re.search(r"(^|/)\.next/?\s*$", ignores, _re.M), ".next is not gitignored"
    assert _re.search(r"(^|/)node_modules/?\s*$", ignores, _re.M), "node_modules is not gitignored"


def test_us_2_frontend_dev_script_port_matches_ui_runner():
    # The runner waits on :3020 (from the log); a different port means it
    # waits forever for a server that is up elsewhere.
    dev = (_package_json().get("scripts") or {}).get("dev", "")
    m = _re.search(r"(?:-p|--port)[ =](\d+)", dev)
    if m:
        assert m.group(1) == "3020", f"dev server on :{m.group(1)}, runner expects :3020"


# ---------- Startup: backend on a slow filesystem ----------


@pytest.fixture
def slow_fs(monkeypatch):
    """Every SQLite open costs what the log's slow-filesystem benchmark did;
    returns a counter of how many opens happened."""
    import sqlite3

    real_connect = sqlite3.connect
    counter = {"n": 0}

    def slow_connect(*args, **kwargs):
        counter["n"] += 1
        time.sleep(SLOW_FS_CONNECT_DELAY_S)
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(db_module.sqlite3, "connect", slow_connect)
    return counter


def test_us_1_ui_readiness_boot_opens_few_connections(fresh_db_path, slow_fs):
    with TestClient(app):
        pass
    assert 1 <= slow_fs["n"] <= 3, f"boot opened {slow_fs['n']} DB connections"


def test_us_1_ui_readiness_boot_on_slow_filesystem_within_budget(fresh_db_path, slow_fs):
    start = time.perf_counter()
    with TestClient(app) as c:
        elapsed = time.perf_counter() - start
        assert c.get("/health").json() == {"status": "ok"}
    assert elapsed < MARKETING_BUDGET_S, f"boot took {elapsed:.2f}s on a slow disk"


def test_us_1_ui_readiness_health_and_plans_do_not_touch_slow_disk(fresh_db_path, slow_fs):
    with TestClient(app) as c:
        before = slow_fs["n"]
        assert c.get("/health").status_code == 200
        assert c.get("/pricing/plans").status_code == 200
        assert c.get("/consent/policy", params={"country": "IN"}).status_code == 200
        assert slow_fs["n"] == before, "readiness/marketing endpoints opened the database"


def test_us_4_create_and_first_stage_on_slow_filesystem_within_budget(fresh_db_path, slow_fs):
    with TestClient(app) as c:
        headers, _ = _session_for()
        before = slow_fs["n"]
        start = time.perf_counter()
        resp = c.post("/projects", json={"name": "Slow", "requirements": "On a slow disk."}, headers=headers)
        assert resp.status_code == 201
        assert slow_fs["n"] - before <= 3, f"create opened {slow_fs['n'] - before} connections"
        gen = c.post(f"/projects/{resp.json()['id']}/generate/requirements", headers=headers)
        elapsed = time.perf_counter() - start
    assert gen.status_code == 200
    assert elapsed < 2 * GENERATION_PROGRESS_BUDGET_S, f"create + first stage took {elapsed:.2f}s"


def test_us_4_create_form_validation_error_on_slow_filesystem_skips_db_write(fresh_db_path, slow_fs):
    # An invalid submit is rejected before the handler opens a connection,
    # so the form's error state appears fast even on a slow disk.
    with TestClient(app) as c:
        headers, _ = _session_for()
        before = slow_fs["n"]
        resp = c.post("/projects", json={"name": "", "requirements": ""}, headers=headers)
        assert resp.status_code == 422
        assert slow_fs["n"] - before <= 1, "validation failure still opened the database for the write"


# ===========================================================================
# Human-requested coverage (Testing defects pass: blur race on the create
# form, padded case-study link name, `next dev` "Ready" but stuck on
# "Compiling / ..." on a slow \\?\D:\ path). Gaps left by the passes above:
#   - Startup: why a first compile crawls on a slow disk. Turbopack picking
#     a workspace root above frontend/ (a stray lockfile up the tree) and then
#     watching the whole repo, Tailwind / tsconfig globs that scan
#     node_modules or the backend, and frontend imports that reach outside
#     frontend/ all multiply the file I/O the 352ms benchmark warned about.
#   - US-4: the button must sit in the same <form> as the fields so the click
#     submits it, and the backend must flag only the missing field for every
#     way a half-filled form can be serialised (empty, null, key absent).
#   - US-12: aria-label must sit on the <Link> itself (not an inner element),
#     and every case-study slug the list links to must be unique and URL-safe.
# Frontend sources are read statically; tests skip when they are absent.
# ===========================================================================

_LOCKFILES = ("package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb", "bun.lock")


def _parent_lockfiles():
    """Lockfiles in directories above frontend/ -- what Turbopack's root
    inference walks up to."""
    found = []
    current = os.path.dirname(os.path.abspath(FRONTEND_DIR))
    while True:
        found.extend(os.path.join(current, f) for f in _LOCKFILES if os.path.isfile(os.path.join(current, f)))
        parent = os.path.dirname(current)
        if parent == current:
            return found
        current = parent


def test_us_2_frontend_turbopack_root_pinned_when_lockfile_above_frontend():
    # With a lockfile higher up, Next picks that directory as the workspace
    # root and watches/compiles from there -- on a slow disk "/" never finishes.
    _, source = _next_config()
    above = _parent_lockfiles()
    if not above:
        pytest.skip("no lockfile above frontend/; root inference is unambiguous")
    assert _re.search(r"turbopack\s*:\s*\{[^}]*\broot\s*:", source, _re.S) or "outputFileTracingRoot" in source, (
        f"lockfile(s) above frontend/ ({above}) but next.config does not pin turbopack.root"
    )


def test_us_2_frontend_turbopack_root_points_at_frontend_not_repo():
    _, source = _next_config()
    m = _re.search(r"turbopack\s*:\s*\{[^}]*\broot\s*:\s*([^,\n}]+)", source, _re.S)
    if not m:
        pytest.skip("turbopack.root not set")
    expr = m.group(1).strip()
    assert ".." not in expr, f"turbopack.root climbs out of frontend/: {expr}"
    if expr.startswith(("'", '"')):
        value = expr.strip("'\"")
        assert not os.path.isabs(value), f"turbopack.root is an absolute path ({value}); breaks on other machines"


def _tailwind_config():
    for name in ("tailwind.config.js", "tailwind.config.ts", "tailwind.config.cjs", "tailwind.config.mjs"):
        path = os.path.join(FRONTEND_DIR, name)
        if os.path.exists(path):
            return name, _read(path)
    return None, None


def test_us_2_frontend_tailwind_content_globs_stay_inside_sources():
    # `./**/*.{js,tsx}` or any `node_modules` / `../` glob makes Tailwind scan
    # tens of thousands of files on every compile of "/".
    if not os.path.isdir(FRONTEND_DIR):
        pytest.skip("frontend not present")
    name, source = _tailwind_config()
    globs = []
    if source:
        m = _re.search(r"content\s*:\s*\[(.*?)\]", source, _re.S)
        if m:
            globs += [(name, g) for g in _re.findall(r"[\"']([^\"']+)[\"']", m.group(1))]
    for sub in ("app", "styles", os.path.join("src", "app")):
        base = os.path.join(FRONTEND_DIR, sub)
        if os.path.isdir(base):
            for f in os.listdir(base):
                if f.endswith(".css"):
                    globs += [(f, g) for g in _re.findall(r"""@source\s+["']([^"']+)["']""", _read(os.path.join(base, f)))]
    if not globs:
        pytest.skip("no Tailwind content globs or @source directives")
    for origin, glob in globs:
        assert "node_modules" not in glob, f"{origin}: Tailwind scans node_modules via {glob!r}"
        assert not _re.match(r"^\.\./\.\./|^/", glob), f"{origin}: Tailwind scans outside frontend/ via {glob!r}"
        assert not _re.match(r"^(\./)?\*\*/", glob), f"{origin}: root-wide glob {glob!r} also matches node_modules/.next"


def test_us_2_frontend_tsconfig_scope_stays_inside_frontend():
    path = os.path.join(FRONTEND_DIR, "tsconfig.json")
    if not os.path.exists(path):
        pytest.skip("no tsconfig.json")
    text = _read(path)
    text = _re.sub(r"/\*.*?\*/", "", text, flags=_re.S)
    text = _re.sub(r"(^|[^:\"])//[^\n]*", r"\1", text)
    text = _re.sub(r",(\s*[}\]])", r"\1", text)
    try:
        config = _json.loads(text)
    except ValueError:
        pytest.fail("tsconfig.json is not valid JSON(C); Next fails to read it at startup")
    for entry in config.get("include") or []:
        assert not entry.startswith(".."), f"tsconfig include reaches outside frontend/: {entry}"
    exclude = config.get("exclude")
    if exclude is not None:
        assert "node_modules" in exclude, "explicit tsconfig exclude drops the default node_modules exclusion"


def test_us_2_frontend_sources_do_not_import_outside_frontend():
    # An import like "../../backend/..." widens the module graph (and the
    # watched tree) to the whole repo, and fails outright once deployed.
    root = os.path.abspath(FRONTEND_DIR)
    escaping = []
    for path in _frontend_files("app", "components", "lib", "src"):
        for spec in _IMPORT_RE.findall(_read(path)):
            if spec.startswith(("./", "../")):
                target = os.path.abspath(os.path.join(os.path.dirname(path), spec))
                if os.path.commonpath([root, target]) != root:
                    escaping.append(f"{os.path.relpath(path, FRONTEND_DIR)} -> {spec}")
    assert not escaping, "imports outside frontend/: " + "; ".join(escaping)


def test_us_2_frontend_next_config_does_not_force_watch_polling():
    # Polling watchers on a slow/network drive make every compile crawl.
    _, source = _next_config()
    assert not _re.search(r"\bpoll\s*:\s*\d", source), "next.config forces file-watch polling"
    dev = (_package_json().get("scripts") or {}).get("dev", "")
    assert "WATCHPACK_POLLING" not in dev and "CHOKIDAR_USEPOLLING" not in dev, "dev script forces polling watchers"


# ---------- US-4: form wiring + backend half of a half-filled form ----------


def test_us_4_create_form_submit_button_inside_same_form_as_fields():
    # The click only fires onSubmit (-> onCreate) if the button is in the form.
    source = _crown_ai_page()
    for m in _re.finditer(r"<form\b[^>]*onSubmit=\{[^}]*\}[^>]*>(.*?)</form>", source, _re.S):
        body = m.group(1)
        if "Generate Project" in body:
            assert "proj-name" in body and "proj-reqs" in body, "Generate Project submits a form without the fields"
            return
    pytest.fail("Generate Project button is not inside an onSubmit <form>")


@pytest.mark.parametrize(
    "body,flagged",
    [
        ({"name": "Inventory App"}, {("body", "requirements")}),
        ({"name": "Inventory App", "requirements": None}, {("body", "requirements")}),
        ({"requirements": "Track stock."}, {("body", "name")}),
        ({"name": None, "requirements": "Track stock."}, {("body", "name")}),
        ({"name": None, "requirements": None}, {("body", "name"), ("body", "requirements")}),
    ],
)
def test_us_4_create_form_half_filled_form_flags_exactly_missing_fields(client, body, flagged):
    headers, _ = _session_for()
    resp = client.post("/projects", json=body, headers=headers)
    assert resp.status_code == 422
    assert _validation_locs(resp) == flagged
    assert client.get("/projects", headers=headers).json() == []


def test_us_4_create_form_valid_submit_after_invalid_trims_and_lists_newest_first(client):
    headers, _ = _session_for()
    assert client.post("/projects", json={"name": "Old", "requirements": ""}, headers=headers).status_code == 422
    first = _create_project(client, headers, name="First", requirements="One.")
    time.sleep(0.01)
    second = _create_project(client, headers, name="\tSecond \n", requirements="  Two.  ")
    assert (second["name"], second["requirements"]) == ("Second", "Two.")
    assert [p["id"] for p in client.get("/projects", headers=headers).json()] == [second["id"], first["id"]]


def test_us_4_create_form_other_users_project_is_403_not_404(client):
    owner, _ = _session_for()
    intruder, _ = _session_for()
    project = _create_project(client, owner)
    assert client.get(f"/projects/{project['id']}", headers=intruder).status_code == 403
    assert client.post(f"/projects/{uuid.uuid4().hex}/generate/requirements", headers=intruder).status_code == 404


# ---------- US-12: aria-label placement and slug hygiene ----------


def test_us_12_case_study_aria_label_on_link_open_tag():
    # aria-label on an inner <span> does not name the link.
    blocks = _case_study_link_blocks(_case_studies_page())
    if not blocks:
        pytest.skip("no templated case-study links found")
    for block in blocks:
        open_tag = _re.match(r"<Link\b.*?>", block, _re.S).group(0)
        assert "aria-label" in open_tag, f"aria-label is not on the <Link> itself: {open_tag}"


def _case_study_slugs():
    if not os.path.isdir(FRONTEND_DIR):
        pytest.skip("frontend not present")
    for root, dirs, files in os.walk(FRONTEND_DIR):
        dirs[:] = [d for d in dirs if d not in ("node_modules", ".next", ".git")]
        for f in files:
            if f.endswith((".ts", ".tsx", ".js", ".json")):
                source = _read(os.path.join(root, f))
                if "Regional NBFC (Chennai)" in source:
                    slugs = _re.findall(r"""["']?slug["']?\s*:\s*["']([^"']+)["']""", source)
                    if slugs:
                        return slugs
    pytest.skip("case-study data with literal slugs not found")


def test_us_12_case_study_slugs_unique_and_url_safe():
    slugs = _case_study_slugs()
    assert len(slugs) == len(set(slugs)), f"duplicate case-study slugs: {slugs}"
    for slug in slugs:
        assert _re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug), f"slug {slug!r} is not URL-safe"


def test_us_12_case_study_slugs_not_served_by_api(client):
    for slug in _case_study_slugs():
        resp = client.get(f"/case-studies/{slug}")
        assert resp.status_code == 404 and resp.headers["content-type"].startswith("application/json")


# ===========================================================================
# Human-requested coverage (Testing defects pass: tailwindcss ^4 with no
# @tailwindcss/postcss, a hand-rolled frontend/tailwind-postcss.js standing in
# for it, and `next dev` stuck on "Compiling / ..."). What must hold once the
# fix lands:
#   - @tailwindcss/postcss is a devDependency on the same major as tailwindcss;
#   - postcss.config.js is exactly the standard v4 setup -- the official plugin,
#     no local shim, no v3 `tailwindcss: {}` key, no autoprefixer;
#   - frontend/tailwind-postcss.js is gone and nothing references it;
#   - package-lock.json was regenerated (`npm install`) and resolves the
#     plugin on a tailwindcss-compatible version, with autoprefixer dropped;
#   - globals.css keeps the v4 entry (@import "tailwindcss" first, @config
#     pointing at a tailwind.config that exists and loads in this module mode).
# `npm run build` runs only when CROWNAI_RUN_FRONTEND_BUILD=1 (slow).
# ===========================================================================

TAILWIND_POSTCSS = "@tailwindcss/postcss"
_POSTCSS_CONFIG_NAMES = ("postcss.config.js", "postcss.config.mjs", "postcss.config.cjs", "postcss.config.ts")


def _postcss_configs():
    if not os.path.isdir(FRONTEND_DIR):
        pytest.skip("frontend not present")
    return [n for n in _POSTCSS_CONFIG_NAMES if os.path.exists(os.path.join(FRONTEND_DIR, n))]


def _postcss_config():
    configs = _postcss_configs()
    if not configs:
        pytest.fail("no PostCSS config: Tailwind v4 classes would never be compiled")
    return configs[0], _read(os.path.join(FRONTEND_DIR, configs[0]))


def _strip_js_comments(source):
    source = _re.sub(r"/\*.*?\*/", "", source, flags=_re.S)
    return _re.sub(r"(^|[^:\"'])//[^\n]*", r"\1", source)


def _global_css_files():
    found = []
    for sub in ("app", "styles", os.path.join("src", "app")):
        base = os.path.join(FRONTEND_DIR, sub)
        if os.path.isdir(base):
            found += [os.path.join(base, f) for f in os.listdir(base) if f.endswith(".css")]
    return found


def _tailwind_entry_css():
    for path in _global_css_files():
        if '@import "tailwindcss"' in _read(path) or "@import 'tailwindcss'" in _read(path):
            return path
    pytest.fail('no global CSS file imports "tailwindcss"')


def test_us_2_tailwind_postcss_declared_as_dev_dependency():
    pkg = _package_json()
    dev = pkg.get("devDependencies") or {}
    deps = pkg.get("dependencies") or {}
    assert TAILWIND_POSTCSS in dev, f"{TAILWIND_POSTCSS} missing from frontend devDependencies"
    assert TAILWIND_POSTCSS not in deps, f"{TAILWIND_POSTCSS} declared in both dependencies and devDependencies"


def test_us_2_tailwind_postcss_spec_tracks_tailwindcss_major_and_floor():
    declared = _declared_packages()
    if "tailwindcss" not in declared:
        pytest.skip("tailwindcss not used")
    tw_spec, plugin_spec = declared["tailwindcss"], declared.get(TAILWIND_POSTCSS)
    assert plugin_spec, f"{TAILWIND_POSTCSS} not declared"
    assert _major(plugin_spec) == _major(tw_spec) == 4, f"tailwindcss {tw_spec} vs {TAILWIND_POSTCSS} {plugin_spec}"
    tw, plugin = _parse_version(tw_spec), _parse_version(plugin_spec)
    assert plugin is not None, f"{TAILWIND_POSTCSS} spec {plugin_spec!r} has no concrete version"
    if tw:
        # The plugin compiles with its own bundled @tailwindcss/node; a lower
        # floor than tailwindcss can resolve an engine older than the CSS uses.
        assert plugin >= tw, f"{TAILWIND_POSTCSS} {plugin_spec} is older than tailwindcss {tw_spec}"
    assert not plugin_spec.strip().startswith(("*", "latest", ">=")), f"unbounded spec {plugin_spec!r}"


def test_us_2_tailwind_single_postcss_config():
    configs = _postcss_configs()
    assert len(configs) <= 1, f"several PostCSS configs, Next picks one arbitrarily: {configs}"


def test_us_2_tailwind_postcss_config_uses_official_plugin():
    name, source = _postcss_config()
    code = _strip_js_comments(source)
    assert _re.search(r"[\"']@tailwindcss/postcss[\"']", code), f"{name} does not load {TAILWIND_POSTCSS}"
    # v3-style `tailwindcss: {}` / require("tailwindcss") throws under v4.
    assert not _re.search(r"(^|[{,\s])[\"']?tailwindcss[\"']?\s*:", code), f"{name} still uses the v3 tailwindcss plugin"
    assert not _re.search(r"require\(\s*[\"']tailwindcss[\"']\s*\)", code), f"{name} requires tailwindcss directly"


def test_us_2_tailwind_postcss_config_has_no_local_plugin_shim():
    name, source = _postcss_config()
    code = _strip_js_comments(source)
    assert "tailwind-postcss" not in code, f"{name} still loads the local tailwind-postcss shim"
    local = _re.findall(r"""(?:require\(|import\s+[^'"]*from\s+|import\(\s*)["'](\.{1,2}/[^"']+)["']""", code)
    local += _re.findall(r"""["'](\.{1,2}/[^"']+)["']\s*:""", code)
    assert not local, f"{name} loads local PostCSS plugins instead of installed packages: {local}"
    assert "compile(" not in code, f"{name} drives the Tailwind compiler by hand"


def test_us_2_tailwind_postcss_config_is_the_standard_plugins_map():
    name, source = _postcss_config()
    code = _strip_js_comments(source)
    m = _re.search(r"plugins\s*:\s*\{(.*?)\}\s*,?\s*\}", code, _re.S)
    assert m, f"{name} does not export a `plugins: {{...}}` map"
    keys = _re.findall(r"[\"']?((?:@[\w.-]+/)?[\w.-]+)[\"']?\s*:", m.group(1))
    assert keys == [TAILWIND_POSTCSS], f"{name} plugins should be just {TAILWIND_POSTCSS}, got {keys}"


def test_us_2_tailwind_postcss_config_module_format_matches_package_type():
    name, source = _postcss_config()
    code = _strip_js_comments(source)
    is_esm = _package_json().get("type") == "module"
    if name == "postcss.config.cjs" or (name == "postcss.config.js" and not is_esm):
        assert "module.exports" in code, f"{name} is CommonJS but has no module.exports"
        assert not _re.search(r"^\s*export\s+default\b", code, _re.M), f"{name} is CommonJS but uses export default"
    elif name == "postcss.config.mjs" or (name == "postcss.config.js" and is_esm):
        assert "module.exports" not in code, f"{name} is ESM but uses module.exports"


def test_us_2_tailwind_local_shim_file_deleted():
    if not os.path.isdir(FRONTEND_DIR):
        pytest.skip("frontend not present")
    for ext in (".js", ".cjs", ".mjs", ".ts"):
        path = os.path.join(FRONTEND_DIR, "tailwind-postcss" + ext)
        assert not os.path.exists(path), f"frontend/{os.path.basename(path)} should be deleted"


def test_us_2_tailwind_nothing_references_deleted_shim():
    if not os.path.isdir(FRONTEND_DIR):
        pytest.skip("frontend not present")
    refs = []
    for root, dirs, files in os.walk(FRONTEND_DIR):
        dirs[:] = [d for d in dirs if d not in ("node_modules", ".next", ".git")]
        for f in files:
            if f.endswith(_SOURCE_EXTS + (".cjs", ".json", ".css")) and f != "package-lock.json":
                if _re.search(r"[\"'./]tailwind-postcss(\.c?js)?[\"']", _read(os.path.join(root, f))):
                    refs.append(os.path.relpath(os.path.join(root, f), FRONTEND_DIR))
    assert not refs, f"still referencing the deleted tailwind-postcss shim: {refs}"


def test_us_2_tailwind_autoprefixer_removed():
    # Tailwind v4 (Lightning CSS) prefixes itself; a leftover autoprefixer is dead weight.
    declared = _declared_packages()
    assert "autoprefixer" not in declared, "autoprefixer still declared; Tailwind v4 adds vendor prefixes itself"
    configs = _postcss_configs()
    for name in configs:
        assert "autoprefixer" not in _strip_js_comments(_read(os.path.join(FRONTEND_DIR, name))), (
            f"{name} still loads autoprefixer"
        )


def test_us_2_tailwind_lockfile_resolves_postcss_plugin():
    # The "required action": npm install was run and the lockfile committed.
    lock = _lockfile()
    packages = lock.get("packages") or {}
    entry = packages.get(f"node_modules/{TAILWIND_POSTCSS}")
    assert entry and entry.get("version"), f"package-lock.json has no node_modules/{TAILWIND_POSTCSS}; run npm install"
    spec = _declared_packages().get(TAILWIND_POSTCSS)
    if spec:
        assert _satisfies(entry["version"], spec), f"locked {TAILWIND_POSTCSS} {entry['version']} outside {spec}"
    assert entry.get("dev") is True or entry.get("devOptional") is True, (
        f"lockfile marks {TAILWIND_POSTCSS} as a production dependency; expected devDependencies"
    )


def test_us_2_tailwind_lockfile_root_mirrors_package_json():
    lock = _lockfile()
    root = (lock.get("packages") or {}).get("") or {}
    pkg = _package_json()
    root_dev = root.get("devDependencies") or {}
    assert root_dev.get(TAILWIND_POSTCSS) == (pkg.get("devDependencies") or {}).get(TAILWIND_POSTCSS), (
        "package-lock.json root devDependencies out of sync for @tailwindcss/postcss; re-run npm install"
    )
    for section in ("dependencies", "devDependencies"):
        assert "autoprefixer" not in (root.get(section) or {}), f"lockfile root {section} still lists autoprefixer"


def test_us_2_tailwind_lockfile_plugin_and_engine_same_major():
    lock = _lockfile()
    tw = _locked_versions(lock, "tailwindcss")
    plugin = _locked_versions(lock, TAILWIND_POSTCSS)
    if not tw:
        pytest.skip("tailwindcss not in lockfile")
    assert plugin, f"{TAILWIND_POSTCSS} not in lockfile"
    top_tw = (lock.get("packages") or {}).get("node_modules/tailwindcss", {}).get("version")
    for v in plugin:
        assert _parse_version(v)[0] == _parse_version(top_tw or tw[0])[0], f"{TAILWIND_POSTCSS} {v} vs tailwindcss {top_tw}"
    # The plugin's own tailwindcss requirement must be met by the hoisted copy.
    entry = (lock.get("packages") or {}).get(f"node_modules/{TAILWIND_POSTCSS}", {})
    wanted = (entry.get("dependencies") or {}).get("tailwindcss")
    nested = (lock.get("packages") or {}).get(f"node_modules/{TAILWIND_POSTCSS}/node_modules/tailwindcss")
    if wanted and top_tw and not nested:
        assert _satisfies(top_tw, wanted), f"{TAILWIND_POSTCSS} needs tailwindcss {wanted}, lockfile has {top_tw}"


def test_us_2_tailwind_css_entry_import_comes_first():
    # A CSS @import after any rule is ignored, so no Tailwind styles at all.
    path = _tailwind_entry_css()
    css = _re.sub(r"/\*.*?\*/", "", _read(path), flags=_re.S)
    before = css[: css.find("@import")]
    leftover = _re.sub(r"@charset[^;]*;|@layer\s+[\w\s,-]+;", "", before).strip()
    assert not leftover, f"{os.path.basename(path)} has rules before @import \"tailwindcss\": {leftover[:80]!r}"
    assert not _re.search(r"@tailwind\s+(base|components|utilities)", css), "v3 @tailwind directives left in"


def test_us_2_tailwind_css_config_directive_points_at_existing_config():
    path = _tailwind_entry_css()
    css = _read(path)
    m = _re.search(r"""@config\s+["']([^"']+)["']""", css)
    if not m:
        pytest.skip("no @config directive (pure CSS-first v4 config)")
    target = os.path.normpath(os.path.join(os.path.dirname(path), m.group(1)))
    assert os.path.exists(target), f"@config {m.group(1)!r} resolves to missing {target}"
    assert os.path.commonpath([os.path.abspath(FRONTEND_DIR), os.path.abspath(target)]) == os.path.abspath(FRONTEND_DIR)


def test_us_2_tailwind_config_module_format_matches_package_type():
    name, source = _tailwind_config()
    if not name:
        pytest.skip("no tailwind.config")
    code = _strip_js_comments(source)
    is_esm = _package_json().get("type") == "module"
    if name.endswith(".cjs") or (name.endswith(".js") and not is_esm):
        assert not _re.search(r"^\s*export\s+default\b", code, _re.M), f"{name} is CommonJS but uses export default"
    elif name.endswith(".mjs") or (name.endswith(".js") and is_esm):
        assert "module.exports" not in code, f"{name} is ESM but uses module.exports"


def test_us_2_tailwind_config_requires_only_installed_packages():
    # @config loads tailwind.config through @tailwindcss/postcss; a plugin it
    # requires that isn't installed fails the first compile of "/".
    name, source = _tailwind_config()
    if not name:
        pytest.skip("no tailwind.config")
    code = _strip_js_comments(source)
    declared = _declared_packages()
    for spec in _re.findall(r"""require\(\s*["']([^"'.][^"']*)["']\s*\)""", code):
        pkg = "/".join(spec.split("/")[:2]) if spec.startswith("@") else spec.split("/")[0]
        assert pkg in declared or pkg == "tailwindcss", f"{name} requires {spec} which is not installed"


def test_us_2_tailwind_postcss_installed_in_node_modules():
    nm = os.path.join(FRONTEND_DIR, "node_modules")
    if not os.path.isdir(nm):
        pytest.skip("frontend/node_modules not installed")
    manifest = os.path.join(nm, "@tailwindcss", "postcss", "package.json")
    assert os.path.exists(manifest), "node_modules has no @tailwindcss/postcss; run npm install in frontend/"
    installed = _load_json(manifest).get("version")
    spec = _declared_packages().get(TAILWIND_POSTCSS)
    if spec:
        assert _satisfies(installed, spec), f"installed {TAILWIND_POSTCSS} {installed} outside {spec}"


@pytest.mark.skipif(os.environ.get("CROWNAI_RUN_FRONTEND_BUILD") != "1", reason="set CROWNAI_RUN_FRONTEND_BUILD=1")
def test_us_2_tailwind_npm_run_build_compiles_styles():
    npm = shutil.which("npm")
    if not npm or not os.path.isdir(os.path.join(FRONTEND_DIR, "node_modules")):
        pytest.skip("npm or frontend/node_modules not available")
    proc = subprocess.run([npm, "run", "build"], cwd=FRONTEND_DIR, capture_output=True, text=True, timeout=900)
    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-3000:]
    css_dir = os.path.join(FRONTEND_DIR, ".next", "static")
    css = ""
    for root, _dirs, files in os.walk(css_dir):
        css += "".join(_read(os.path.join(root, f)) for f in files if f.endswith(".css"))
    assert css, "build emitted no CSS"
    assert "@tailwind" not in css and '@import "tailwindcss"' not in css, "Tailwind directives were not compiled"
    assert _re.search(r"--tw-|\.flex\{|\.grid\{", css), "compiled CSS has no Tailwind utilities"


# ===========================================================================
# Human-requested coverage (Testing defects pass, all fixes applied together:
# blur race on the create form, padded case-study link name, missing
# @tailwindcss/postcss + local shim, ui/performance layers skipped while
# `next dev` sat on "Compiling / ..."). Gaps the passes above leave open:
#   - US-4: once the click reaches the button, nothing else may stop onCreate
#     -- native `required` validation (no noValidate) blocks the submit event
#     just like the layout shift did, and an onClick={onCreate} on a submit
#     button runs it twice per click.
#   - US-12: aria-label is built from the client string, so client names must
#     be trimmed (no "View case study:  X ") and unique (distinct link names).
#   - Tailwind: what `npm run build` relies on -- Node can actually load the
#     standard postcss.config.js and resolve @tailwindcss/postcss from
#     frontend/, the plugin compiles globals.css in bounded time (the hand-
#     rolled Once()/compile() shim was the stand-in that "Compiling / ..."
#     was stuck behind), tailwindcss stays declared for `@import "tailwindcss"`,
#     the regenerated lockfile carries the native oxide/lightningcss binaries
#     for every platform (Windows included), and no orphaned autoprefixer.
# Frontend checks skip when the frontend, node, or node_modules are absent.
# ===========================================================================

FRONTEND_COMPILE_BUDGET_S = 60.0
_NATIVE_TAILWIND_PACKAGES = ("@tailwindcss/oxide", "lightningcss")


def _form_open_tag_around(source, element_id):
    idx = source.find(f'id="{element_id}"')
    if idx == -1:
        idx = source.find(f"id='{element_id}'")
    if idx == -1:
        pytest.skip(f"#{element_id} not found in crown-ai/page.tsx")
    start = source.rfind("<form", 0, idx)
    if start == -1:
        pytest.skip(f"#{element_id} is not inside a <form>")
    return _re.match(r"<form\b.*?>", source[start:], _re.S).group(0)


def _has_native_required(tag):
    return bool(_re.search(r"(?<![\w-])required(?=\s|/?>|=\{\s*true\s*\})", tag))


def test_us_4_create_form_native_required_does_not_block_on_create():
    # A `required` field without noValidate makes the browser cancel the
    # submit: onCreate never runs, so no formSubmitted, no requirements error
    # and no refocus -- the same symptom as the blur race.
    source = _crown_ai_page()
    for field in ("proj-name", "proj-reqs"):
        if _has_native_required(_input_tag(source, field)):
            form = _form_open_tag_around(source, field)
            assert "noValidate" in form, (
                f"#{field} is `required` but the form lacks noValidate; native validation swallows the submit"
            )


def test_us_4_create_form_submit_button_does_not_also_call_on_create_on_click():
    # type="submit" inside an onSubmit form already runs onCreate; an extra
    # onClick={onCreate} creates two projects per click.
    tag = _submit_button_tag(_crown_ai_page())
    on_click = _re.search(r"onClick=\{([^}]*)\}", tag)
    if on_click:
        assert "onCreate" not in on_click.group(1), "Generate Project calls onCreate from onClick and onSubmit"


def test_us_4_create_form_touched_not_set_in_on_focus():
    # Marking touched on focus has the same effect for the autofocused name:
    # the error is visible before the user has typed anything.
    source = _crown_ai_page()
    for field in ("proj-name", "proj-reqs"):
        focus = _re.search(r"onFocus=\{([^\n]*)", _input_tag(source, field))
        assert not (focus and "ouched" in focus.group(1)), f"#{field} is marked touched on focus"


def test_us_4_create_form_rapid_invalid_double_click_creates_nothing_and_keeps_quota(client):
    # Two quick clicks on an empty form: both rejected, workspace stays
    # empty, and the free daily allowance is untouched.
    headers, _ = _session_for()
    codes = _run_parallel(
        lambda: client.post("/projects", json={"name": "", "requirements": ""}, headers=headers).status_code,
        [()] * 2,
    )
    assert codes == [422, 422]
    assert client.get("/projects", headers=headers).json() == []
    project = _create_project(client, headers)
    _generate_all(client, headers, project["id"])


def _case_study_clients():
    if not os.path.isdir(FRONTEND_DIR):
        pytest.skip("frontend not present")
    for root, dirs, files in os.walk(FRONTEND_DIR):
        dirs[:] = [d for d in dirs if d not in ("node_modules", ".next", ".git")]
        for f in files:
            if f.endswith((".ts", ".tsx", ".js", ".json")):
                source = _read(os.path.join(root, f))
                if "Regional NBFC (Chennai)" in source:
                    clients = _re.findall(r"""["']?client["']?\s*:\s*(["'`])(.*?)\1""", source)
                    if clients:
                        return [c for _, c in clients]
    pytest.skip("case-study data with literal client names not found")


def test_us_12_case_study_client_names_trimmed_and_unique():
    clients = _case_study_clients()
    assert "Regional NBFC (Chennai)" in clients
    for client_name in clients:
        assert client_name == client_name.strip(), f"client {client_name!r} has padding; aria-label would too"
        assert "  " not in client_name, f"client {client_name!r} has a double space"
    assert len(clients) == len(set(clients)), f"two links would share an accessible name: {clients}"


def test_us_12_case_study_every_label_matches_ui_regex_shape():
    # The UI test uses `View case study: <escaped client>`; every card's label
    # must have exactly that shape for its own client.
    for client_name in _case_study_clients():
        label = f"View case study: {client_name}"
        assert _re.fullmatch(r"View case study: " + _re.escape(client_name), label)
        assert not _re.search(r"\s:|:\s{2,}|\s$", label), f"badly spaced label {label!r}"


def _node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    if not os.path.isdir(os.path.join(FRONTEND_DIR, "node_modules")):
        pytest.skip("frontend/node_modules not installed")
    return node


def _run_node(script, timeout=120, env_extra=None):
    env = {**os.environ, **(env_extra or {})}
    return subprocess.run(
        [_node(), "-e", script], cwd=FRONTEND_DIR, capture_output=True, text=True, timeout=timeout, env=env
    )


def test_us_2_tailwind_postcss_config_value_is_empty_options():
    # The standard setup is `"@tailwindcss/postcss": {}` -- v3 options like
    # `config:` are not supported and the @config directive handles it.
    name, source = _postcss_config()
    m = _re.search(r"[\"']@tailwindcss/postcss[\"']\s*:\s*(\{[^}]*\})", _strip_js_comments(source))
    assert m, f"{name} does not configure {TAILWIND_POSTCSS} as an object"
    assert _re.fullmatch(r"\{\s*\}", m.group(1)), f"{name} passes options to {TAILWIND_POSTCSS}: {m.group(1)}"


def test_us_2_tailwind_tailwindcss_still_declared_for_css_import():
    # `@import "tailwindcss"` in globals.css resolves the tailwindcss package;
    # replacing it with the plugin instead of adding the plugin breaks that.
    declared = _declared_packages()
    assert "tailwindcss" in declared, "tailwindcss removed; globals.css imports it"
    assert TAILWIND_POSTCSS in declared


def test_us_2_tailwind_node_loads_postcss_config_exactly():
    # Next loads postcss.config.js with Node; assert what Node actually sees.
    name, _ = _postcss_config()
    proc = _run_node(
        "const c=require('./" + name + "');const m=c&&c.__esModule?c.default:c;"
        "process.stdout.write(JSON.stringify(m))",
        timeout=60,
    )
    if proc.returncode != 0 and "ERR_REQUIRE_ESM" in proc.stderr:
        pytest.skip(f"{name} is ESM; require() cannot load it")
    assert proc.returncode == 0, proc.stderr[-2000:]
    loaded = _json.loads(proc.stdout)
    assert loaded == {"plugins": {TAILWIND_POSTCSS: {}}}, f"{name} evaluates to {loaded}"


def test_us_2_tailwind_postcss_plugin_resolves_from_frontend():
    proc = _run_node(
        "process.stdout.write(require.resolve('@tailwindcss/postcss'))",
        timeout=60,
    )
    assert proc.returncode == 0, f"@tailwindcss/postcss not resolvable from frontend/: {proc.stderr[-1000:]}"
    resolved = os.path.abspath(proc.stdout.strip())
    assert os.path.commonpath([os.path.abspath(FRONTEND_DIR), resolved]) == os.path.abspath(FRONTEND_DIR), (
        f"@tailwindcss/postcss resolves outside frontend/ ({resolved}); a stray parent install is masking it"
    )


def test_us_2_tailwind_plugin_compiles_globals_css_within_budget():
    # The piece of `npm run build` / "Compiling / ..." this fix changes: run
    # globals.css through the official plugin and bound the time it takes.
    entry = _tailwind_entry_css()
    script = (
        "let postcss;try{postcss=require('postcss')}catch(e){process.exit(42)}"
        "const tw=require('@tailwindcss/postcss');const fs=require('fs');const from=process.env.TW_ENTRY;"
        "postcss([tw()]).process(fs.readFileSync(from,'utf8'),{from}).then(r=>process.stdout.write(r.css))"
        ".catch(e=>{console.error(e&&e.stack||e);process.exit(1)})"
    )
    start = time.perf_counter()
    try:
        proc = _run_node(script, timeout=FRONTEND_COMPILE_BUDGET_S * 3, env_extra={"TW_ENTRY": entry})
    except subprocess.TimeoutExpired:
        pytest.fail("Tailwind compile of globals.css never finished -- the 'Compiling / ...' hang")
    elapsed = time.perf_counter() - start
    if proc.returncode == 42:
        pytest.skip("postcss not resolvable from frontend/")
    assert proc.returncode == 0, proc.stderr[-3000:]
    css = proc.stdout
    assert css.strip(), "plugin produced no CSS"
    assert '@import "tailwindcss"' not in css and "@import 'tailwindcss'" not in css, "import was not inlined"
    assert "@config" not in css, "@config directive was not consumed"
    assert _re.search(r"--tw-|--color-|\.flex\b|\.grid\b", css), "compiled CSS has no Tailwind output"
    assert elapsed < FRONTEND_COMPILE_BUDGET_S, f"globals.css took {elapsed:.1f}s to compile"


def test_us_2_tailwind_lockfile_has_native_binaries_for_every_platform():
    # `npm install` records every platform's optional binary; a lockfile
    # made on Linux with entries pruned breaks the build on Windows dev boxes.
    packages = _lockfile().get("packages") or {}
    checked = False
    for name in _NATIVE_TAILWIND_PACKAGES:
        meta = packages.get(f"node_modules/{name}")
        if not meta:
            continue
        optional = meta.get("optionalDependencies") or {}
        missing = [d for d in optional if not _locked_versions({"packages": packages}, d)]
        assert not missing, f"lockfile lacks {name} platform binaries: {missing}"
        if optional:
            checked = True
            assert any("win32" in d for d in optional), f"{name} has no Windows binary in the lockfile"
    if not checked:
        pytest.skip("no native Tailwind packages with optional binaries in lockfile")


def test_us_2_tailwind_native_binding_installed_for_this_platform():
    nm = os.path.join(FRONTEND_DIR, "node_modules")
    if not os.path.isdir(nm):
        pytest.skip("frontend/node_modules not installed")
    platform = {"win32": "win32", "linux": "linux", "darwin": "darwin"}.get(sys.platform)
    if not platform:
        pytest.skip(f"unmapped platform {sys.platform}")
    oxide = os.path.join(nm, "@tailwindcss")
    if not os.path.isdir(os.path.join(oxide, "oxide")):
        pytest.skip("@tailwindcss/oxide not installed")
    bindings = [d for d in os.listdir(oxide) if d.startswith(f"oxide-{platform}")]
    assert bindings, f"no @tailwindcss/oxide-{platform}-* binding installed; re-run npm install on this machine"


def test_us_2_tailwind_lockfile_has_no_orphaned_autoprefixer():
    # Removing autoprefixer from package.json without `npm install` leaves it
    # in the lockfile with nothing depending on it.
    packages = _lockfile().get("packages") or {}
    if "node_modules/autoprefixer" not in packages:
        return
    dependents = [
        key
        for key, meta in packages.items()
        if key != "node_modules/autoprefixer"
        and any("autoprefixer" in (meta.get(s) or {}) for s in ("dependencies", "optionalDependencies", "peerDependencies"))
    ]
    assert dependents, "autoprefixer is still locked but nothing depends on it; re-run npm install"


# ===========================================================================
# Human-requested coverage (Testing defects pass, all four fixes together):
#   1. projects/[id]/page.tsx: a "use client" page that reads the route via
#      useParams(). The fix destructures `const { id } = useParams()` so no
#      `params.x` access is left (what the Next 16 sync-params check matches),
#      every URL/dependency array uses `id`, and no `await params` / `use(params)`
#      is bolted onto a hook result. Backend half: the ids that page puts in
#      its URLs (including the "undefined" a broken destructure would send)
#      must give clean 404/403 JSON, never a 5xx.
#   2. frontend/tailwind-postcss.js (the "crownai-tailwindcss" compile() shim)
#      is deleted in every extension, untracked by git, and nothing -- sources,
#      package.json scripts or a package.json "postcss" field -- still points
#      at it or drives tailwindcss's compile() by hand.
#   3. Header.tsx: the mobile toggle is disabled until hydration
#      (`useState(false)` + mount-only effect -> `disabled={!hydrated}`), the
#      flag never goes back to false, `disabled` depends ONLY on hydration (so
#      "Close menu" is never disabled), aria-expanded={open} and the
#      Open/Close label switch stay, and the mobile nav adds no 100vw overflow.
#   4. globals.css: `.btn-primary` must not move under the pointer -- no
#      transform/translate/scale or box-size change on :hover (nor :focus /
#      :active, which apply during the same slow click), in plain, nested (&)
#      or @layer-wrapped rules, via @apply, or via hover: utilities on the
#      Generate Project button; the brightness/saturate filter stays.
# The CSS/JSX helpers are unit-tested on synthetic input so the checks are
# known to catch the original defects even where the frontend is absent.
# ===========================================================================

REPO_DIR = os.path.dirname(BACKEND_DIR)
_SHIFT_PROPS = {
    "transform", "translate", "scale", "rotate", "position", "top", "bottom", "left", "right",
    "inset", "inset-block", "inset-inline", "margin", "margin-top", "margin-bottom", "margin-left",
    "margin-right", "margin-block", "margin-inline", "padding", "padding-top", "padding-bottom",
    "padding-left", "padding-right", "padding-block", "padding-inline", "width", "height",
    "min-height", "border-width", "font-size", "line-height",
}
_SHIFT_APPLY_RE = _re.compile(r"(?:^|\s)!?-?(?:translate|scale|top|bottom|inset|m[trblxy]?|p[trblxy]?|h|w)-")


# ---------- helpers ----------


def _css_rules(css):
    """Flat [(selectors, declarations)] for a stylesheet, resolving CSS
    nesting (`&`) and descending into @media/@layer/@supports."""
    css = _re.sub(r"/\*.*?\*/", "", css, flags=_re.S)
    rules = []

    def matching(text, i):
        depth = 0
        for j in range(i, len(text)):
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
                if depth == 0:
                    return j
        return len(text) - 1

    def combine(parent, sel):
        parts = [s.strip() for s in sel.split(",") if s.strip()]
        if not parent:
            return parts
        return [s.replace("&", p) if "&" in s else f"{p} {s}" for p in parent for s in parts]

    def walk(text, parent):
        start, i, decls = 0, 0, []
        while i < len(text):
            c = text[i]
            if c == "{":
                sel = text[start:i].strip()
                j = matching(text, i)
                inner = text[i + 1 : j]
                if sel.startswith("@"):
                    if not _re.match(r"@(-webkit-)?(keyframes|font-face|property)\b", sel):
                        own = walk(inner, parent)
                        if parent and own:
                            rules.append((parent, own))
                else:
                    sels = combine(parent, sel)
                    rules.append((sels, walk(inner, sels)))
                i = j + 1
                start = i
                continue
            if c == ";":
                decls.append(text[start:i].strip())
                start = i + 1
            elif c == "}":
                start = i + 1
            i += 1
        tail = text[start:].strip()
        if tail:
            decls.append(tail)
        return ";".join(d for d in decls if d)

    walk(css, None)
    return rules


def _declarations(decls):
    out = []
    for d in decls.split(";"):
        d = d.strip()
        if d.startswith("@apply"):
            out.append(("@apply", d[len("@apply") :].strip()))
        elif ":" in d:
            prop, value = d.split(":", 1)
            out.append((prop.strip().lower(), value.strip()))
    return out


def _shifting_declarations(decls):
    """Declarations that move or resize the element (box position changes)."""
    bad = []
    for prop, value in _declarations(decls):
        if prop == "@apply":
            if _SHIFT_APPLY_RE.search(" " + value):
                bad.append(f"@apply {value}")
        elif prop in _SHIFT_PROPS:
            if prop in ("transform", "translate", "scale", "rotate") and value.lower() in ("none", "initial", "unset"):
                continue
            bad.append(f"{prop}: {value}")
    return bad


def _state_rules(rules, cls, state):
    return [
        (sel, decls)
        for sels, decls in rules
        for sel in sels
        if cls in sel and f":{state}" in sel
    ]


def _globals_css():
    return _frontend_source("app", "globals.css")


def _jsx_open_tags(source, tag):
    """Every `<tag ...>` opening tag, brace-aware so `=>` in handlers
    doesn't end the tag early."""
    tags = []
    for m in _re.finditer(rf"<{tag}\b", source):
        depth, j = 0, m.end()
        while j < len(source):
            c = source[j]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            elif c == ">" and depth == 0:
                break
            j += 1
        tags.append(source[m.start() : j + 1])
    return tags


def _menu_toggle_tag(source):
    for tag in _jsx_open_tags(source, "button"):
        if "aria-expanded" in tag:
            return tag
    pytest.fail("Header has no <button aria-expanded=...> mobile menu toggle")


def _hydration_gate(source, toggle):
    """(flag, setter) when the toggle is `disabled={!flag}` and flag is a
    useState(false) set true by a mount-only effect; else a reason string."""
    m = _re.search(r"disabled=\{\s*!\s*(\w+)\s*\}", toggle)
    if not m:
        return "toggle is not `disabled={!<hydrated flag>}`"
    flag = m.group(1)
    state = _re.search(
        rf"const\s*\[\s*{flag}\s*,\s*(\w+)\s*\]\s*=\s*(?:React\.)?useState(?:<boolean>)?\(\s*false\s*\)", source
    )
    if not state:
        return f"`{flag}` is not declared as useState(false)"
    setter = state.group(1)
    effect = _re.search(
        rf"use(?:Layout)?Effect\(\s*\(\)\s*=>\s*\{{?\s*{setter}\(\s*true\s*\)\s*;?\s*\}}?\s*,\s*\[\s*\]\s*\)", source
    )
    if not effect:
        return f"no mount-only effect calls {setter}(true)"
    return flag, setter


def _header_source():
    return _frontend_source("components", "Header.tsx")


def _project_detail_page():
    return _frontend_source("app", "crown-ai", "projects", "[id]", "page.tsx")


def _function_window(source, name, size=1800):
    m = _re.search(rf"(?:const|let|function|async\s+function)\s+{name}\b", source)
    return source[m.start() : m.start() + size] if m else None


def _hook_dependency_arrays(source):
    return [
        [d.strip() for d in body.split(",") if d.strip()]
        for body in _re.findall(r",\s*\[([\w\s,.?]*)\]\s*\)", source)
    ]


_NEXT16_SYNC_PARAMS_RE = _re.compile(r"\bparams\.\w+")
_NEXT16_AWAITED_RE = _re.compile(r"await\s+params|use\(\s*params\s*\)")


def _flagged_by_sync_params_check(source):
    # Same rule as test_us_2_frontend_dynamic_route_pages_await_params.
    return bool(_NEXT16_SYNC_PARAMS_RE.search(source)) and not _NEXT16_AWAITED_RE.search(source)


# ---------- helper self-tests (always run) ----------


def test_us_4_btn_primary_css_walker_flags_original_hover_lift():
    css = """
    .btn-primary { transition: transform .15s ease, filter .15s ease; }
    .btn-primary:hover:not(:disabled) { transform: translateY(-1px); filter: brightness(1.05) saturate(1.1); }
    """
    hover = _state_rules(_css_rules(css), ".btn-primary", "hover")
    assert hover and _shifting_declarations(hover[0][1]) == ["transform: translateY(-1px)"]


def test_us_4_btn_primary_css_walker_accepts_fixed_filter_and_shadow_only():
    css = """
    .btn-primary:hover:not(:disabled) { filter: brightness(1.05) saturate(1.1); box-shadow: 0 6px 16px rgb(0 0 0 / .2); }
    .btn-primary:active { transform: none; }
    """
    rules = _css_rules(css)
    for state in ("hover", "active"):
        for _, decls in _state_rules(rules, ".btn-primary", state):
            assert _shifting_declarations(decls) == []


@pytest.mark.parametrize(
    "css",
    [
        "@layer components { .btn-primary:hover:not(:disabled) { transform: translateY(-1px); } }",
        ".btn-primary { color: red; &:hover:not(:disabled) { translate: 0 -1px; } }",
        ".btn-primary:hover { @apply -translate-y-px; }",
        "@media (hover: hover) { .btn-primary:hover { margin-top: -1px; } }",
        ".btn-secondary, .btn-primary:hover { scale: 1.02; }",
        ".btn-primary:hover { @media (min-width: 640px) { transform: translateY(-2px); } }",
    ],
)
def test_us_4_btn_primary_css_walker_catches_every_lift_spelling(css):
    hover = _state_rules(_css_rules(css), ".btn-primary", "hover")
    assert hover, f"walker missed the hover rule in {css!r}"
    assert any(_shifting_declarations(d) for _, d in hover), f"walker missed the shift in {css!r}"


def test_us_4_btn_primary_css_walker_ignores_keyframes_and_other_classes():
    css = """
    @keyframes rise { from { transform: translateY(4px); } to { transform: none; } }
    .card:hover { transform: translateY(-2px); }
    .btn-primary:hover:not(:disabled) { filter: brightness(1.05); }
    """
    hover = _state_rules(_css_rules(css), ".btn-primary", "hover")
    assert [s for s, _ in hover] == [".btn-primary:hover:not(:disabled)"]
    assert _shifting_declarations(hover[0][1]) == []


def test_us_2_header_hydration_gate_helper_accepts_fix_and_rejects_variants():
    fixed = """
    const [open, setOpen] = useState(false);
    const [hydrated, setHydrated] = useState(false);
    useEffect(() => setHydrated(true), []);
    """
    tag = '<button aria-expanded={open} disabled={!hydrated} onClick={() => setOpen((o) => !o)}>'
    assert _hydration_gate(fixed, tag) == ("hydrated", "setHydrated")
    braces = fixed.replace("useEffect(() => setHydrated(true), [])", "useEffect(() => { setHydrated(true); }, [])")
    assert _hydration_gate(braces, tag) == ("hydrated", "setHydrated")
    # Effect re-running on every render / tied to open, or flag starting true, is not a gate.
    assert isinstance(_hydration_gate(fixed.replace(", []);", ", [open]);"), tag), str)
    assert isinstance(_hydration_gate(fixed.replace("useState(false);\n    useEffect", "useState(true);\n    useEffect"), tag), str)
    assert isinstance(_hydration_gate(fixed, tag.replace("disabled={!hydrated}", "disabled={!hydrated || open}")), str)
    assert isinstance(_hydration_gate(fixed, tag.replace(" disabled={!hydrated}", "")), str)


def test_us_2_header_jsx_tag_scanner_survives_arrow_handlers():
    src = '<button onClick={() => setOpen((o) => !o)} aria-expanded={open} disabled={!hydrated}>Menu</button>'
    assert _jsx_open_tags(src, "button") == [src[: src.index(">Menu") + 1]]


def test_us_2_project_detail_sync_params_check_semantics():
    # Why the old page was flagged and the fixed one is not.
    old = 'const params = useParams<{ id: string }>();\nfetch(`/projects/${params.id}`);'
    fixed = 'const { id } = useParams<{ id: string }>();\nfetch(`/projects/${id}`);'
    server_ok = "export default async function Page({ params }) { const { slug } = await params; }"
    server_bad = "export default function Page({ params }) { return params.slug; }"
    assert _flagged_by_sync_params_check(old)
    assert not _flagged_by_sync_params_check(fixed)
    assert not _flagged_by_sync_params_check(server_ok)
    assert _flagged_by_sync_params_check(server_bad)


# ---------- 1. projects/[id]/page.tsx: destructured useParams ----------


def test_us_2_project_detail_page_is_client_and_uses_use_params():
    source = _project_detail_page()
    assert _USE_CLIENT_RE.match(source), "projects/[id]/page.tsx must stay a client component"
    assert _re.search(r"import\s*\{[^}]*\buseParams\b[^}]*\}\s*from\s*[\"']next/navigation[\"']", source), (
        "useParams must still be imported from next/navigation"
    )


def test_us_2_project_detail_destructures_id_from_use_params():
    source = _project_detail_page()
    assert _re.search(r"const\s*\{\s*id\s*\}\s*=\s*useParams\s*(?:<[^>]*>)?\s*\(\s*\)", source), (
        "expected `const { id } = useParams<{ id: string }>();`"
    )
    assert not _re.search(r"\b(?:const|let|var)\s+params\s*=", source), "a `params` variable is still declared"


def test_us_2_project_detail_has_no_params_member_access_left():
    source = _project_detail_page()
    leftovers = _re.findall(r"\bparams\s*(?:\?\.|\.|\[)\s*\w*", source)
    assert not leftovers, f"params access still present: {leftovers}"


def test_us_2_project_detail_no_longer_matches_next16_sync_params_check():
    assert not _flagged_by_sync_params_check(_project_detail_page())


def test_us_2_project_detail_does_not_await_or_use_hook_result():
    # useParams() returns a plain object; awaiting it / React.use() on it in
    # a client component would be a new bug, not the fix.
    source = _project_detail_page()
    assert not _NEXT16_AWAITED_RE.search(source)
    assert not _re.search(r"await\s+useParams|use\(\s*useParams", source)


@pytest.mark.parametrize("name", ["refresh", "runGenerate", "onDownload"])
def test_us_2_project_detail_handlers_build_urls_from_id(name):
    window = _function_window(_project_detail_page(), name)
    if window is None:
        pytest.skip(f"{name} not found in projects/[id]/page.tsx")
    assert "${id}" in window, f"{name} no longer puts the project id in its request URL"
    assert "params" not in window, f"{name} still reads params"


def test_us_2_project_detail_request_urls_all_use_id():
    source = _project_detail_page()
    urls = _re.findall(r"`[^`]*/projects/[^`]*`", source)
    assert urls, "no /projects/... request URLs found"
    for url in urls:
        assert "${id}" in url or "${encodeURIComponent(id)}" in url, f"URL does not use id: {url}"
    for suffix in ("/generate/", "/download"):
        assert any(suffix in u for u in urls), f"no request URL for {suffix}"


def test_us_2_project_detail_dependency_arrays_use_id():
    arrays = _hook_dependency_arrays(_project_detail_page())
    assert not [a for a in arrays if any("params" in d for d in a)], f"params left in deps: {arrays}"
    assert any(set(a) >= {"id", "router"} for a in arrays), f"refresh deps should be [id, router]; got {arrays}"
    assert any(set(a) >= {"id", "refresh"} for a in arrays), f"effect deps should be [id, refresh]; got {arrays}"


def test_us_2_every_dynamic_route_page_passes_sync_params_check():
    offenders = [
        os.path.relpath(p, FRONTEND_DIR) for p in _route_pages_with_dynamic_segments() if _flagged_by_sync_params_check(_read(p))
    ]
    assert not offenders, f"still flagged as sync params access: {offenders}"


def test_us_2_client_dynamic_pages_never_destructure_params_prop():
    # A "use client" page can't receive async params as a prop and read it
    # synchronously; it must use useParams() like the projects page does.
    offenders = []
    for path in _route_pages_with_dynamic_segments():
        source = _read(path)
        if _USE_CLIENT_RE.match(source) and _re.search(r"export\s+default\s+function\s+\w*\s*\(\s*\{\s*params", source):
            if not _NEXT16_AWAITED_RE.search(source):
                offenders.append(os.path.relpath(path, FRONTEND_DIR))
    assert not offenders, offenders


# ---------- 1. backend half: the ids that page sends ----------


@pytest.mark.parametrize("bad_id", ["undefined", "null", "[id]", "%5Bid%5D", " ", "x" * 500, "' OR '1'='1"])
def test_us_4_project_detail_bogus_route_ids_are_json_404(client, bad_id):
    # A broken destructure sends /projects/undefined; none of these may 5xx.
    headers, _ = _session_for()
    pid = urllib.parse.quote(bad_id, safe="%")
    for method, path in (
        ("get", f"/projects/{pid}"),
        ("get", f"/projects/{pid}/artifacts"),
        ("post", f"/projects/{pid}/generate/requirements"),
        ("post", f"/projects/{pid}/download"),
        ("delete", f"/projects/{pid}"),
    ):
        resp = getattr(client, method)(path, headers=headers)
        assert resp.status_code == 404, (method, path, resp.status_code, resp.text)
        assert resp.json()["detail"]


def test_us_4_project_detail_refresh_generate_download_flow_by_route_id(client):
    # refresh -> runGenerate -> refresh -> onDownload, all keyed by the id in the URL.
    headers, _ = _session_for()
    project = _create_project(client, headers)
    pid = project["id"]
    detail = client.get(f"/projects/{pid}", headers=headers).json()
    assert detail["id"] == pid and detail["artifacts"] == []
    gen = client.post(f"/projects/{pid}/generate/requirements", headers=headers)
    assert gen.status_code == 200 and gen.json()["project_id"] == pid
    detail = client.get(f"/projects/{pid}", headers=headers).json()
    assert [a["stage"] for a in detail["artifacts"]] == ["requirements"]
    assert detail["status"] == "requirements"
    blocked = client.post(f"/projects/{pid}/download", headers=headers)
    assert blocked.status_code == 402 and blocked.json()["detail"]["upgrade_url"] == "/pricing"
    _set_tier(headers, client, "mid")
    ok = client.post(f"/projects/{pid}/download", headers=headers)
    assert ok.status_code == 200
    assert zipfile.ZipFile(io.BytesIO(ok.content)).namelist() == ["requirements.md"]


def test_us_4_project_detail_other_users_id_is_403_not_leaked(client):
    owner, _ = _session_for()
    intruder, _ = _session_for()
    pid = _create_project(client, owner)["id"]
    for method, path in (("get", f"/projects/{pid}"), ("post", f"/projects/{pid}/generate/requirements"),
                         ("post", f"/projects/{pid}/download"), ("get", f"/projects/{pid}/artifacts")):
        resp = getattr(client, method)(path, headers=intruder)
        assert resp.status_code == 403, (method, path)
        assert "requirements" not in resp.text.lower() or "access" in resp.text.lower()
    assert client.get(f"/projects/{pid}/artifacts", headers=owner).json() == []


def test_us_4_project_detail_invalid_stage_on_real_id_is_422(client):
    headers, _ = _session_for()
    pid = _create_project(client, headers)["id"]
    assert client.post(f"/projects/{pid}/generate/undefined", headers=headers).status_code == 422


def test_us_4_project_detail_deleted_project_id_then_404(client):
    # The detail page refreshing after a delete must get 404, not stale data.
    headers, _ = _session_for()
    pid = _create_project(client, headers)["id"]
    assert client.delete(f"/projects/{pid}", headers=headers).status_code == 204
    assert client.get(f"/projects/{pid}", headers=headers).status_code == 404
    assert client.post(f"/projects/{pid}/generate/requirements", headers=headers).status_code == 404


# ---------- 2. tailwind-postcss shim fully gone ----------


def _frontend_walk():
    if not os.path.isdir(FRONTEND_DIR):
        pytest.skip("frontend not present")
    for root, dirs, files in os.walk(FRONTEND_DIR):
        dirs[:] = [d for d in dirs if d not in ("node_modules", ".next", ".git", ".turbo")]
        for f in files:
            yield root, f


def test_us_2_tailwind_shim_no_variant_anywhere_in_frontend():
    leftovers = [
        os.path.relpath(os.path.join(root, f), FRONTEND_DIR)
        for root, f in _frontend_walk()
        if _re.fullmatch(r"tailwind-postcss\.(c|m)?(j|t)sx?", f, _re.I)
    ]
    assert not leftovers, f"tailwind-postcss shim still present: {leftovers}"


def test_us_2_tailwind_shim_plugin_name_not_referenced():
    refs = [
        os.path.relpath(os.path.join(root, f), FRONTEND_DIR)
        for root, f in _frontend_walk()
        if f.endswith(_SOURCE_EXTS + (".cjs", ".cts", ".mts", ".json", ".css"))
        and f != "package-lock.json"
        and "crownai-tailwindcss" in _read(os.path.join(root, f))
    ]
    assert not refs, f"the crownai-tailwindcss shim plugin is still referenced in {refs}"


def test_us_2_tailwind_shim_no_hand_rolled_compile_api_usage():
    # The shim imported compile() from tailwindcss / @tailwindcss/node.
    pattern = _re.compile(
        r"""(?:import\s*\{[^}]*\bcompile\b[^}]*\}\s*from|require\()\s*\(?\s*["'](?:tailwindcss|@tailwindcss/node)["']"""
    )
    offenders = [
        os.path.relpath(os.path.join(root, f), FRONTEND_DIR)
        for root, f in _frontend_walk()
        if f.endswith(_SOURCE_EXTS + (".cjs",))
        and pattern.search(_strip_js_comments(_read(os.path.join(root, f))))
    ]
    assert not offenders, f"Tailwind compiler driven by hand in {offenders}"


def test_us_2_tailwind_shim_not_tracked_by_git():
    git = shutil.which("git")
    if not git or not os.path.isdir(FRONTEND_DIR):
        pytest.skip("git or frontend not available")
    proc = subprocess.run(
        [git, "ls-files", "--", "frontend/tailwind-postcss.*"], cwd=REPO_DIR, capture_output=True, text=True, timeout=30
    )
    if proc.returncode != 0:
        pytest.skip(f"not a git checkout: {proc.stderr.strip()}")
    tracked = [p for p in proc.stdout.splitlines() if p.strip()]
    on_disk = [p for p in tracked if os.path.exists(os.path.join(REPO_DIR, p))]
    assert not on_disk, f"shim still tracked and present: {on_disk}"


def test_us_2_tailwind_shim_package_json_scripts_and_postcss_field_clean():
    pkg = _package_json()
    for name, cmd in (pkg.get("scripts") or {}).items():
        assert "tailwind-postcss" not in cmd, f"script {name!r} still runs the shim: {cmd}"
    field = pkg.get("postcss")
    if field is not None:
        # postcss-load-config reads this field too; it must not resurrect the shim.
        text = _json.dumps(field)
        assert "tailwind-postcss" not in text and "crownai-tailwindcss" not in text, field
        assert TAILWIND_POSTCSS in text, f'package.json "postcss" field does not use {TAILWIND_POSTCSS}: {field}'


def test_us_2_tailwind_shim_postcss_config_unchanged_official_plugin_only():
    name, source = _postcss_config()
    code = _strip_js_comments(source)
    assert _re.search(r"[\"']@tailwindcss/postcss[\"']\s*:", code), f"{name} must keep {TAILWIND_POSTCSS}"
    assert "tailwind-postcss" not in code and "crownai" not in code.lower(), f"{name} still mentions the shim"


def test_us_2_tailwind_shim_declared_plugin_floor_matches_fix():
    spec = _declared_packages().get(TAILWIND_POSTCSS)
    assert spec, f"{TAILWIND_POSTCSS} not declared in package.json"
    version = _parse_version(spec)
    assert version and version[0] == 4, f"{TAILWIND_POSTCSS} {spec} is not on v4"


# ---------- 3. Header: mobile toggle gated on hydration ----------


def test_us_2_header_is_client_component_importing_hooks():
    source = _header_source()
    assert _USE_CLIENT_RE.match(source), "Header must be a client component to hydrate the toggle"
    for hook in ("useState", "useEffect"):
        assert _re.search(rf"import\s*(?:\w+\s*,\s*)?\{{[^}}]*\b{hook}\b[^}}]*\}}\s*from\s*[\"']react[\"']", source) or (
            f"React.{hook}" in source
        ), f"{hook} not imported from react"


def test_us_2_header_mobile_toggle_disabled_until_hydrated():
    source = _header_source()
    gate = _hydration_gate(source, _menu_toggle_tag(source))
    assert isinstance(gate, tuple), gate


def test_us_2_header_hydrated_flag_never_reset_to_false():
    source = _header_source()
    gate = _hydration_gate(source, _menu_toggle_tag(source))
    if not isinstance(gate, tuple):
        pytest.fail(gate)
    _, setter = gate
    assert not _re.search(rf"\b{setter}\(\s*(false|!)", source), f"{setter} can re-disable the toggle"
    assert len(_re.findall(rf"\b{setter}\(", source)) == 1, f"{setter} should be called only by the mount effect"


def test_us_2_header_toggle_disabled_depends_only_on_hydration():
    # Once open, the same button reads "Close menu"; it must stay clickable.
    tag = _menu_toggle_tag(_header_source())
    disabled = _re.findall(r"disabled=\{([^}]*)\}", tag)
    assert len(disabled) == 1, f"expected one disabled prop, got {disabled}"
    assert _re.fullmatch(r"\s*!\s*\w+\s*", disabled[0]), f"disabled={{{disabled[0]}}} also depends on other state"


def test_us_2_header_toggle_keeps_aria_expanded_bound_to_open():
    source = _header_source()
    tag = _menu_toggle_tag(source)
    m = _re.search(r"aria-expanded=\{\s*(\w+)\s*\}", tag)
    assert m, "aria-expanded must stay a plain {open} binding"
    open_var = m.group(1)
    assert _re.search(rf"const\s*\[\s*{open_var}\s*,\s*\w+\s*\]\s*=\s*(?:React\.)?useState(?:<boolean>)?\(\s*false\s*\)", source), (
        f"{open_var} must start false so SSR markup says aria-expanded=false"
    )
    gate = _hydration_gate(source, tag)
    if isinstance(gate, tuple):
        assert open_var != gate[0], "aria-expanded is bound to the hydration flag instead of open"


def test_us_2_header_toggle_label_switches_open_close():
    tag = _menu_toggle_tag(_header_source())
    open_var = _re.search(r"aria-expanded=\{\s*(\w+)\s*\}", tag).group(1)
    label = _re.search(r"aria-label=\{([^}]*)\}", tag)
    assert label, "toggle aria-label must switch with open state"
    expr = label.group(1)
    assert open_var in expr and "Close menu" in expr and "Open menu" in expr, f"aria-label={{{expr}}}"
    # The "Close menu" branch must be the one taken when open is true.
    assert _re.search(rf"{open_var}\s*\?\s*[\"'`]Close menu[\"'`]\s*:\s*[\"'`]Open menu[\"'`]", expr) or _re.search(
        rf"!\s*{open_var}\s*\?\s*[\"'`]Open menu[\"'`]\s*:\s*[\"'`]Close menu[\"'`]", expr
    ), f"labels are swapped: {expr}"


def test_us_2_header_toggle_onclick_toggles_open():
    tag = _menu_toggle_tag(_header_source())
    assert _re.search(r"onClick=\{", tag), "toggle has no onClick"
    assert _re.search(r"onClick=\{[^\n]*set\w+\(|onClick=\{\s*\w+\s*\}", tag), "onClick does not change open state"


def test_us_2_header_toggle_is_type_button_and_not_hidden_while_disabled():
    tag = _menu_toggle_tag(_header_source())
    assert 'type="submit"' not in tag
    cls = _re.search(r"className=(?:\"([^\"]*)\"|\{`([^`]*)`\})", tag)
    if cls:
        classes = cls.group(1) or cls.group(2)
        assert not _re.search(r"\bdisabled:(hidden|invisible|opacity-0)\b", classes), (
            "toggle disappears while disabled; the SSR button must still render"
        )


def test_us_2_header_mobile_nav_rendered_from_open_state():
    source = _header_source()
    open_var = _re.search(r"aria-expanded=\{\s*(\w+)\s*\}", _menu_toggle_tag(source)).group(1)
    assert _re.search(rf"\{{\s*{open_var}\s*&&|\{{\s*{open_var}\s*\?", source), "mobile nav is not rendered from open"


def test_us_2_header_aria_controls_points_at_rendered_nav():
    source = _header_source()
    m = _re.search(r"aria-controls=[\"']([\w-]+)[\"']", _menu_toggle_tag(source))
    if not m:
        pytest.skip("toggle has no aria-controls")
    assert _re.search(rf"id=[\"']{m.group(1)}[\"']", source), f"aria-controls={m.group(1)} has no matching element"


def test_us_2_header_mobile_nav_adds_no_horizontal_overflow():
    # 100vw includes the scrollbar width -> horizontal scroll on mobile.
    source = _header_source()
    assert not _re.search(r"\bw-screen\b|100vw|min-w-\[\d{3,}px\]", source), "Header uses a width wider than the viewport"


# ---------- 4. globals.css: .btn-primary must not move under the pointer ----------


def test_us_4_btn_primary_hover_rule_has_no_shift():
    rules = _css_rules(_globals_css())
    hover = _state_rules(rules, ".btn-primary", "hover")
    assert hover, "no .btn-primary:hover rule found in globals.css"
    shifts = {sel: _shifting_declarations(d) for sel, d in hover if _shifting_declarations(d)}
    assert not shifts, f".btn-primary hover moves/resizes the button: {shifts}"


@pytest.mark.parametrize("state", ["focus", "active"])
def test_us_4_btn_primary_click_states_have_no_shift(state):
    # A slow click focuses the button on mousedown and holds :active until
    # mouseup -- a shift there moves it under the pointer just the same.
    rules = _css_rules(_globals_css())
    shifts = {sel: _shifting_declarations(d) for sel, d in _state_rules(rules, ".btn-primary", state) if _shifting_declarations(d)}
    assert not shifts, f".btn-primary :{state} moves/resizes the button: {shifts}"


def test_us_4_btn_primary_hover_keeps_brightness_saturate_filter():
    rules = _css_rules(_globals_css())
    filters = [v for _, d in _state_rules(rules, ".btn-primary", "hover") for p, v in _declarations(d) if p == "filter"]
    assert filters, "hover feedback was removed entirely; keep the filter brightness/saturate effect"
    assert any(_re.search(r"brightness\(|saturate\(", f) for f in filters), filters


def test_us_4_btn_primary_hover_still_skips_disabled_button():
    rules = _css_rules(_globals_css())
    sels = [s for s, _ in _state_rules(rules, ".btn-primary", "hover")]
    assert any(":not(:disabled)" in s or ":enabled" in s for s in sels), f"hover applies to disabled buttons too: {sels}"


def test_us_4_btn_primary_base_rule_applies_no_hover_shift_utilities():
    rules = _css_rules(_globals_css())
    for sels, decls in rules:
        if any(_re.search(r"\.btn-primary(?![\w-])(?!:)", s) for s in sels):
            for prop, value in _declarations(decls):
                if prop == "@apply":
                    assert not _re.search(r"\b(?:hover|focus|active):-?(?:translate|scale|m[trblxy]?)-", value), (
                        f".btn-primary @apply adds a state shift: {value}"
                    )


def test_us_4_btn_primary_no_shift_in_any_css_file():
    # The same lift could be re-added in another stylesheet the app imports.
    found = []
    for root, f in _frontend_walk():
        if f.endswith(".css"):
            path = os.path.join(root, f)
            for state in ("hover", "focus", "active"):
                for sel, d in _state_rules(_css_rules(_read(path)), ".btn-primary", state):
                    if _shifting_declarations(d):
                        found.append(f"{os.path.relpath(path, FRONTEND_DIR)}: {sel}")
    assert not found, found


def test_us_4_generate_project_button_has_no_shift_utilities():
    tag = _submit_button_tag(_crown_ai_page())
    assert not _re.search(r"\b(?:hover|focus|active|focus-visible):-?(?:translate|scale|m[trblxy]?|top|p[trblxy]?)-", tag), (
        f"Generate Project button moves on hover/click via utilities: {tag}"
    )


def test_us_4_generate_project_button_still_uses_btn_primary():
    # The CSS fix only helps if the submit button is the one styled by it.
    assert "btn-primary" in _submit_button_tag(_crown_ai_page())


def test_us_4_name_field_touched_only_on_change_not_blur():
    # The fix analysis: the error cannot appear on blur because touched is
    # set in onChange; keep it that way so nothing else shifts the button.
    tag = _input_tag(_crown_ai_page(), "proj-name")
    blur = _re.search(r"onBlur=\{([^\n]*)", tag)
    assert not (blur and "ouched" in blur.group(1)), "#proj-name marks itself touched on blur"
    change = _re.search(r"onChange=\{([^\n]*)", tag)
    assert change, "#proj-name has no onChange"


# ===========================================================================
# Gap-fill for the same four-fix pass: edge cases the section above leaves open.
#   - The defects must be closed in the frontend, not by loosening the
#     original detector tests (sync-params regex, shim extension list).
#   - projects/[id]: useParams() called once and `id` never re-bound/shadowed,
#     so every `${id}` in a request URL really is the route id.
#   - Header: hydration must gate the toggle with `disabled`, not hide it
#     (`{hydrated && <button>}` drops the SSR button the UI test inspects),
#     and plain nav links must not be gated (they work without JS).
#   - globals.css: other ways the button box can move/resize on hover or
#     between :disabled and enabled (border width, font-weight, letter-spacing,
#     zoom) -- getBoundingClientRect sees all of them; filter/box-shadow it doesn't.
# ===========================================================================

def _extra_size_changes(decls):
    out = []
    for prop, value in _declarations(decls):
        if prop in ("border", "border-top", "border-bottom", "border-left", "border-right"):
            # Colour-only borders are fine; a width keyword/length changes the box.
            if _re.search(r"\b\d*\.?\d+(px|rem|em)\b|\b(thin|medium|thick)\b", value):
                out.append(f"{prop}: {value}")
        elif prop in ("font-weight", "letter-spacing", "zoom"):
            out.append(f"{prop}: {value}")
    return out


def _this_test_file():
    with open(os.path.abspath(__file__), encoding="utf-8") as fh:
        return fh.read()


def _test_body(name):
    src = _this_test_file()
    m = _re.search(rf"^def {name}\(.*?(?=^def |\Z)", src, _re.S | _re.M)
    assert m, f"{name} was removed; the defect must be fixed in source, not by deleting its test"
    return m.group(0)


def test_us_2_original_sync_params_detector_not_weakened():
    body = _test_body("test_us_2_frontend_dynamic_route_pages_await_params")
    assert r"\bparams\.\w+" in body
    assert r"await\s+params|use\(\s*params\s*\)" in body
    assert "assert not offenders" in body
    assert "skip" not in body and "xfail" not in body


def test_us_2_original_shim_deleted_detector_not_weakened():
    body = _test_body("test_us_2_tailwind_local_shim_file_deleted")
    for ext in ('".js"', '".cjs"', '".mjs"', '".ts"'):
        assert ext in body, f"shim check no longer covers {ext}"
    assert "xfail" not in body


def test_us_4_extra_size_helper_flags_width_changes_but_not_colour():
    assert _extra_size_changes("border: 2px solid red; font-weight: 700") == ["border: 2px solid red", "font-weight: 700"]
    assert _extra_size_changes("border-color: red; border: solid transparent; filter: brightness(1.1)") == []
    assert _extra_size_changes("box-shadow: 0 4px 12px rgb(0 0 0/.2); outline: 2px solid blue") == []


# ---------- projects/[id]: route id binding ----------


def test_us_2_project_detail_calls_use_params_exactly_once():
    calls = _re.findall(r"\buseParams\s*(?:<[^>]*>)?\s*\(", _strip_js_comments(_project_detail_page()))
    assert len(calls) == 1, f"useParams() called {len(calls)} times"


def test_us_2_project_detail_id_not_rebound_or_shadowed():
    # A second `id` binding (e.g. `.map((id) => ...)` or `const id = ...`)
    # would make some `${id}` URLs point at something other than the route.
    code = _strip_js_comments(_project_detail_page())
    decls = _re.findall(r"\b(?:const|let|var)\s+id\b\s*=", code)
    assert not decls, f"`id` re-declared: {decls}"
    params = _re.findall(r"\(\s*id\s*(?::[^)]*)?\)\s*=>|\bfunction\s*\w*\s*\(\s*id\b|\(\s*\{\s*id\s*\}\s*\)\s*=>", code)
    assert not params, f"`id` shadowed by a callback parameter: {params}"
    destructures = _re.findall(r"\{\s*id\s*\}\s*=", code)
    assert len(destructures) == 1, f"expected one `{{ id }} =` destructure, got {len(destructures)}"


def test_us_2_project_detail_id_not_coerced_from_array_or_stringified():
    # useParams<{ id: string }>() already types it; String(params)/[0] hacks
    # would send "undefined" or "a,b" to the API.
    code = _strip_js_comments(_project_detail_page())
    assert not _re.search(r"\bid\s*\[\s*0\s*\]|String\(\s*id\s*\)|\bid\s+as\s+string\[\]", code)


# ---------- Header: gate, don't hide ----------


def test_us_2_header_toggle_not_conditionally_rendered_on_hydration():
    source = _header_source()
    gate = _hydration_gate(source, _menu_toggle_tag(source))
    if not isinstance(gate, tuple):
        pytest.fail(gate)
    flag = gate[0]
    assert not _re.search(rf"\{{\s*{flag}\s*&&\s*\(?\s*<button", source), (
        "toggle is hidden until hydration; the SSR button must render (disabled) instead"
    )
    assert not _re.search(rf"\{{\s*!?\s*{flag}\s*\?", source), "hydration flag switches markup (hydration mismatch risk)"
    assert not _re.search(rf"if\s*\(\s*!\s*{flag}\s*\)\s*return\b", source), "Header renders nothing before hydration"


def test_us_2_header_nav_links_not_gated_on_hydration():
    source = _header_source()
    for tag_name in ("a", "Link"):
        for tag in _jsx_open_tags(source, tag_name):
            assert "disabled=" not in tag and "aria-disabled" not in tag, f"nav link gated: {tag}"


def test_us_2_header_only_one_hydration_gated_element():
    source = _header_source()
    gate = _hydration_gate(source, _menu_toggle_tag(source))
    if not isinstance(gate, tuple):
        pytest.fail(gate)
    uses = _re.findall(rf"disabled=\{{\s*!\s*{gate[0]}\s*\}}", source)
    assert len(uses) == 1, f"{len(uses)} elements gated on {gate[0]}; only the mobile toggle needs it"


# ---------- globals.css: no box change between states ----------


@pytest.mark.parametrize("state", ["hover", "focus", "active"])
def test_us_4_btn_primary_states_change_no_border_width_or_font_metrics(state):
    rules = _css_rules(_globals_css())
    found = {sel: _extra_size_changes(d) for sel, d in _state_rules(rules, ".btn-primary", state) if _extra_size_changes(d)}
    assert not found, f".btn-primary :{state} resizes the button: {found}"


def test_us_4_btn_primary_disabled_to_enabled_does_not_move_button():
    # Typing the name enables the button right before the slow click; a
    # :disabled rule with its own transform/size would shift it at that moment.
    rules = _css_rules(_globals_css())
    found = {}
    for sel, d in _state_rules(rules, ".btn-primary", "disabled"):
        if ":not(:disabled)" in sel:
            continue
        bad = _shifting_declarations(d) + _extra_size_changes(d)
        if bad:
            found[sel] = bad
    assert not found, f".btn-primary:disabled changes the box vs enabled: {found}"


def test_us_4_btn_primary_hover_lift_replacement_is_non_layout_only():
    # If a lift effect is kept, it must be box-shadow/filter (no box change).
    rules = _css_rules(_globals_css())
    for _, d in _state_rules(rules, ".btn-primary", "hover"):
        for prop, _ in _declarations(d):
            assert prop in ("filter", "box-shadow", "background", "background-color", "background-image", "color",
                            "border-color", "opacity", "outline", "outline-color", "cursor", "text-decoration",
                            "--tw-shadow", "transition") or prop.startswith("--"), (
                f".btn-primary:hover sets {prop}; only paint-only properties keep the button still"
            )


# ===========================================================================
# Second gap-fill for the four-fix pass:
#   - Shim: nothing (tsconfig include, ignore files, next.config loaders)
#     still names the deleted `tailwind-postcss` path -- a dangling reference
#     breaks `tsc`/`next build` once the file is gone, or hides a stale copy.
#   - Header: a global `:disabled` rule must not hide/collapse the SSR toggle;
#     the pre-hydration button has to be visible with aria-expanded="false"
#     for the UI test (and real users) to see it.
#   - globals.css: the .btn-primary base rule must itself sit at rest
#     (no transform/translate), so removing the hover lift leaves it still.
#   - projects/[id] backend half: an empty id (`useParams` returning "")
#     produces `/projects//...` URLs; they must be clean 4xx, never 5xx.
# ===========================================================================

_SHIM_PATH_RE = _re.compile(r"(?<![\w@/-])tailwind-postcss(?:\.(?:c|m)?(?:j|t)sx?)?\b")


def test_us_2_tailwind_shim_path_regex_semantics():
    assert _SHIM_PATH_RE.search('"include": ["tailwind-postcss.js"]')
    assert _SHIM_PATH_RE.search("require('./tailwind-postcss')")
    assert _SHIM_PATH_RE.search("tailwind-postcss.cjs\n")
    assert not _SHIM_PATH_RE.search('"@tailwindcss/postcss": {}')
    assert not _SHIM_PATH_RE.search("crownai-tailwind-postcss-plugin-docs")


def test_us_2_tailwind_shim_path_not_referenced_by_any_config_or_source():
    offenders = []
    for root, f in _frontend_walk():
        if f == "package-lock.json":
            continue
        is_ignore = f.startswith(".") and f.endswith("ignore")
        if not (is_ignore or f.endswith(_SOURCE_EXTS + (".cjs", ".cts", ".mts", ".json", ".css"))):
            continue
        path = os.path.join(root, f)
        text = _read(path)
        if not is_ignore and f.endswith(_SOURCE_EXTS + (".cjs", ".cts", ".mts")):
            text = _strip_js_comments(text)
        if _SHIM_PATH_RE.search(text):
            offenders.append(os.path.relpath(path, FRONTEND_DIR))
    assert not offenders, f"deleted tailwind-postcss shim still referenced in {offenders}"


def test_us_2_tailwind_shim_only_one_postcss_config_loads_tailwind():
    # A second postcss config (e.g. a leftover .cjs) could shadow the official one.
    configs = _postcss_configs()
    assert len(configs) == 1, f"expected exactly one postcss config, got {configs}"


_HIDE_DECL_RE = _re.compile(
    r"^(display\s*:\s*none|visibility\s*:\s*hidden|opacity\s*:\s*0(?:\.0+)?(?:\s*!important)?$|"
    r"width\s*:\s*0(?:px)?$|height\s*:\s*0(?:px)?$)",
    _re.I,
)


def test_us_2_global_disabled_rules_do_not_hide_buttons():
    # The SSR menu toggle is disabled until hydration; a blanket
    # `button:disabled { display:none }` would make it vanish instead.
    found = []
    for root, f in _frontend_walk():
        if not f.endswith(".css"):
            continue
        path = os.path.join(root, f)
        for sels, decls in _css_rules(_read(path)):
            for sel in sels:
                if (":disabled" in sel and ":not(:disabled)" not in sel) or "[disabled]" in sel or "[aria-disabled" in sel:
                    if "btn-primary" in sel or "btn-secondary" in sel or _re.split(r"[:\[]", sel)[0].strip() in ("button", "", "*"):
                        for d in decls.split(";"):
                            if _HIDE_DECL_RE.search(d.strip()):
                                found.append(f"{os.path.relpath(path, FRONTEND_DIR)}: {sel} {{{d.strip()}}}")
    assert not found, f"disabled buttons are hidden: {found}"


def test_us_2_header_toggle_has_no_hiding_disabled_utilities_or_inline_style():
    tag = _menu_toggle_tag(_header_source())
    assert not _re.search(r"\bdisabled:(?:hidden|invisible|opacity-0|w-0|h-0)\b", tag), tag
    style = _re.search(r"style=\{\{([^}]*)\}\}", tag)
    if style:
        assert not _re.search(r"display\s*:\s*[\"']none|visibility\s*:\s*[\"']hidden", style.group(1)), style.group(1)


def test_us_2_header_hydrated_flag_not_used_for_aria_or_label():
    # Gate only `disabled`; aria-expanded / label must not change on hydration
    # (that would be an SSR/CSR mismatch and confuse the UI test).
    source = _header_source()
    tag = _menu_toggle_tag(source)
    gate = _hydration_gate(source, tag)
    if not isinstance(gate, tuple):
        pytest.fail(gate)
    flag = gate[0]
    for attr in ("aria-expanded", "aria-label"):
        m = _re.search(rf"{attr}=\{{([^}}]*)\}}", tag)
        if m:
            assert not _re.search(rf"\b{flag}\b", m.group(1)), f"{attr} depends on {flag}: {m.group(1)}"


def test_us_4_btn_primary_base_rule_at_rest():
    rules = _css_rules(_globals_css())
    base = [
        (sel, d)
        for sels, d in rules
        for sel in sels
        if _re.fullmatch(r"\.btn-primary", sel.strip())
    ]
    assert base, "no .btn-primary base rule in globals.css"
    for sel, d in base:
        moved = [
            f"{p}: {v}"
            for p, v in _declarations(d)
            if p in ("transform", "translate", "scale") and v.lower() not in ("none", "initial", "unset")
        ]
        assert not moved, f"{sel} is offset at rest: {moved}"


def test_us_4_btn_primary_transition_does_not_animate_layout_props():
    # The 0.15s transform transition is what made the 1px lift visible
    # mid-wait; if transform is gone it may stay listed, but nothing that
    # changes the box (top/margin/width/height) may be transitioned.
    rules = _css_rules(_globals_css())
    for sels, d in rules:
        if any(".btn-primary" in s for s in sels):
            for p, v in _declarations(d):
                if p in ("transition", "transition-property"):
                    assert not _re.search(r"\b(top|margin[\w-]*|width|height|padding[\w-]*|inset)\b", v), f"{p}: {v}"


@pytest.mark.parametrize("suffix", ["", "/artifacts", "/generate/requirements", "/download"])
def test_us_4_project_detail_empty_route_id_is_clean_4xx(client, suffix):
    headers, _ = _session_for()
    for method in ("get", "post", "delete"):
        resp = getattr(client, method)(f"/projects//{suffix.lstrip('/')}" if suffix else "/projects//", headers=headers)
        assert resp.status_code < 500, (method, suffix, resp.status_code, resp.text)
        if suffix:
            assert resp.status_code in (404, 405, 307), (method, suffix, resp.status_code)


def test_us_4_project_detail_url_encoded_real_id_resolves(client):
    # `${encodeURIComponent(id)}` of a real id must hit the same project.
    headers, _ = _session_for()
    pid = _create_project(client, headers)["id"]
    resp = client.get(f"/projects/{urllib.parse.quote(str(pid), safe='')}", headers=headers)
    assert resp.status_code == 200 and resp.json()["id"] == pid


def test_us_4_project_detail_unauthenticated_route_id_is_401_not_404(client):
    # Before sign-in the page's refresh() must be told to log in, not that
    # the project is missing -- and the project's existence is not leaked.
    owner, _ = _session_for()
    pid = _create_project(client, owner)["id"]
    real = client.get(f"/projects/{pid}")
    fake = client.get("/projects/undefined")
    assert real.status_code == fake.status_code == 401, (real.status_code, fake.status_code)


# ===========================================================================
# Human-requested coverage (Testing defects pass: five reports, one root
# cause -- frontend/tailwind-postcss.js, the hand-rolled "crownai-tailwindcss"
# PostCSS plugin calling tailwindcss's compile(), was never deleted). What the
# fix actually has to achieve, beyond "the file is gone":
#   - deleted from git's index too (`git rm`), so `git ls-files` prints
#     nothing -- an on-disk delete with the removal left unstaged still ships it;
#   - not resurrected under another name, case or extension, anywhere in
#     the repo, and not staged under a new path;
#   - not "fixed" by renaming the plugin string: no local PostCSS plugin
#     and no hand-driven Tailwind compile() survives in any spelling;
#   - the official @tailwindcss/postcss that replaces it is installed and
#     really is the upstream plugin (the `npm install` follow-up);
#   - the five original detectors still exist un-skipped and un-weakened.
# Detector regexes are unit-tested on a reconstruction of the shim so they
# are known to catch it even when the frontend is absent.
# ===========================================================================

_SHIM_FILENAME_RE = _re.compile(r"tailwind[-_.]?postcss\.(?:c|m)?(?:j|t)sx?", _re.I)
_SHIM_PLUGIN_NAME = "crownai-tailwindcss"
_LOCAL_POSTCSS_PLUGIN_RE = _re.compile(
    r"\bpostcssPlugin\s*:|(?:module\.exports|exports|\w+)\.postcss\s*=\s*true\b"
)
_HAND_COMPILE_RE = _re.compile(
    r"""(?:import\s*\{[^}]*\bcompile\b[^}]*\}\s*from\s*["'](?:tailwindcss|@tailwindcss/node)["']"""
    r"""|\bconst\s*\{[^}]*\bcompile\b[^}]*\}\s*=\s*(?:await\s+)?(?:require|import)\(\s*["'](?:tailwindcss|@tailwindcss/node)["']"""
    r"""|(?:require|import)\(\s*["'](?:tailwindcss|@tailwindcss/node)["']\s*\)\s*\)?\s*\.\s*compile\b"""
    r"""|(?:require|import)\(\s*["']@tailwindcss/node["']\s*\))"""
)
_SHIM_SCAN_EXTS = _SOURCE_EXTS + (".cjs", ".cts", ".mts")
_REPO_SKIP_DIRS = {"node_modules", ".next", ".git", ".turbo", ".venv", "venv", "__pycache__", ".pytest_cache", "dist", "build"}

# Reconstruction of the deleted shim, per the defect reports (line 10 require,
# postcssPlugin at ~64, dependency messages at ~98/101).
_SHIM_SAMPLE = """
const path = require("path");
const fs = require("fs");
const { compile } = require("tailwindcss");

module.exports = () => ({
  postcssPlugin: "crownai-tailwindcss",
  async Once(root, { result }) {
    const compiler = await compile(root.toString(), { base: process.cwd() });
    result.messages.push({ type: "dependency", plugin: "crownai-tailwindcss", file: "x" });
    result.messages.push({ type: "dir-dependency", plugin: "crownai-tailwindcss", dir: "y" });
  },
});
module.exports.postcss = true;
"""
_OFFICIAL_CONFIG_SAMPLE = 'module.exports = {\n  plugins: {\n    "@tailwindcss/postcss": {},\n  },\n};\n'


def _repo_walk():
    for root, dirs, files in os.walk(REPO_DIR):
        dirs[:] = [d for d in dirs if d not in _REPO_SKIP_DIRS]
        for f in files:
            yield root, f


def _git(*args):
    git = shutil.which("git")
    if not git:
        pytest.skip("git not available")
    proc = subprocess.run(
        [git, "-c", "core.quotepath=off", *args], cwd=REPO_DIR, capture_output=True, text=True, timeout=60
    )
    if proc.returncode not in (0, 1) or "not a git repository" in proc.stderr:
        pytest.skip(f"not a git checkout: {proc.stderr.strip()}")
    return proc


def _frontend_scan_files():
    for root, f in _frontend_walk():
        if f.endswith(_SHIM_SCAN_EXTS):
            yield os.path.join(root, f)


def _is_frontend_test_file(rel_path):
    # The UI suite's own detectors (frontend/tests/test_*.py) must name the
    # shim to look for it; that is not the app referencing it. Only Python
    # test modules are exempt -- a .js shim moved into tests/ is still caught
    # (by name and by content), as is any non-test file there.
    parts = rel_path.replace("\\", "/").split("/")
    name = parts[-1]
    return name.endswith(".py") and (name.startswith("test_") or name == "conftest.py" or "tests" in parts[:-1])


# ---------- detector self-tests (always run) ----------


def test_us_2_tailwind_shim_detectors_catch_reconstructed_shim():
    code = _strip_js_comments(_SHIM_SAMPLE)
    assert _SHIM_PLUGIN_NAME in code
    assert _LOCAL_POSTCSS_PLUGIN_RE.search(code)
    assert _HAND_COMPILE_RE.search(code)
    assert _SHIM_FILENAME_RE.fullmatch("tailwind-postcss.js")


def test_us_2_tailwind_shim_detectors_ignore_official_postcss_config():
    code = _strip_js_comments(_OFFICIAL_CONFIG_SAMPLE)
    assert _SHIM_PLUGIN_NAME not in code
    assert not _LOCAL_POSTCSS_PLUGIN_RE.search(code)
    assert not _HAND_COMPILE_RE.search(code)
    assert not _SHIM_FILENAME_RE.fullmatch("postcss.config.js")


@pytest.mark.parametrize(
    "name",
    ["tailwind-postcss.js", "tailwind-postcss.cjs", "tailwind-postcss.mjs", "tailwind-postcss.ts",
     "tailwind-postcss.tsx", "Tailwind-PostCSS.JS", "tailwind_postcss.js", "tailwind.postcss.cjs", "tailwindpostcss.mjs"],
)
def test_us_2_tailwind_shim_filename_detector_covers_renames(name):
    assert _SHIM_FILENAME_RE.fullmatch(name)


@pytest.mark.parametrize(
    "name", ["postcss.config.js", "tailwind.config.ts", "tailwind-postcss.md", "tailwind-postcss.js.map", "globals.css"]
)
def test_us_2_tailwind_shim_filename_detector_no_false_positives(name):
    assert not _SHIM_FILENAME_RE.fullmatch(name)


@pytest.mark.parametrize(
    "snippet",
    [
        'const { compile } = require("tailwindcss");',
        "const { compile, Features } = require('tailwindcss')",
        'import { compile } from "tailwindcss";',
        'import { compile as twCompile } from "@tailwindcss/node";',
        'const compiler = await require("tailwindcss").compile(css)',
        'const { compile } = await import("tailwindcss");',
        'const tw = (await import("tailwindcss")).compile;',
        'const node = require("@tailwindcss/node");',
    ],
)
def test_us_2_tailwind_hand_compile_detector_catches_every_spelling(snippet):
    assert _HAND_COMPILE_RE.search(snippet), snippet


@pytest.mark.parametrize(
    "snippet",
    [
        'plugins: { "@tailwindcss/postcss": {} }',
        'const defaultTheme = require("tailwindcss/defaultTheme");',
        'import type { Config } from "tailwindcss";',
        '@import "tailwindcss";',
        "const compile = (s) => s; compile(x);",
    ],
)
def test_us_2_tailwind_hand_compile_detector_no_false_positives(snippet):
    assert not _HAND_COMPILE_RE.search(snippet), snippet


@pytest.mark.parametrize(
    "snippet",
    [
        'module.exports = () => ({ postcssPlugin: "renamed-tailwind" });',
        "module.exports.postcss = true;",
        "plugin.postcss = true",
        "exports.postcss = true",
    ],
)
def test_us_2_tailwind_local_plugin_detector_catches_renamed_shim(snippet):
    # "Don't just rename the plugin string" -- any local PostCSS plugin counts.
    assert _LOCAL_POSTCSS_PLUGIN_RE.search(snippet), snippet


# ---------- the shim is gone everywhere, under any name ----------


def test_us_2_tailwind_shim_no_variant_anywhere_in_repo():
    # Moving it to the repo root, scripts/ or backend/ is not deleting it.
    leftovers = [
        os.path.relpath(os.path.join(root, f), REPO_DIR)
        for root, f in _repo_walk()
        if _SHIM_FILENAME_RE.fullmatch(f)
    ]
    assert not leftovers, f"tailwind-postcss shim still present: {leftovers}"


def test_us_2_tailwind_shim_absent_from_git_index_entirely():
    # The human's confirm step: `git ls-files -- frontend/tailwind-postcss.*`
    # prints nothing. Deleting on disk without `git rm` leaves it in the index
    # (and in the next commit); the older test only caught tracked+on-disk.
    proc = _git("ls-files", "-z")
    tracked = [p for p in proc.stdout.split("\0") if p]
    hits = [p for p in tracked if _SHIM_FILENAME_RE.fullmatch(p.rsplit("/", 1)[-1])]
    assert not hits, f"shim still in git index (run `git rm`): {hits}"


def test_us_2_tailwind_shim_exact_confirm_command_prints_nothing():
    proc = _git("ls-files", "--", "frontend/tailwind-postcss.*")
    assert proc.stdout.strip() == "", f"`git ls-files -- frontend/tailwind-postcss.*` printed: {proc.stdout!r}"


def test_us_2_tailwind_shim_not_staged_under_another_path():
    # `git grep --cached` reads the index: a renamed copy staged for commit
    # is caught even if the working-tree file was later edited.
    proc = _git("grep", "--cached", "-l", "-F", _SHIM_PLUGIN_NAME, "--", "frontend")
    staged = [
        p for p in proc.stdout.splitlines()
        if p.strip() and not p.endswith("package-lock.json") and not _is_frontend_test_file(p)
    ]
    assert not staged, f"{_SHIM_PLUGIN_NAME} still in staged frontend files: {staged}"


def test_us_2_tailwind_shim_deletion_not_left_unstaged():
    # ` D frontend/tailwind-postcss.js` = deleted on disk, still in the index.
    proc = _git("status", "--porcelain", "--", "frontend")
    unstaged = [
        line for line in proc.stdout.splitlines()
        if len(line) > 3 and line[1] == "D" and _SHIM_FILENAME_RE.fullmatch(line[3:].strip().rsplit("/", 1)[-1])
    ]
    assert not unstaged, f"shim deletion not staged; run `git rm`: {unstaged}"


# ---------- not "fixed" by renaming: no local plugin, no hand compile ----------


def test_us_2_tailwind_no_local_postcss_plugin_in_frontend_sources():
    offenders = [
        os.path.relpath(p, FRONTEND_DIR)
        for p in _frontend_scan_files()
        if _LOCAL_POSTCSS_PLUGIN_RE.search(_strip_js_comments(_read(p)))
    ]
    assert not offenders, f"hand-written PostCSS plugin(s) still in frontend: {offenders}"


def test_us_2_tailwind_no_hand_compile_in_any_spelling():
    offenders = [
        os.path.relpath(p, FRONTEND_DIR)
        for p in _frontend_scan_files()
        if _HAND_COMPILE_RE.search(_strip_js_comments(_read(p)))
    ]
    assert not offenders, f"Tailwind compile() driven by hand in {offenders}"


def test_us_2_tailwind_shim_plugin_name_not_in_any_text_file():
    # Also .md/.yml/.txt/.html and dotfiles -- docs or CI pointing at the shim.
    offenders = []
    for root, f in _frontend_walk():
        if f == "package-lock.json" or f.endswith((".png", ".jpg", ".jpeg", ".ico", ".gif", ".webp", ".woff", ".woff2")):
            continue
        if _is_frontend_test_file(os.path.relpath(os.path.join(root, f), FRONTEND_DIR)):
            continue
        try:
            text = _read(os.path.join(root, f))
        except (UnicodeDecodeError, OSError):
            continue
        if _SHIM_PLUGIN_NAME in text:
            offenders.append(os.path.relpath(os.path.join(root, f), FRONTEND_DIR))
    assert not offenders, f"{_SHIM_PLUGIN_NAME} still mentioned in {offenders}"


def test_us_2_tailwind_postcss_config_loads_no_relative_module():
    # The config is the only thing PostCSS loads; it must load packages only.
    name, source = _postcss_config()
    code = _strip_js_comments(source)
    rel = _re.findall(r"""(?:require\(|import\(|from)\s*["'](\.{1,2}/[^"']*)["']""", code)
    assert not rel, f"{name} loads local module(s) {rel}"
    assert not _LOCAL_POSTCSS_PLUGIN_RE.search(code), f"{name} defines a PostCSS plugin inline"


# ---------- the official plugin that replaces it ----------


def test_us_2_tailwind_installed_plugin_is_upstream_not_the_shim():
    # Proves the `npm install` follow-up landed: the resolved plugin is the
    # real package, a PostCSS plugin creator, and not named like the shim.
    script = (
        "const p=require('@tailwindcss/postcss');const f=typeof p==='function'?p:p.default;"
        "const inst=f({});const path=require.resolve('@tailwindcss/postcss');"
        "process.stdout.write(JSON.stringify({postcss:f.postcss===true,name:inst&&inst.postcssPlugin,path}))"
    )
    proc = _run_node(script)
    assert proc.returncode == 0, f"@tailwindcss/postcss failed to load; run npm install in frontend/: {proc.stderr[-1500:]}"
    info = _json.loads(proc.stdout)
    assert info["postcss"], "@tailwindcss/postcss export is not a PostCSS plugin creator"
    assert info["name"] and _SHIM_PLUGIN_NAME not in info["name"], f"resolved plugin is named {info['name']!r}"
    norm = info["path"].replace("\\", "/")
    assert "/node_modules/@tailwindcss/postcss/" in norm, f"plugin resolved to {info['path']}"


def test_us_2_tailwind_postcss_config_plugins_all_resolve_into_node_modules():
    # Every plugin the config names must come from node_modules; a shim that
    # someone published as a local `file:` dependency would show up here.
    name, _ = _postcss_config()
    script = (
        f"const c=require('./{name}');const cfg=c&&c.default?c.default:c;"
        "const keys=Object.keys((cfg&&cfg.plugins)||{});"
        "process.stdout.write(JSON.stringify(keys.map(k=>[k,require.resolve(k)])))"
    )
    if name.endswith((".mjs", ".ts")) or (name.endswith(".js") and _package_json().get("type") == "module"):
        pytest.skip(f"{name} cannot be required from node -e")
    proc = _run_node(script)
    assert proc.returncode == 0, proc.stderr[-1500:]
    pairs = _json.loads(proc.stdout)
    assert [k for k, _ in pairs] == [TAILWIND_POSTCSS], pairs
    for key, path in pairs:
        assert "/node_modules/" in path.replace("\\", "/"), f"{key} resolves to {path}"


def test_us_2_tailwind_postcss_not_a_local_file_dependency():
    for section in ("dependencies", "devDependencies"):
        for name, spec in (_package_json().get(section) or {}).items():
            if "tailwind" in name:
                assert not str(spec).startswith(("file:", "link:", "./", "../")), f"{name} -> {spec} is a local path"


# ---------- the original five detectors stay intact ----------

_ORIGINAL_SHIM_TESTS = {
    "test_us_2_tailwind_local_shim_file_deleted": ["tailwind-postcss", '".js"', '".cjs"', '".mjs"', '".ts"', "assert not os.path.exists"],
    "test_us_2_tailwind_shim_no_variant_anywhere_in_frontend": ["tailwind-postcss", "(c|m)?(j|t)sx?", "assert not leftovers"],
    "test_us_2_tailwind_shim_plugin_name_not_referenced": ["crownai-tailwindcss", "assert not refs"],
    "test_us_2_tailwind_shim_no_hand_rolled_compile_api_usage": ["compile", "tailwindcss", "@tailwindcss/node", "assert not offenders"],
    "test_us_2_tailwind_shim_not_tracked_by_git": ["ls-files", "frontend/tailwind-postcss.*", "assert not"],
}


@pytest.mark.parametrize("name", sorted(_ORIGINAL_SHIM_TESTS))
def test_us_2_tailwind_original_shim_detectors_not_weakened(name):
    body = _test_body(name)
    for needle in _ORIGINAL_SHIM_TESTS[name]:
        assert needle in body, f"{name} no longer contains {needle!r}"
    assert "xfail" not in body
    src = _this_test_file()
    decorated = _re.search(rf"@pytest\.mark\.(?:skip|skipif|xfail)[^\n]*\n(?:@[^\n]*\n)*def {name}\(", src)
    assert not decorated, f"{name} is skipped/xfailed by a decorator"


# ===========================================================================
# Human-requested coverage (Testing defects pass -- shim removal + live UI):
#   Items 1-14 (touched-on-change, tsconfig scope, the tailwind shim gone from
#   disk, the git index, renamed copies, plugin name and hand-driven
#   compile()) are covered by the test_us_4_create_form_* and
#   test_us_2_tailwind_* sections above. This section covers the remaining
#   UI failures from the backend's side:
#   15/16/18/19/20. "Live backend not reachable from the browser": a real
#      uvicorn process on a real socket must answer /health, sign-in, pricing
#      plans, lead capture and contact within the browser's 5 s timeout,
#      started from either directory, cross-origin from the UI port. The
#      frontend's default API base must also point where uvicorn listens.
#   17. Creating a project yields all five artifacts, in order, within a free
#      user's daily quota, and shows up in the workspace listing.
#   21/23. Brand gold: .btn-primary on the site must end up with the same
#      gold gradient as the Crown AI sign-in surface (not `none`).
# ===========================================================================

import http.client  # noqa: E402

_UI_ORIGIN = "http://localhost:3020"
_BROWSER_TIMEOUT_S = 5.0
_BRAND_GOLD_STOPS = ["#fcd34d", "#eab308", "#d97706"]
_TAILWIND_GOLD = {"amber-300": "#fcd34d", "yellow-500": "#eab308", "amber-600": "#d97706"}


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _live_request(port, method, path, body=None, headers=None, timeout=_BROWSER_TIMEOUT_S):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        data = _json.dumps(body).encode() if body is not None else None
        hdrs = {"Origin": _UI_ORIGIN, **(headers or {})}
        if data is not None:
            hdrs["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=hdrs)
        resp = conn.getresponse()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
    finally:
        conn.close()


@contextmanager
def _live_backend(cwd, target):
    """A real uvicorn process, the way the UI suite runs the API."""
    if importlib.util.find_spec("uvicorn") is None:
        pytest.skip("uvicorn not installed")
    port = _free_port()
    env = dict(os.environ)
    env["CROWNAI_DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="crownai-live-"), "crownai.db")
    env.pop("FRONTEND_URL", None)
    log = tempfile.TemporaryFile()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", target, "--host", "127.0.0.1", "--port", str(port)],
        cwd=cwd,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )

    def output():
        log.seek(0)
        return log.read().decode(errors="ignore")[-2000:]

    try:
        deadline = time.monotonic() + 30
        while True:
            if proc.poll() is not None:
                pytest.fail(f"`uvicorn {target}` from {cwd} exited with {proc.returncode}:\n{output()}")
            try:
                if _live_request(port, "GET", "/health", timeout=1)[0] == 200:
                    break
            except OSError:
                pass
            if time.monotonic() > deadline:
                pytest.fail(f"`uvicorn {target}` from {cwd} not answering /health after 30s:\n{output()}")
            time.sleep(0.2)
        yield port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        log.close()


@pytest.fixture(scope="module")
def live_backend():
    with _live_backend(BACKEND_DIR, "main:app") as port:
        yield port


# ---------- 15/16: the API answers on a real socket ----------


def test_us_2_live_backend_health_answers_within_browser_timeout(live_backend):
    start = time.monotonic()
    status, headers, body = _live_request(live_backend, "GET", "/health")
    elapsed = time.monotonic() - start
    assert status == 200
    assert _json.loads(body) == {"status": "ok"}
    assert elapsed < _BROWSER_TIMEOUT_S, f"/health took {elapsed:.2f}s"
    assert headers.get("access-control-allow-origin") == _UI_ORIGIN


@pytest.mark.parametrize("origin", ["http://127.0.0.1:3020", "http://localhost:3000", "http://localhost:9603"])
def test_us_2_live_backend_reachable_from_any_local_ui_origin(live_backend, origin):
    status, headers, _ = _live_request(live_backend, "GET", "/health", headers={"Origin": origin})
    assert status == 200
    assert headers.get("access-control-allow-origin") == origin


def test_us_2_live_backend_rejects_foreign_origin_but_still_answers(live_backend):
    status, headers, _ = _live_request(live_backend, "GET", "/health", headers={"Origin": "https://evil.example"})
    assert status == 200
    assert headers.get("access-control-allow-origin") != "https://evil.example"


def test_us_2_live_backend_unknown_route_is_404_not_hang(live_backend):
    status, _, _ = _live_request(live_backend, "GET", "/definitely-not-a-route")
    assert status == 404


def test_us_2_live_backend_starts_from_repo_root_with_all_routes():
    # `uvicorn backend.main:app` from the repo root must serve the same routes,
    # not a half-imported app that only answers /health.
    with _live_backend(REPO_DIR, "backend.main:app") as port:
        status, _, body = _live_request(port, "GET", "/openapi.json")
        assert status == 200
        paths = _json.loads(body)["paths"]
        for path in ("/health", "/auth/{provider}/login", "/projects", "/pricing/plans", "/pricing/leads", "/contact"):
            assert path in paths, f"{path} missing when started from repo root"
        assert _live_request(port, "GET", "/pricing/plans")[0] == 200


# ---------- 15: sign-in hands off to the provider ----------


@pytest.mark.parametrize(
    "provider,host", [("google", "accounts.google.com"), ("microsoft", "login.microsoftonline.com")]
)
def test_us_1_live_oauth_login_redirects_to_provider(live_backend, provider, host):
    start = time.monotonic()
    status, headers, _ = _live_request(live_backend, "GET", f"/auth/{provider}/login")
    assert time.monotonic() - start < _BROWSER_TIMEOUT_S
    assert status == 302
    location = headers["location"]
    parsed = urllib.parse.urlparse(location)
    query = urllib.parse.parse_qs(parsed.query)
    assert query.get("state", [""])[0], f"no CSRF state in {location}"
    if parsed.path == f"/auth/{provider}/mock":
        # No OAuth app configured on this runner: the dev stand-in must load.
        screen_status, screen_headers, screen = _live_request(live_backend, "GET", location)
        assert screen_status == 200
        assert "text/html" in screen_headers["content-type"]
        assert b'action="/auth/' + provider.encode() + b'/mock"' in screen
    else:
        assert parsed.scheme == "https" and parsed.hostname == host, location
        assert query["response_type"] == ["code"]
        assert query.get("client_id", [""])[0]


def test_us_1_live_oauth_unknown_provider_is_422(live_backend):
    assert _live_request(live_backend, "GET", "/auth/github/login")[0] == 422


@pytest.mark.parametrize(
    "provider,host,path_suffix",
    [
        ("google", "accounts.google.com", "/o/oauth2/v2/auth"),
        ("microsoft", "login.microsoftonline.com", "/oauth2/v2.0/authorize"),
    ],
)
def test_us_1_oauth_login_redirects_to_real_provider_when_configured(client, force_real_oauth, provider, host, path_suffix):
    states = []
    for _ in range(2):
        resp = client.get(f"/auth/{provider}/login", follow_redirects=False)
        assert resp.status_code == 302
        parsed = urllib.parse.urlparse(resp.headers["location"])
        assert parsed.scheme == "https" and parsed.hostname == host
        assert parsed.path.endswith(path_suffix)
        query = urllib.parse.parse_qs(parsed.query)
        assert query["client_id"] == [f"{'g' if provider == 'google' else 'ms'}-client-id"]
        assert query["response_type"] == ["code"]
        scopes = query["scope"][0].split()
        assert "openid" in scopes and "email" in scopes
        assert query["redirect_uri"][0].endswith(f"/auth/{provider}/callback")
        assert "secret" not in resp.headers["location"], "client secret leaked into the redirect"
        states.append(query["state"][0])
    assert states[0] != states[1], "CSRF state reused across sign-in attempts"


def test_us_1_oauth_mock_screen_unreachable_once_real_oauth_configured(client, force_real_oauth):
    resp = client.get("/auth/google/mock", params={"state": "anything"})
    assert resp.status_code == 404


@pytest.mark.parametrize(
    "params,expected",
    [({"state": "s"}, 422), ({"code": "c"}, 422), ({"code": "c", "state": "never-issued"}, 400)],
)
def test_us_1_oauth_callback_validation(client, params, expected):
    resp = client.get("/auth/google/callback", params=params, follow_redirects=False)
    assert resp.status_code == expected


# ---------- 19: pricing plans load from the backend ----------


def test_us_7_live_pricing_plans_load_from_backend(live_backend):
    status, headers, body = _live_request(live_backend, "GET", "/pricing/plans")
    assert status == 200
    assert headers.get("access-control-allow-origin") == _UI_ORIGIN
    plans = _json.loads(body)
    assert {p["id"] for p in plans} == set(PLANS)
    for plan in plans:
        assert plan["amount_cents"] > 0 and plan["currency"] == "usd"
        assert plan["name"] and plan["features"]


# ---------- 18: lead capture reaches the backend ----------


def _preflight_ok(port, path):
    status, headers, _ = _live_request(
        port,
        "OPTIONS",
        path,
        headers={"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"},
    )
    assert status == 200, f"preflight for {path} -> {status}"
    assert headers.get("access-control-allow-origin") == _UI_ORIGIN
    allowed = headers.get("access-control-allow-methods", "")
    assert "POST" in allowed or "*" in allowed


def test_us_6_live_lead_capture_reaches_backend(live_backend):
    _preflight_ok(live_backend, "/pricing/leads")
    lead = {
        "name": "  Priya Raman ",
        "email": _unique_email(),
        "company": "Acme Pvt Ltd",
        "phone": "+91 98765 43210",
        "plan": "mid",
    }
    status, headers, body = _live_request(live_backend, "POST", "/pricing/leads", body=lead)
    assert status == 201, body
    assert headers.get("access-control-allow-origin") == _UI_ORIGIN
    data = _json.loads(body)
    assert data["id"] and data["name"] == "Priya Raman" and data["plan"] == "mid"


@pytest.mark.parametrize(
    "missing_or_bad",
    [{"phone": None}, {"email": None}, {"company": None}, {"plan": "free"}, {"email": "not-an-email"}, {"phone": "abc"}],
)
def test_us_6_live_lead_capture_invalid_is_readable_422(live_backend, missing_or_bad):
    lead = {"name": "P", "email": _unique_email(), "company": "C", "phone": "+91 98765 43210", "plan": "mid"}
    for key, value in missing_or_bad.items():
        if value is None:
            lead.pop(key)
        else:
            lead[key] = value
    status, headers, body = _live_request(live_backend, "POST", "/pricing/leads", body=lead)
    assert status == 422, body
    # The browser can only show the error if CORS headers ride on the 422.
    assert headers.get("access-control-allow-origin") == _UI_ORIGIN


# ---------- 20: contact submissions are captured ----------


def test_us_9_live_contact_submission_is_captured(live_backend):
    _preflight_ok(live_backend, "/contact")
    email = _unique_email()
    status, headers, body = _live_request(
        live_backend, "POST", "/contact", body={"name": " Arun ", "email": email, "message": " Need a quote. "}
    )
    assert status == 201, body
    assert headers.get("access-control-allow-origin") == _UI_ORIGIN
    data = _json.loads(body)
    assert data["id"] and data["created_at"]
    assert data["name"] == "Arun" and data["email"] == email and data["message"] == "Need a quote."


@pytest.mark.parametrize(
    "body",
    [
        {"email": "a@example.com", "message": "hi"},
        {"name": "A", "message": "hi"},
        {"name": "A", "email": "a@example.com"},
        {"name": "A", "email": "a@example.com", "message": "   "},
        {"name": "A", "email": "nope", "message": "hi"},
    ],
)
def test_us_9_live_contact_invalid_is_readable_422(live_backend, body):
    status, headers, resp = _live_request(live_backend, "POST", "/contact", body=body)
    assert status == 422, resp
    assert headers.get("access-control-allow-origin") == _UI_ORIGIN


# ---------- 15-20: the frontend points at where uvicorn listens ----------


def test_us_2_frontend_default_api_base_matches_backend_address():
    # The UI failures say: "Start the API at the address the frontend was
    # built with (NEXT_PUBLIC_API_BASE, default http://127.0.0.1:8000)".
    hits = []
    for root, f in _frontend_walk():
        rel_root = os.path.relpath(root, FRONTEND_DIR).split(os.sep)
        if "tests" in rel_root or not f.endswith((".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")):
            continue
        src = _read(os.path.join(root, f))
        for m in _re.finditer(r"NEXT_PUBLIC_API_BASE\w*\s*(?:\?\?|\|\|)\s*[\"'`]([^\"'`]+)[\"'`]", src):
            hits.append((os.path.relpath(os.path.join(root, f), FRONTEND_DIR), m.group(1)))
    if not hits:
        pytest.skip("no NEXT_PUBLIC_API_BASE fallback in frontend sources")
    for rel, url in hits:
        parsed = urllib.parse.urlparse(url)
        assert parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost"), f"{rel}: {url}"
        assert parsed.port == 8000, f"{rel}: default API base {url} is not where `uvicorn main:app` listens (:8000)"
        assert parsed.path in ("", "/"), f"{rel}: default API base {url} has a path prefix the backend does not serve"


# ---------- 17: one project, all five artifacts ----------


def test_us_4_create_project_generates_all_five_artifacts(client):
    headers, _ = _session_for()
    requirements = "Track stock levels for a warehouse in Chennai."
    project = _create_project(client, headers, name="Stock Tracker", requirements=requirements)
    outs = _generate_all(client, headers, project["id"])

    assert len(ALL_STAGES) == 5
    assert [o["stage"] for o in outs] == ALL_STAGES
    assert [o["stage_label"] for o in outs] == [STAGE_LABELS[s] for s in STAGE_ORDER]
    assert all(o["content"].strip() and o["project_id"] == project["id"] for o in outs)
    assert requirements in outs[0]["content"]

    detail = client.get(f"/projects/{project['id']}", headers=headers).json()
    assert [a["stage"] for a in detail["artifacts"]] == ALL_STAGES
    assert detail["status"] == ALL_STAGES[-1]
    listed = client.get(f"/projects/{project['id']}/artifacts", headers=headers).json()
    assert [a["id"] for a in listed] == [o["id"] for o in outs]

    workspace = client.get("/projects", headers=headers).json()
    mine = [p for p in workspace if p["id"] == project["id"]]
    assert mine and mine[0]["status"] == ALL_STAGES[-1]


def test_us_4_five_artifacts_fit_exactly_in_free_daily_quota(client):
    headers, _ = _session_for()
    first = _create_project(client, headers)
    _generate_all(client, headers, first["id"])  # all five, free tier
    second = _create_project(client, headers, name="Second")
    resp = client.post(f"/projects/{second['id']}/generate/{ALL_STAGES[0]}", headers=headers)
    assert resp.status_code == 429
    assert "Pricing" in resp.json()["detail"]
    assert client.get(f"/projects/{second['id']}/artifacts", headers=headers).json() == []


def test_us_4_five_artifacts_each_stage_shows_progress_within_budget(client):
    headers, _ = _session_for()
    project = _create_project(client, headers)
    for stage in ALL_STAGES:
        start = time.monotonic()
        resp = client.post(f"/projects/{project['id']}/generate/{stage}", headers=headers)
        assert resp.status_code == 200
        assert time.monotonic() - start < GENERATION_PROGRESS_BUDGET_S, stage


def test_us_4_five_artifacts_readable_cross_origin_from_ui_port(client):
    headers, _ = _session_for()
    project = _create_project(client, headers)
    for stage in ALL_STAGES:
        resp = client.post(
            f"/projects/{project['id']}/generate/{stage}", headers={**headers, "Origin": _UI_ORIGIN}
        )
        assert resp.status_code == 200
        assert resp.headers.get("access-control-allow-origin") == _UI_ORIGIN


@pytest.mark.parametrize("skipped_to", ALL_STAGES[1:])
def test_us_4_five_artifacts_out_of_order_is_409_and_creates_nothing(client, skipped_to):
    headers, _ = _session_for()
    project = _create_project(client, headers)
    resp = client.post(f"/projects/{project['id']}/generate/{skipped_to}", headers=headers)
    assert resp.status_code == 409
    assert client.get(f"/projects/{project['id']}/artifacts", headers=headers).json() == []


def test_us_4_five_artifacts_unknown_stage_is_422(client):
    headers, _ = _session_for()
    project = _create_project(client, headers)
    assert client.post(f"/projects/{project['id']}/generate/deploy", headers=headers).status_code == 422


def test_us_4_five_artifacts_unknown_project_is_404(client):
    headers, _ = _session_for()
    resp = client.post(f"/projects/{uuid.uuid4().hex}/generate/{ALL_STAGES[0]}", headers=headers)
    assert resp.status_code == 404


def test_us_4_five_artifacts_require_sign_in(client):
    headers, _ = _session_for()
    project = _create_project(client, headers)
    assert client.post(f"/projects/{project['id']}/generate/{ALL_STAGES[0]}").status_code == 401


# ---------- 21/23: one brand gold gradient on site and tool ----------


def _hex(color):
    color = color.strip().lower()
    m = _re.fullmatch(r"rgba?\(\s*(\d+)[\s,]+(\d+)[\s,]+(\d+)(?:[\s,/]+[\d.]+%?)?\s*\)", color)
    if m:
        return "#" + "".join(f"{int(c):02x}" for c in m.groups())
    if _re.fullmatch(r"#[0-9a-f]{3}", color):
        return "#" + "".join(c * 2 for c in color[1:])
    return color


def _gradient_stops(value):
    colors = _re.findall(r"#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b|rgba?\([^)]*\)", value)
    return [_hex(c) for c in colors]


def _resolve_vars(value, props, depth=0):
    if depth > 10:
        return value
    out = _re.sub(
        r"var\(\s*(--[\w-]+)\s*(?:,\s*([^)]*))?\)",
        lambda m: props.get(m.group(1), m.group(2) or m.group(0)),
        value,
    )
    return out if out == value else _resolve_vars(out, props, depth + 1)


def _btn_primary_background(css):
    """Final background of the base `.btn-primary` rule, vars resolved, or
    the colours named by an `@apply` gradient."""
    rules = _css_rules(css)
    # Regex over the whole sheet so `@theme { --x: ... }` (which the rule
    # walker skips) resolves too; first definition = light theme.
    props = {}
    for name, value in _re.findall(r"(--[\w-]+)\s*:\s*([^;{}]+)", _re.sub(r"/\*.*?\*/", "", css, flags=_re.S)):
        props.setdefault(name, value.strip())
    final = None
    for sels, decls in rules:
        if ".btn-primary" not in [s.strip() for s in sels]:
            continue
        for prop, value in _declarations(decls):
            if prop in ("background", "background-image"):
                final = _resolve_vars(value, props)
            elif prop == "@apply" and _re.search(r"\bbg-(?:linear|gradient)-", value):
                stops = []
                for kind in ("from", "via", "to"):
                    m = _re.search(rf"\b{kind}-(\[[^\]]+\]|[a-z]+-\d+)", value)
                    if m:
                        token = m.group(1)
                        stops.append(_hex(token[1:-1]) if token.startswith("[") else _TAILWIND_GOLD.get(token, token))
                final = "linear-gradient(" + ", ".join(stops) + ")"
    return final


def test_us_3_brand_gradient_helpers_self_test():
    assert _gradient_stops("linear-gradient(135deg, rgb(252, 211, 77) 0%, rgb(234, 179, 8) 50%, rgb(217, 119, 6) 100%)") == _BRAND_GOLD_STOPS
    assert _gradient_stops("linear-gradient(135deg,#FCD34D,#EAB308 50%,#D97706)") == _BRAND_GOLD_STOPS
    css = ":root{--gold:linear-gradient(135deg,#fcd34d,#eab308 50%,#d97706)} .btn-primary{background-image:var(--gold)}"
    assert _gradient_stops(_btn_primary_background(css)) == _BRAND_GOLD_STOPS
    themed = "@theme{--color-g1:#fcd34d;--color-g2:#eab308;--color-g3:#d97706}" \
        ".btn-primary{background:linear-gradient(135deg,var(--color-g1),var(--color-g2) 50%,var(--color-g3))}"
    assert _gradient_stops(_btn_primary_background(themed)) == _BRAND_GOLD_STOPS
    applied = ".btn-primary{@apply bg-linear-135 from-amber-300 via-yellow-500 to-amber-600;}"
    assert _gradient_stops(_btn_primary_background(applied)) == _BRAND_GOLD_STOPS
    # A later `background: none` wins -- exactly the "styles missing: none" failure.
    overridden = ".btn-primary{background-image:linear-gradient(#fcd34d,#eab308,#d97706)} .btn-primary{background:none}"
    assert "gradient" not in _btn_primary_background(overridden)


@pytest.mark.parametrize("provider", ["google", "microsoft"])
def test_us_3_crown_ai_sign_in_surface_uses_brand_gold_gradient(client, force_mock, provider):
    login = client.get(f"/auth/{provider}/login", follow_redirects=False)
    screen = client.get(login.headers["location"])
    assert screen.status_code == 200
    gradients = _re.findall(r"linear-gradient\([^;]*?\)\s*[;}]", screen.text)
    assert gradients, "sign-in surface has no brand gradient"
    for g in gradients:
        assert _gradient_stops(g) == _BRAND_GOLD_STOPS, g


def test_us_2_btn_primary_compiles_to_brand_gold_gradient():
    background = _btn_primary_background(_globals_css())
    assert background, "no base .btn-primary background in app/globals.css"
    assert "gradient" in background, f".btn-primary background resolves to {background!r}, not a gradient"
    assert _gradient_stops(background) == _BRAND_GOLD_STOPS, f".btn-primary gradient stops: {background!r}"


def test_us_3_site_and_tool_share_one_gold_gradient(client, force_mock):
    site = _gradient_stops(_btn_primary_background(_globals_css()) or "")
    login = client.get("/auth/google/login", follow_redirects=False)
    tool = _gradient_stops(_re.search(r"linear-gradient\([^;]*?\)\s*[;}]", client.get(login.headers["location"]).text).group(0))
    assert site == tool, f"site {site} vs Crown AI tool {tool}"


def test_us_2_root_layout_loads_globals_css():
    layout = None
    for name in ("layout.tsx", "layout.jsx", "layout.js"):
        if os.path.exists(os.path.join(FRONTEND_DIR, "app", name)):
            layout = _frontend_source("app", name)
            break
    if layout is None:
        pytest.skip("frontend/app/layout not present")
    assert _re.search(r"""import\s+["'](?:\./|@/app/)globals\.css["']""", layout), "root layout does not import globals.css"


# ===========================================================================
# Human-requested coverage (Testing defects pass: 17 reports applied together):
#   1. OAuth buttons must reach the real provider: the authorize URL is built
#      from env vars (client id, redirect URI, tenant), and an unconfigured
#      provider gives a clear "not configured" answer, never a dead redirect
#      with an empty client_id.
#   2. Browser -> backend: CORS for the UI origin on every call the
#      workspace makes (incl. credentialed preflight and error responses),
#      and no hardcoded backend host in the frontend outside the
#      NEXT_PUBLIC_API_BASE fallback.
#   3. "Generate Project" redirect: POST /projects answers fast with a
#      URL-safe id, /crown-ai/page.tsx warms /crown-ai/projects/[id] (router
#      prefetch + error-swallowing no-store fetch of /crown-ai/projects/warm-up),
#      that route has a loading.tsx, and the backend answers the "warm-up" id
#      with a clean 404 (never 5xx).
#   4. Exactly one bold <h1> in every /crown-ai state: in the Suspense
#      fallback and in WorkspaceBar (no <p> title).
#   5-16. The shim is covered by the test_us_2_tailwind_* sections above;
#      the content scans now exempt the UI suite's own Python detectors
#      (frontend/tests/test_ui_generated.py names the shim to look for it),
#      which is unit-tested here so the exemption cannot hide a real shim.
# ===========================================================================

import importlib  # noqa: E402

_OAUTH_ENV_KEYS = (
    "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REDIRECT_URI",
    "MICROSOFT_CLIENT_ID", "MICROSOFT_CLIENT_SECRET", "MICROSOFT_TENANT_ID", "MICROSOFT_REDIRECT_URI",
    "OAUTH_DEV_MOCK",
)


@pytest.fixture
def oauth_env():
    """Sets OAuth env vars and reloads oauth_providers so it reads them the
    way a freshly started backend does; restores both afterwards."""
    saved = {k: os.environ.get(k) for k in _OAUTH_ENV_KEYS}

    def apply(**values):
        for k in _OAUTH_ENV_KEYS:
            os.environ.pop(k, None)
        for k, v in values.items():
            os.environ[k] = v
        importlib.reload(oauth_providers)

    yield apply
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    importlib.reload(oauth_providers)


def _login_location(client, provider):
    resp = client.get(f"/auth/{provider}/login", follow_redirects=False)
    assert resp.status_code == 302, resp.text
    parsed = urllib.parse.urlparse(resp.headers["location"])
    return parsed, urllib.parse.parse_qs(parsed.query)


# ---------- 1. OAuth authorize URL comes from the environment ----------


def test_us_1_google_authorize_url_built_from_env_vars(client, oauth_env):
    oauth_env(
        GOOGLE_CLIENT_ID="env-google-id.apps.googleusercontent.com",
        GOOGLE_CLIENT_SECRET="env-google-secret",
        GOOGLE_REDIRECT_URI="https://api.crownwright.example/auth/google/callback",
    )
    parsed, query = _login_location(client, "google")
    assert (parsed.scheme, parsed.hostname, parsed.path) == ("https", "accounts.google.com", "/o/oauth2/v2/auth")
    assert query["client_id"] == ["env-google-id.apps.googleusercontent.com"]
    assert query["redirect_uri"] == ["https://api.crownwright.example/auth/google/callback"]
    assert query["response_type"] == ["code"] and query["state"][0]
    assert "env-google-secret" not in parsed.geturl()


def test_us_1_microsoft_authorize_url_built_from_env_vars(client, oauth_env):
    oauth_env(
        MICROSOFT_CLIENT_ID="env-ms-id",
        MICROSOFT_CLIENT_SECRET="env-ms-secret",
        MICROSOFT_TENANT_ID="contoso-tenant",
        MICROSOFT_REDIRECT_URI="https://api.crownwright.example/auth/microsoft/callback",
    )
    parsed, query = _login_location(client, "microsoft")
    assert parsed.hostname == "login.microsoftonline.com"
    assert parsed.path == "/contoso-tenant/oauth2/v2.0/authorize"
    assert query["client_id"] == ["env-ms-id"]
    assert query["redirect_uri"] == ["https://api.crownwright.example/auth/microsoft/callback"]
    assert "env-ms-secret" not in parsed.geturl()


def test_us_1_microsoft_tenant_defaults_to_common(client, oauth_env):
    oauth_env(MICROSOFT_CLIENT_ID="id", MICROSOFT_CLIENT_SECRET="secret")
    parsed, _ = _login_location(client, "microsoft")
    assert parsed.path == "/common/oauth2/v2.0/authorize"


@pytest.mark.parametrize("provider", ["google", "microsoft"])
def test_us_1_default_redirect_uri_points_at_this_backends_callback(client, oauth_env, provider):
    oauth_env(**{f"{provider.upper()}_CLIENT_ID": "id", f"{provider.upper()}_CLIENT_SECRET": "secret"})
    _, query = _login_location(client, provider)
    redirect = urllib.parse.urlparse(query["redirect_uri"][0])
    assert redirect.path == f"/auth/{provider}/callback"
    assert redirect.hostname in ("localhost", "127.0.0.1") and redirect.port == 8000


@pytest.mark.parametrize("provider", ["google", "microsoft"])
def test_us_1_unconfigured_provider_with_mock_off_says_not_configured(client, oauth_env, provider):
    # The defect: a button that "works" but lands on the provider with an
    # empty client_id. With no credentials and the dev mock off, the user
    # must get a clear "not configured" answer instead.
    oauth_env(OAUTH_DEV_MOCK="false")
    resp = client.get(f"/auth/{provider}/login", follow_redirects=False)
    if resp.status_code == 302:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(resp.headers["location"]).query)
        pytest.fail(f"redirected to the provider without credentials (client_id={query.get('client_id')})")
    assert resp.status_code in (501, 503), resp.text
    assert "not configured" in resp.text.lower(), resp.text


@pytest.mark.parametrize(
    "present", [{"GOOGLE_CLIENT_ID": "id-only"}, {"GOOGLE_CLIENT_SECRET": "secret-only"}]
)
def test_us_1_half_configured_provider_is_not_configured(client, oauth_env, present):
    oauth_env(OAUTH_DEV_MOCK="false", **present)
    assert oauth_providers.is_configured("google") is False
    resp = client.get("/auth/google/login", follow_redirects=False)
    assert resp.status_code != 302 or "client_id=&" not in resp.headers["location"]
    assert resp.status_code in (501, 503) and "not configured" in resp.text.lower(), resp.text


def test_us_1_unconfigured_provider_with_mock_on_uses_labelled_dev_screen(client, oauth_env):
    oauth_env()  # no creds, mock defaults on
    parsed, query = _login_location(client, "google")
    assert parsed.path == "/auth/google/mock" and query["state"][0]
    screen = client.get(parsed.path, params={"state": query["state"][0]})
    assert screen.status_code == 200
    assert "no Google OAuth app is configured" in screen.text


def test_us_1_mock_off_blocks_mock_endpoints_even_without_creds(client, oauth_env):
    oauth_env(OAUTH_DEV_MOCK="false")
    assert client.get("/auth/google/mock", params={"state": "x"}).status_code == 404
    resp = client.post("/auth/google/mock", data={"state": "x", "name": "n", "email": "a@example.com"})
    assert resp.status_code == 404


def test_us_1_one_provider_configured_does_not_affect_the_other(client, oauth_env):
    oauth_env(GOOGLE_CLIENT_ID="gid", GOOGLE_CLIENT_SECRET="gs")
    g, _ = _login_location(client, "google")
    m, _ = _login_location(client, "microsoft")
    assert g.hostname == "accounts.google.com"
    assert m.path == "/auth/microsoft/mock"


def test_us_1_login_unknown_provider_is_422_and_missing_provider_404(client):
    assert client.get("/auth/github/login", follow_redirects=False).status_code == 422
    assert client.get("/auth//login", follow_redirects=False).status_code == 404


_PLACEHOLDER_OAUTH_RE = _re.compile(
    r"YOUR[_-]?CLIENT[_-]?ID|CLIENT[_-]?ID[_-]?HERE|<client[_-]?id>|client_id=(?:xxx|placeholder|changeme)\b"
    r"|href=[\"']#[\"'][^>]*>\s*(?:Sign in|Continue) with (?:Google|Microsoft)",
    _re.I,
)


def _frontend_app_sources():
    for root, f in _frontend_walk():
        rel = os.path.relpath(os.path.join(root, f), FRONTEND_DIR)
        if "tests" in rel.replace("\\", "/").split("/")[:-1] or not f.endswith(_SOURCE_EXTS):
            continue
        yield rel, _read(os.path.join(root, f))


def test_us_1_frontend_sign_in_buttons_have_no_placeholder_urls():
    offenders = [rel for rel, src in _frontend_app_sources() if _PLACEHOLDER_OAUTH_RE.search(src)]
    assert not offenders, f"placeholder OAuth URLs in {offenders}"


def test_us_1_frontend_sign_in_targets_backend_login_route():
    hits = [
        rel for rel, src in _frontend_app_sources()
        if _re.search(r"/auth/(?:\$\{[^}]+\}|google|microsoft)/login", src)
    ]
    if not hits:
        pytest.skip("no sign-in link found in frontend sources")
    for rel, src in _frontend_app_sources():
        if rel not in hits:
            continue
        # Never a hardcoded host in front of the login route.
        assert not _re.search(r"https?://[^\"'`\s]+/auth/(?:\$\{[^}]+\}|google|microsoft)/login", src), rel


def test_us_1_frontend_client_side_oauth_env_has_not_configured_message():
    # If the frontend builds the authorize URL itself from NEXT_PUBLIC_*
    # client ids, it must show a "not configured" message when they're unset.
    for rel, src in _frontend_app_sources():
        if _re.search(r"NEXT_PUBLIC_(?:GOOGLE|MICROSOFT|MS|AZURE)\w*CLIENT_ID", src):
            assert _re.search(r"not configured", src, _re.I), f"{rel} has no 'not configured' fallback"


# ---------- 2. browser reaches the backend ----------


@pytest.mark.parametrize("path,method", [("/projects", "POST"), ("/projects", "GET"), ("/auth/me", "GET")])
def test_us_2_credentialed_preflight_allowed_from_ui_origin(client, path, method):
    resp = client.options(
        path,
        headers={
            "Origin": _UI_ORIGIN,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == _UI_ORIGIN
    assert resp.headers.get("access-control-allow-credentials") == "true"
    allowed = resp.headers.get("access-control-allow-headers", "").lower()
    assert "authorization" in allowed or "*" in allowed


@pytest.mark.parametrize("path", ["/auth/me", "/projects"])
def test_us_2_unauthenticated_401_still_carries_cors(client, path):
    # Without CORS on the 401 the browser reports "unreachable", not "sign in".
    resp = client.get(path, headers={"Origin": _UI_ORIGIN})
    assert resp.status_code == 401
    assert resp.headers.get("access-control-allow-origin") == _UI_ORIGIN


@pytest.mark.parametrize("origin", ["http://localhost.evil.example", "http://evil.example:3020", "null"])
def test_us_2_cors_regex_does_not_admit_lookalike_origins(client, origin):
    resp = client.get("/health", headers={"Origin": origin})
    assert resp.headers.get("access-control-allow-origin") != origin


_HARDCODED_API_RE = _re.compile(r"https?://(?:localhost|127\.0\.0\.1):8000")


def test_us_2_frontend_backend_host_only_in_api_base_fallback():
    offenders = []
    for rel, src in _frontend_app_sources():
        for line in src.splitlines():
            if _HARDCODED_API_RE.search(line) and "NEXT_PUBLIC_API_BASE" not in line:
                offenders.append(f"{rel}: {line.strip()[:120]}")
    assert not offenders, f"backend host hardcoded outside NEXT_PUBLIC_API_BASE: {offenders}"


def test_us_2_frontend_reads_next_public_api_base():
    if not any("NEXT_PUBLIC_API_BASE" in src for _, src in _frontend_app_sources()):
        pytest.fail("frontend never reads NEXT_PUBLIC_API_BASE")


# ---------- 3. "Generate Project" lands on the project page fast ----------


def _crown_ai_page():
    return _frontend_source("app", "crown-ai", "page.tsx")


def test_us_4_create_project_returns_url_safe_id_quickly(client):
    headers, _ = _session_for()
    start = time.monotonic()
    resp = client.post("/projects", json={"name": "Fast", "requirements": "r"}, headers={**headers, "Origin": _UI_ORIGIN})
    assert time.monotonic() - start < 1.0
    assert resp.status_code == 201
    assert resp.headers.get("access-control-allow-origin") == _UI_ORIGIN
    pid = resp.json()["id"]
    assert pid and urllib.parse.quote(pid, safe="") == pid, f"id {pid!r} is not URL-safe for router.push"
    assert pid != "warm-up"


def test_us_4_warm_up_project_id_is_clean_404_not_5xx(client):
    # The prefetch renders /crown-ai/projects/warm-up; if that page asks the
    # API about "warm-up", the answer must be an ordinary 404.
    headers, _ = _session_for()
    assert client.get("/projects/warm-up", headers=headers).status_code == 404
    assert client.get("/projects/warm-up/artifacts", headers=headers).status_code == 404
    assert client.get("/projects/warm-up").status_code == 401


def test_us_4_new_project_reachable_immediately_after_create(client):
    headers, _ = _session_for()
    pid = _create_project(client, headers)["id"]
    detail = client.get(f"/projects/{pid}", headers=headers)
    assert detail.status_code == 200 and detail.json()["id"] == pid
    assert detail.json()["artifacts"] == []


@pytest.mark.parametrize("body", [{}, {"name": "n"}, {"requirements": "r"}, {"name": " ", "requirements": "r"}])
def test_us_4_create_project_invalid_is_422_no_redirect_target(client, body):
    headers, _ = _session_for()
    resp = client.post("/projects", json=body, headers=headers)
    assert resp.status_code == 422
    assert client.get("/projects", headers=headers).json() == []


def test_us_4_workspace_prefetches_project_route():
    src = _strip_js_comments(_crown_ai_page())
    assert _re.search(r"router\.prefetch\(\s*[\"'`]/crown-ai/projects/warm-up[\"'`]\s*\)", src), (
        "crown-ai/page.tsx does not call router.prefetch('/crown-ai/projects/warm-up')"
    )


def test_us_4_workspace_warm_up_fetch_is_no_store_and_swallows_errors():
    src = _strip_js_comments(_crown_ai_page())
    m = _re.search(
        r"fetch\(\s*[\"'`]/crown-ai/projects/warm-up[\"'`]\s*,\s*\{[^}]*cache\s*:\s*[\"']no-store[\"'][^}]*\}\s*\)\s*\.catch\(",
        src,
    )
    assert m, "warm-up fetch must be fetch('/crown-ai/projects/warm-up', { cache: 'no-store' }).catch(() => {})"


def test_us_4_warm_up_is_relative_not_backend_host():
    # It warms the Next.js page, so it must hit the frontend, not API_BASE.
    src = _strip_js_comments(_crown_ai_page())
    assert not _re.search(r"fetch\(\s*`?\$\{[^}]*\}/crown-ai/projects/warm-up", src)
    assert not _re.search(r"https?://[^\"'`]*/crown-ai/projects/warm-up", src)


def test_us_4_warm_up_runs_only_once_signed_in():
    # Warming before sign-in would hit a page that bounces to the sign-in
    # screen; the fix puts it in the signed-in `load` effect or on "+ New Project".
    src = _strip_js_comments(_crown_ai_page())
    idx = src.find("/crown-ai/projects/warm-up")
    assert idx != -1, "no warm-up of /crown-ai/projects/[id]"
    window = src[max(0, idx - 1500):idx]
    assert _re.search(r"\btoken\b|New Project|\bload\b", window), "warm-up is not tied to the signed-in workspace"


def test_us_4_redirect_still_targets_project_page_with_autogenerate():
    src = _strip_js_comments(_crown_ai_page())
    assert _re.search(r"router\.push\(\s*`/crown-ai/projects/\$\{[^}]+\}\?autogenerate=1`", src), (
        "Generate Project must still push to /crown-ai/projects/<id>?autogenerate=1"
    )


def test_us_4_project_route_has_loading_placeholder():
    base = os.path.join(FRONTEND_DIR, "app", "crown-ai", "projects", "[id]")
    if not os.path.isdir(base):
        pytest.skip("frontend/app/crown-ai/projects/[id] not present")
    assert any(os.path.exists(os.path.join(base, n)) for n in ("loading.tsx", "loading.jsx", "loading.js"))
    assert any(os.path.exists(os.path.join(base, n)) for n in ("page.tsx", "page.jsx", "page.js"))


# ---------- 4. one bold <h1> in every /crown-ai state ----------


def _suspense_fallback(src):
    m = _re.search(r"fallback=\{", src)
    assert m, "crown-ai/page.tsx has no Suspense fallback"
    depth, i = 1, m.end()
    while i < len(src) and depth:
        depth += {"{": 1, "}": -1}.get(src[i], 0)
        i += 1
    return src[m.end():i - 1]


def test_us_2_suspense_fallback_helper_self_test():
    sample = '<Suspense fallback={<div className="container-page"><h1 className="x">{A} B</h1><LoadingState /></div>}>'
    body = _suspense_fallback(sample)
    assert "<h1" in body and "LoadingState" in body and "Suspense" not in body


def test_us_2_crown_ai_suspense_fallback_has_heading_before_loading():
    body = _suspense_fallback(_strip_js_comments(_crown_ai_page()))
    h1 = body.find("<h1")
    assert h1 != -1, "Suspense fallback renders no <h1>"
    assert body.count("<h1") == 1, "Suspense fallback renders more than one <h1>"
    loading = body.find("LoadingState")
    assert loading == -1 or h1 < loading, "<h1> must come before the LoadingState"
    tag = _re.search(r"<h1[^>]*>", body[h1:]).group(0)
    assert "page-title" in tag, f"fallback heading lacks page-title styling: {tag}"
    assert "PRODUCT_NAME" in body[h1:] and "Workspace" in body[h1:]


def test_us_2_crown_ai_fallback_heading_matches_not_ready_branch():
    src = _strip_js_comments(_crown_ai_page())
    tags = set(_re.findall(r"<h1[^>]*className=\"([^\"]*)\"", src))
    assert "page-title mb-6" in tags, f"no page-title h1 in crown-ai/page.tsx: {tags}"


def test_us_2_workspace_bar_title_is_bold_h1():
    src = _strip_js_comments(_frontend_source("components", "WorkspaceBar.tsx"))
    assert len(_re.findall(r"<h1\b", src)) == 1, "WorkspaceBar must render exactly one <h1>"
    tag = _re.search(r"<h1[^>]*>", src).group(0)
    assert "font-bold" in tag, f"WorkspaceBar <h1> is not bold: {tag}"
    assert not _re.search(r"<p[^>]*text-lg font-bold", src), "WorkspaceBar title is still a <p>"


def test_us_2_crown_ai_signed_in_view_has_no_second_h1():
    # The signed-in branch renders WorkspaceBar's <h1>; page.tsx must not add
    # another one alongside it.
    src = _strip_js_comments(_crown_ai_page())
    if "WorkspaceBar" not in src:
        pytest.skip("crown-ai/page.tsx does not render WorkspaceBar")
    bar = src.find("<WorkspaceBar")
    following = src[bar:bar + 3000]
    nxt = _re.search(r"\n\s*(?:if\s*\(|return\s*\(|function\s)", following[1:])
    block = following[: nxt.start() + 1] if nxt else following
    assert "<h1" not in block, "signed-in view has an <h1> besides WorkspaceBar's"


# ---------- 13/16: test-file exemption is narrow ----------


@pytest.mark.parametrize(
    "rel,exempt",
    [
        ("tests/test_ui_generated.py", True),
        ("tests\\test_ui_generated.py", True),
        ("tests/conftest.py", True),
        ("tests/tailwind-postcss.js", False),
        ("tests/helpers.ts", False),
        ("tailwind-postcss.js", False),
        ("scripts/build.py", False),
        ("app/test_page.tsx", False),
    ],
)
def test_us_2_tailwind_scan_test_file_exemption_is_narrow(rel, exempt):
    assert _is_frontend_test_file(rel) is exempt


def test_us_2_tailwind_shim_moved_into_frontend_tests_still_caught_by_name():
    # The exemption is content-only: the filename scans walk tests/ too.
    assert _SHIM_FILENAME_RE.fullmatch("tailwind-postcss.js")
    walked = {os.path.relpath(r, FRONTEND_DIR).replace("\\", "/") for r, _ in _frontend_walk()}
    if os.path.isdir(os.path.join(FRONTEND_DIR, "tests")):
        assert "tests" in walked, "_frontend_walk skips frontend/tests"


# ---------- 17-20: "not configured" instead of an empty client_id, edge cases ----------


@pytest.mark.parametrize(
    "present", [{"MICROSOFT_CLIENT_ID": "id-only"}, {"MICROSOFT_CLIENT_SECRET": "secret-only"}]
)
def test_us_1_half_configured_microsoft_is_not_configured(client, oauth_env, present):
    oauth_env(OAUTH_DEV_MOCK="false", **present)
    assert oauth_providers.is_configured("microsoft") is False
    resp = client.get("/auth/microsoft/login", follow_redirects=False)
    assert resp.status_code in (501, 503), resp.text
    assert "not configured" in resp.text.lower()


@pytest.mark.parametrize("present", [{"GOOGLE_CLIENT_ID": ""}, {"GOOGLE_CLIENT_ID": "", "GOOGLE_CLIENT_SECRET": ""}])
def test_us_1_empty_string_credentials_count_as_unset(client, oauth_env, present):
    # `GOOGLE_CLIENT_ID=` in a .env file is the common half-configured case.
    oauth_env(OAUTH_DEV_MOCK="false", **present)
    resp = client.get("/auth/google/login", follow_redirects=False)
    assert resp.status_code in (501, 503), resp.text


@pytest.mark.parametrize("provider", ["google", "microsoft"])
def test_us_1_not_configured_message_names_the_missing_env_vars(client, oauth_env, provider):
    oauth_env(OAUTH_DEV_MOCK="false")
    resp = client.get(f"/auth/{provider}/login", follow_redirects=False)
    detail = resp.json()["detail"]
    prefix = provider.upper()
    assert f"{prefix}_CLIENT_ID" in detail and f"{prefix}_CLIENT_SECRET" in detail, detail
    assert "location" not in {k.lower() for k in resp.headers}


def test_us_1_not_configured_login_leaves_no_pending_state(client, oauth_env):
    oauth_env(OAUTH_DEV_MOCK="false")
    before = dict(auth_module._PENDING_STATES)
    for _ in range(3):
        client.get("/auth/google/login", follow_redirects=False)
    assert auth_module._PENDING_STATES == before, "a refused login must not register CSRF state"


@pytest.mark.parametrize("flag", ["false", "FALSE", " False ", "0", "no", "off"])
def test_us_1_every_mock_off_spelling_refuses_unconfigured_login(client, oauth_env, flag):
    oauth_env(OAUTH_DEV_MOCK=flag)
    resp = client.get("/auth/google/login", follow_redirects=False)
    assert resp.status_code in (501, 503), f"OAUTH_DEV_MOCK={flag!r} -> {resp.status_code}"


@pytest.mark.parametrize("flag", ["true", "1", "yes", ""])
def test_us_1_mock_on_spellings_still_use_dev_screen(client, oauth_env, flag):
    oauth_env(OAUTH_DEV_MOCK=flag)
    parsed, query = _login_location(client, "microsoft")
    assert parsed.path == "/auth/microsoft/mock" and query["state"][0]


@pytest.mark.parametrize("mock_flag", ["false", "true"])
@pytest.mark.parametrize(
    "provider,host", [("google", "accounts.google.com"), ("microsoft", "login.microsoftonline.com")]
)
def test_us_1_fully_configured_always_redirects_with_real_client_id(client, oauth_env, mock_flag, provider, host):
    # Real credentials win whether or not the dev mock is enabled.
    oauth_env(
        OAUTH_DEV_MOCK=mock_flag,
        **{f"{provider.upper()}_CLIENT_ID": f"{provider}-cid", f"{provider.upper()}_CLIENT_SECRET": "s3cret"},
    )
    parsed, query = _login_location(client, provider)
    assert parsed.hostname == host
    assert query["client_id"] == [f"{provider}-cid"]
    assert query["state"][0] in auth_module._PENDING_STATES
    assert "s3cret" not in parsed.geturl()


def test_us_1_not_configured_response_readable_by_browser(client, oauth_env):
    # The UI shows this message; without CORS it would look like "backend down".
    oauth_env(OAUTH_DEV_MOCK="false")
    resp = client.get("/auth/google/login", headers={"Origin": _UI_ORIGIN}, follow_redirects=False)
    assert resp.status_code in (501, 503)
    assert resp.headers.get("access-control-allow-origin") == _UI_ORIGIN


def test_us_1_callback_when_not_configured_is_clean_4xx(client, oauth_env):
    oauth_env(OAUTH_DEV_MOCK="false")
    assert client.get("/auth/google/callback", params={"code": "c", "state": "never-issued"}).status_code == 400
    assert client.get("/auth/google/callback", params={"state": "s"}).status_code == 422
    assert client.get("/auth/google/callback", params={"code": "c"}).status_code == 422


def test_us_1_unknown_provider_with_mock_off_is_422(client, oauth_env):
    oauth_env(OAUTH_DEV_MOCK="false")
    assert client.get("/auth/github/login", follow_redirects=False).status_code == 422


# ===========================================================================
# Human-requested coverage (17-defect pass, remaining gaps). Items 1, 2, 4
# and 5-16 are covered by the sections above; this adds what they left open:
#   3. The warm-up must never delay "Generate Project": it is fire-and-forget
#      (not awaited), it lives outside onCreate (signed-in load effect or
#      "+ New Project"), and onCreate's own pause before router.push stays
#      small. The optional production-build run (`npm run build && npm run
#      start -- -p 3020`) must be possible with the package.json scripts.
#   4. Every /crown-ai state has a bold <h1>: fallback, !ready and !token
#      each render a page-title <h1>, and .page-title is actually bold.
#   8. frontend/tests/test_ui_generated.py must not depend on the deleted
#      shim existing (it expects it gone).
#   11-16. The newer shim detectors stay in force (not skipped or weakened).
# ===========================================================================


def _on_create_block(src):
    """(start, end, body) of onCreate's function body, brace matched."""
    m = _re.search(r"(?:function\s+onCreate\s*\([^)]*\)|const\s+onCreate\s*=\s*(?:async\s*)?\([^)]*\)\s*(?::[^=]*)?=>)", src)
    if not m:
        pytest.skip("onCreate not found in crown-ai/page.tsx")
    open_ = src.find("{", m.end())
    if open_ == -1:
        pytest.skip("onCreate has no block body")
    depth, i = 1, open_ + 1
    while i < len(src) and depth:
        depth += {"{": 1, "}": -1}.get(src[i], 0)
        i += 1
    return m.start(), i, src[open_ + 1:i - 1]


# ---------- 3. warm-up never sits on the redirect path ----------


def test_us_4_on_create_block_helper_self_test():
    sample = "const onCreate = async (e) => { if (x) { return; } router.push(`/a`); }\nconst other = 1;"
    start, end, body = _on_create_block(sample)
    assert start == 0 and "router.push" in body and "other" not in body
    assert sample[end:].strip() == "const other = 1;"


def test_us_4_warm_up_is_fire_and_forget_not_awaited():
    src = _strip_js_comments(_crown_ai_page())
    if "/crown-ai/projects/warm-up" not in src:
        pytest.fail("crown-ai/page.tsx does not warm /crown-ai/projects/[id]")
    assert not _re.search(r"await\s+fetch\(\s*[\"'`]/crown-ai/projects/warm-up", src), (
        "awaiting the warm-up fetch blocks the workspace on a page build"
    )
    assert not _re.search(r"await\s+router\.prefetch\(", src)
    assert not _re.search(r"\.then\([^)]*router\.push", src[src.find("/crown-ai/projects/warm-up"):][:400]), (
        "the redirect must not be chained onto the warm-up"
    )


def test_us_4_warm_up_happens_before_generate_not_inside_on_create():
    # Warming only after POST /projects answers is too late: the build of
    # /crown-ai/projects/[id] is exactly what the redirect was waiting on.
    src = _strip_js_comments(_crown_ai_page())
    start, end, _ = _on_create_block(src)
    outside = src[:start] + src[end:]
    assert "/crown-ai/projects/warm-up" in outside, (
        "warm-up only runs inside onCreate; start it in the signed-in load effect or on '+ New Project'"
    )


def test_us_4_redirect_pause_after_create_stays_small():
    # The report: POST answered immediately, then a 400 ms pause, then push.
    # Any pause before router.push eats the test's 5 s budget; keep it <= 1 s.
    _, _, body = _on_create_block(_strip_js_comments(_crown_ai_page()))
    if "router.push" not in body:
        pytest.skip("onCreate does not redirect itself")
    delays = [int(d) for d in _re.findall(r"setTimeout\((?:[^()]|\([^()]*\))*?,\s*(\d+)\s*\)", body)]
    assert sum(delays) <= 1000, f"onCreate waits {sum(delays)} ms before redirecting"


def test_us_4_create_then_open_project_page_data_within_budget(client):
    # Backend half of the 5 s window: create + the project page's first
    # calls (detail, artifacts, first stage) must leave room for navigation.
    headers, _ = _session_for()
    start = time.monotonic()
    pid = _create_project(client, headers)["id"]
    assert client.get(f"/projects/{pid}", headers=headers).status_code == 200
    assert client.get(f"/projects/{pid}/artifacts", headers=headers).json() == []
    first = client.post(f"/projects/{pid}/generate/{ALL_STAGES[0]}", headers=headers)
    assert first.status_code == 200, first.text
    assert time.monotonic() - start < 2.0


@pytest.mark.parametrize("pid", ["warm-up", "WARM-UP", "warm-up%20", "00000000-0000-0000-0000-000000000000"])
def test_us_4_prefetch_like_ids_never_5xx(client, pid):
    headers, _ = _session_for()
    resp = client.get(f"/projects/{pid}", headers=headers)
    assert resp.status_code == 404, resp.text
    assert client.post(f"/projects/{pid}/generate/{ALL_STAGES[0]}", headers=headers).status_code == 404
    assert client.delete(f"/projects/{pid}", headers=headers).status_code == 404


def test_us_4_frontend_supports_production_build_on_ui_port():
    # DevOps follow-up: `npm run build && npm run start -- -p 3020`.
    scripts = _package_json().get("scripts") or {}
    assert "next build" in scripts.get("build", ""), f"build script is {scripts.get('build')!r}"
    start = scripts.get("start", "")
    assert "next start" in start, f"start script is {start!r}"
    pinned = _re.search(r"(?:-p|--port)[\s=]+(\d+)", start)
    assert not pinned or pinned.group(1) == "3020", f"start pins port {pinned.group(1)}; `-- -p 3020` would conflict"


# ---------- 4. a bold <h1> in every /crown-ai state ----------


def test_us_2_crown_ai_fallback_ready_and_token_states_each_have_page_title_h1():
    src = _strip_js_comments(_crown_ai_page())
    titles = [t for t in _re.findall(r"<h1\b[^>]*>", src) if "page-title" in t]
    assert len(titles) >= 3, f"expected the Suspense fallback, !ready and !token to each render a page-title <h1>, got {titles}"


def test_us_2_crown_ai_fallback_still_renders_loading_state():
    # Adding the heading must not replace the loading indicator.
    body = _suspense_fallback(_strip_js_comments(_crown_ai_page()))
    assert "LoadingState" in body or "aria-busy" in body or "role=\"status\"" in body


def test_us_2_page_title_class_is_bold():
    css = "\n".join(_read(p) for p in _global_css_files())
    if ".page-title" not in css:
        pytest.skip("no .page-title rule in global CSS")
    rule = _re.search(r"\.page-title\s*\{([^}]*)\}", css)
    assert rule, ".page-title rule not found"
    assert _re.search(r"font-(?:bold|extrabold|black)|font-weight\s*:\s*(?:[6-9]00|bold)", rule.group(1)), (
        f".page-title is not bold: {rule.group(1).strip()}"
    )


# ---------- 8. the UI suite expects the shim gone ----------


def test_us_2_tailwind_ui_suite_does_not_require_shim_to_exist():
    path = os.path.join(FRONTEND_DIR, "tests", "test_ui_generated.py")
    if not os.path.exists(path):
        pytest.skip("frontend/tests/test_ui_generated.py not present")
    src = _read(path)
    shim_vars = set(_re.findall(r"^\s*(\w+)\s*=.*tailwind[-_]postcss", src, _re.M))
    offenders = []
    for n, line in enumerate(src.splitlines(), 1):
        s = line.strip()
        if not s.startswith("assert ") or s.startswith("assert not "):
            continue
        mentions = "tailwind-postcss" in s or any(_re.search(rf"\b{v}\b", s) for v in shim_vars)
        if mentions and _re.search(r"os\.path\.(?:exists|isfile)\(|\.exists\(\)|\.is_file\(\)", s):
            offenders.append(f"{n}: {s[:120]}")
    assert not offenders, f"UI suite asserts the deleted shim exists: {offenders}"


# ---------- 11-16. the newer detectors stay in force ----------

_NEWER_SHIM_TESTS = {
    "test_us_2_tailwind_shim_absent_from_git_index_entirely": ["ls-files", "assert not hits"],
    "test_us_2_tailwind_shim_exact_confirm_command_prints_nothing": ["frontend/tailwind-postcss.*", '== ""'],
    "test_us_2_tailwind_shim_not_staged_under_another_path": ["--cached", "assert not staged"],
    "test_us_2_tailwind_no_local_postcss_plugin_in_frontend_sources": ["_LOCAL_POSTCSS_PLUGIN_RE", "assert not offenders"],
    "test_us_2_tailwind_no_hand_compile_in_any_spelling": ["_HAND_COMPILE_RE", "assert not offenders"],
    "test_us_2_tailwind_shim_plugin_name_not_in_any_text_file": ["_SHIM_PLUGIN_NAME", "assert not offenders"],
    "test_us_2_tailwind_shim_no_variant_anywhere_in_repo": ["_repo_walk", "assert not leftovers"],
}


@pytest.mark.parametrize("name", sorted(_NEWER_SHIM_TESTS))
def test_us_2_tailwind_newer_shim_detectors_not_weakened(name):
    body = _test_body(name)
    for needle in _NEWER_SHIM_TESTS[name]:
        assert needle in body, f"{name} no longer contains {needle!r}"
    assert "xfail" not in body and "pytest.skip" not in body
    decorated = _re.search(rf"@pytest\.mark\.(?:skip|skipif|xfail)[^\n]*\n(?:@[^\n]*\n)*def {name}\(", _this_test_file())
    assert not decorated, f"{name} is skipped/xfailed by a decorator"
