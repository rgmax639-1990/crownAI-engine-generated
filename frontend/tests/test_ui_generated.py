"""
UI tests for the Crownwright Technologies site and the Crown AI tool.

Why this file is shaped the way it is
-------------------------------------
The Go/No-Go scorecard was NO_GO because *no UI test results were ever
recorded*. What that needs in practice is a suite that always collects, always
finishes, and produces a real pass/fail for every user story:

* UI_BASE_URL is read without raising at import time (a KeyError during
  collection would record nothing); a missing/invalid value fails each test
  with a clear message instead.
* Every action/navigation has a bounded timeout, and the suite is kept small
  and fast (no multi-minute live quota runs, no 20-way bursts) so the run is
  never killed before it reports.
* Flows that need a signed-in user or a controlled failure use ``FakeBackend``
  (``page.route`` answering the browser's API calls), so each story gets a
  real result even when the API is down. OAuth with a real Google/Microsoft
  account can't be automated, so tests "return" from the provider the same way
  the backend does: by landing on /crown-ai/callback?token=...
* A handful of ``live`` tests use the real backend. Its reachability is probed
  once (5s) and a down backend fails those tests immediately with a clear
  message rather than stalling the run.
* ``test_us_2_suite_*`` tests check the suite itself: base URL, every route
  rendering without runtime errors, and that every story has UI tests.
"""

import ast
import json
import os
import re
import time
import uuid
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import Browser, Page, Route, expect


def _read_ui_base_url() -> str:
    try:
        return os.environ["UI_BASE_URL"].strip().rstrip("/")
    except KeyError:
        return ""


BASE_URL = _read_ui_base_url()

ACTION_TIMEOUT_MS = 15000
NAVIGATION_TIMEOUT_MS = 30000


def _ui_base_url_problem():
    if not BASE_URL:
        return "UI_BASE_URL is not set (or empty); point it at the running frontend, e.g. http://127.0.0.1:3000"
    parsed = urlparse(BASE_URL)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return f"UI_BASE_URL={BASE_URL!r} is not an absolute http(s) URL"
    return None


@pytest.fixture(autouse=True)
def _require_ui_base_url(request):
    problem = _ui_base_url_problem()
    if problem:
        pytest.fail(problem, pytrace=False)
    if "page" in request.fixturenames:
        page = request.getfixturevalue("page")
        page.set_default_timeout(ACTION_TIMEOUT_MS)
        page.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)


LEGAL_NAME = "Crownwright Technologies Pvt Ltd"
BRAND = "Crownwright"
ADDRESS_RE = re.compile(r"Maraimalar Nagar, Chennai", re.I)

TEST_TOKEN = "test-token-123"
STRIPE_URL = "https://checkout.stripe.com/c/pay/cs_live_test_session"

STAGES = [
    ("requirements", "Requirements", "Requirements Document"),
    ("design", "Design", "System Design"),
    ("code", "Code", "Source Code"),
    ("tests", "Test Cases", "Test Cases"),
    ("nfr", "NFR Testing", "Non-Functional Test Report"),
]
STAGE_ARTIFACT = {key: label for key, _, label in STAGES}

PAGES = {
    "/": re.compile(r"Crownwright builds software"),
    "/about": re.compile(r"About Crownwright"),
    "/services": re.compile(r"What we do"),
    "/services/crown-ai": re.compile(r"Crown AI"),
    "/case-studies": re.compile(r"Proof in delivery"),
    "/blog": re.compile(r"The Crownwright blog"),
    "/careers": re.compile(r"Build with Crownwright"),
    "/team": re.compile(r"The people behind Crownwright"),
    "/pricing": re.compile(r"Simple pricing for Crown AI"),
    "/contact": re.compile(r"Let.s talk"),
    "/legal/privacy": re.compile(r"Privacy Policy"),
    "/legal/terms": re.compile(r"Terms of Service"),
}

CASE_STUDIES = {
    "regional-nbfc-chennai": "Regional NBFC (Chennai)",
    "saas-logistics-bengaluru": "SaaS logistics platform (Bengaluru)",
    "eu-retail-gdpr": "European retail client (GDPR scope)",
}

def _plan(plan_id, name, seats, hosting, monthly_cents, annual_cents, features):
    return {"id": plan_id, "name": name, "seats": seats, "hosting": hosting, "currency": "usd",
            "setup_fee_cents": 500000, "monthly_cents": monthly_cents, "annual_cents": annual_cents,
            "amount_cents": 500000 + monthly_cents, "description": f"{name} plan.", "features": features}


PLANS = [
    _plan("mid", "Mid", 50, "shared platform", 750000, 9000000, ["50 seats", "Shared platform"]),
    _plan("large", "Large", 250, "dedicated", 2250000, 27000000, ["250 seats", "Dedicated platform"]),
    _plan("global", "Global", 1000, "HA + DR", 6000000, 72000000, ["1,000 seats", "HA + DR"]),
]

API_PATH_RE = re.compile(
    r"^/(auth/me|projects(/[^/?]+(/generate/[a-z]+|/download)?)?"
    r"|pricing/(plans|leads|checkout(/[^/]+)?)|contact(/map)?|consent/policy)$"
)
OAUTH_LOGIN_RE = re.compile(r"/auth/(google|microsoft)/login$")
SAVED_BUT_FAILED_RE = re.compile(r"saved your details, but couldn.t start checkout", re.I)


def goto(page: Page, path: str = "/", wait_until: str = "domcontentloaded"):
    return page.goto(BASE_URL + path, wait_until=wait_until)


def unique_email(prefix: str = "qa") -> str:
    return f"{prefix}.{uuid.uuid4().hex[:10]}@example.com"


def banner(page: Page):
    return page.get_by_role("banner")


def footer(page: Page):
    return page.get_by_role("contentinfo")


def consent_region(page: Page):
    return page.get_by_role("region", name="Privacy and cookie consent")


def accept_consent_when_in_the_way(page: Page):
    accept = consent_region(page).get_by_role("button", name="Accept", exact=True)

    def _accept(*_):
        accept.click()

    page.add_locator_handler(accept, _accept)


# ---------------------------------------------------------------------------
# Fake backend: answers the browser's API calls (fetch/XHR only)
# ---------------------------------------------------------------------------


class FakeBackend:
    def __init__(self, page: Page):
        self.page = page
        self.tier = "free"
        self.tokens = {TEST_TOKEN}
        self.projects: dict = {}
        self.calls: list = []  # (method, path, body, headers)
        self.counter = 0
        self.generations = 0
        self.gen_limit = None
        self.fail_stage_once: dict = {}
        self.create_error = None
        self.projects_fail_times = 0
        self.detail_fail_times = 0
        self.delete_error = False
        self.download_detail = {"message": "Downloads require a paid plan.", "upgrade_url": "/pricing"}
        self.plans_abort = False
        self.lead_error = None
        self.checkout_fail_times = 0
        self.checkout_error = (503, {"detail": "Payment gateway unavailable"})
        self.checkout_status = "succeeded"
        self.contact_error = None
        # (METHOD, path regex) -> hold the next matching request until release()
        self.hold_rules: list = []
        self.held: list = []

    def install(self):
        self.page.add_init_script(
            "try { localStorage.setItem('crownai_consent_dismissed', 'TEST'); } catch (e) {}"
        )
        self.page.route("**/*", self._handle)

    # -- helpers -----------------------------------------------------------
    def _id(self, prefix):
        self.counter += 1
        return f"{prefix}-{self.counter}"

    def seed_project(self, name, stages=(), requirements="Seeded requirements"):
        pid = self._id("proj")
        self.projects[pid] = {
            "id": pid, "name": name, "requirements": requirements, "status": "created",
            "created_at": "2026-09-01T10:00:00Z", "artifacts": [],
        }
        for stage in stages:
            self._add_artifact(pid, stage)
        return pid

    def _add_artifact(self, pid, stage):
        project = self.projects[pid]
        project["artifacts"].append({
            "id": self._id("art"), "stage": stage, "stage_label": STAGE_ARTIFACT[stage],
            "content": f"{STAGE_ARTIFACT[stage]} generated for {project['name']}",
            "created_at": "2026-09-01T10:05:00Z",
        })
        project["status"] = stage

    def calls_to(self, method, pattern):
        rx = re.compile(pattern)
        return [c for c in self.calls if c[0] == method and rx.search(c[1])]

    def generated_stages(self):
        return [c[1].rsplit("/", 1)[-1] for c in self.calls_to("POST", r"/generate/")]

    def hold(self, method, pattern):
        self.hold_rules.append((method, re.compile(pattern)))

    def wait_for_held(self, n=1, timeout_s=10):
        deadline = time.monotonic() + timeout_s
        while len(self.held) < n and time.monotonic() < deadline:
            self.page.wait_for_timeout(50)
        assert len(self.held) >= n, "The expected API request was never sent"

    def release(self):
        held, self.held = self.held, []
        for args in held:
            self._answer(*args)

    @staticmethod
    def _cors(request):
        return {
            "access-control-allow-origin": request.headers.get("origin", "*"),
            "access-control-allow-methods": "GET, POST, DELETE, OPTIONS",
            "access-control-allow-headers": "Authorization, Content-Type",
            "access-control-expose-headers": "Content-Disposition",
        }

    def _json(self, route, request, status, body):
        headers = self._cors(request)
        headers["content-type"] = "application/json"
        try:
            route.fulfill(status=status, headers=headers, body=json.dumps(body))
        except Exception:
            pass  # the browser already gave up on this request

    def _authorized(self, request):
        auth = request.headers.get("authorization", "")
        return auth.startswith("Bearer ") and auth[7:] in self.tokens

    # -- routing -----------------------------------------------------------
    def _handle(self, route: Route):
        request = route.request
        parsed = urlparse(request.url)
        if parsed.hostname == "checkout.stripe.com":
            route.fulfill(status=200, content_type="text/html",
                          body="<html><body><h1>Secure checkout</h1><p>Stripe hosted payment page (test stub)</p></body></html>")
            return
        path = re.sub(r"^/api(?=/)", "", parsed.path)
        if request.resource_type == "document" and OAUTH_LOGIN_RE.search(path):
            provider = OAUTH_LOGIN_RE.search(path).group(1)
            route.fulfill(status=200, content_type="text/html",
                          body=f"<html><body><h1>{provider} sign-in (test stub)</h1></body></html>")
            return
        if (
            request.resource_type not in ("fetch", "xhr")
            or not API_PATH_RE.match(path)
            or request.headers.get("rsc")
        ):
            route.fallback()
            return
        if request.method == "OPTIONS":
            route.fulfill(status=204, headers=self._cors(request), body="")
            return
        try:
            body = request.post_data_json if request.post_data else None
        except Exception:
            body = None
        self.calls.append((request.method, path, body, dict(request.headers)))
        query = parse_qs(parsed.query)
        for i, (method, rx) in enumerate(self.hold_rules):
            if method == request.method and rx.search(path):
                del self.hold_rules[i]
                self.held.append((route, request, path, request.method, body, query))
                return
        self._answer(route, request, path, request.method, body, query)

    def _answer(self, route, request, path, method, body, query):
        if path == "/consent/policy":
            return self._json(route, request, 200, {
                "country": (query.get("country") or ["US"])[0], "regime": "TEST",
                "regime_name": "Test regime", "requires_opt_in": False,
                "banner_text": "Test banner.", "rights": ["access"],
            })
        if path == "/pricing/plans":
            if self.plans_abort:
                return route.abort()
            return self._json(route, request, 200, PLANS)
        if path == "/pricing/leads" and method == "POST":
            if self.lead_error:
                return self._json(route, request, *self.lead_error)
            n = len(self.calls_to("POST", r"^/pricing/leads$"))
            return self._json(route, request, 201, {"id": f"lead-{n}"})
        if path == "/pricing/checkout" and method == "POST":
            if request.headers.get("authorization") and not self._authorized(request):
                return self._json(route, request, 401, {"detail": "Not authenticated"})
            if self.checkout_fail_times > 0:
                self.checkout_fail_times -= 1
                return self._json(route, request, *self.checkout_error)
            return self._json(route, request, 200, {"checkout_url": STRIPE_URL})
        if path.startswith("/pricing/checkout/") and method == "GET":
            return self._json(route, request, 200, {"status": self.checkout_status, "plan": "mid"})
        if path == "/contact/map":
            return self._json(route, request, 503, {"detail": "Map is temporarily unavailable."})
        if path == "/contact" and method == "POST":
            if self.contact_error:
                return self._json(route, request, *self.contact_error)
            return self._json(route, request, 201, {"id": "inq-1"})

        if not self._authorized(request):
            return self._json(route, request, 401, {"detail": "Not authenticated"})

        if path == "/auth/me":
            if method == "DELETE":
                self.tokens.clear()
                self.projects.clear()
                return route.fulfill(status=204, headers=self._cors(request), body="")
            return self._json(route, request, 200, {
                "id": "user-1", "email": "dev@example.com", "name": "Dev User",
                "provider": "google", "tier": self.tier,
            })
        if path == "/projects" and method == "GET":
            if self.projects_fail_times > 0:
                self.projects_fail_times -= 1
                return self._json(route, request, 500, {"detail": "boom"})
            return self._json(route, request, 200,
                              [{k: v for k, v in p.items() if k != "artifacts"} for p in self.projects.values()])
        if path == "/projects" and method == "POST":
            if self.create_error:
                return self._json(route, request, *self.create_error)
            pid = self.seed_project(body["name"], requirements=body["requirements"])
            return self._json(route, request, 201,
                              {k: v for k, v in self.projects[pid].items() if k != "artifacts"})

        m = re.match(r"^/projects/([^/]+)(?:/generate/([a-z]+)|/(download))?$", path)
        if m:
            project = self.projects.get(m.group(1))
            if project is None:
                return self._json(route, request, 404, {"detail": "Project not found"})
            if m.group(2) and method == "POST":
                stage = m.group(2)
                if stage in self.fail_stage_once:
                    return self._json(route, request, *self.fail_stage_once.pop(stage))
                if self.gen_limit is not None and self.generations >= self.gen_limit:
                    return self._json(route, request, 429, {
                        "detail": "Daily free-tier generation limit reached (5/day). Upgrade for unlimited generations."})
                self.generations += 1
                self._add_artifact(project["id"], stage)
                return self._json(route, request, 200, {"stage": stage})
            if m.group(3) and method == "POST":
                if self.tier == "free":
                    return self._json(route, request, 402, {"detail": self.download_detail})
                headers = self._cors(request)
                headers["content-type"] = "application/zip"
                headers["content-disposition"] = 'attachment; filename="crownai_export.zip"'
                return route.fulfill(status=200, headers=headers, body=b"PK\x05\x06" + b"\x00" * 18)
            if method == "GET":
                if self.detail_fail_times > 0:
                    self.detail_fail_times -= 1
                    return self._json(route, request, 500, {"detail": "boom"})
                return self._json(route, request, 200, project)
            if method == "DELETE":
                if self.delete_error:
                    return self._json(route, request, 500, {"detail": "boom"})
                del self.projects[project["id"]]
                return route.fulfill(status=204, headers=self._cors(request), body="")
        route.fallback()


@pytest.fixture
def backend(page: Page) -> FakeBackend:
    fake = FakeBackend(page)
    fake.install()
    accept_consent_when_in_the_way(page)
    return fake


def sign_in(page: Page, token: str = TEST_TOKEN):
    """Return from the OAuth provider the way the backend's callback does."""
    goto(page, f"/crown-ai/callback?token={token}")
    expect(page).to_have_url(re.compile(r"/crown-ai/?$"))
    expect(page.get_by_role("button", name="Sign out")).to_be_visible()
    expect(page.get_by_text("Dev User · dev@example.com")).to_be_visible()


def sign_in_heading(page: Page):
    return page.get_by_role("heading", level=1, name=re.compile(r"Sign in to Crown AI"))


def reveal_providers(page: Page, trigger_name=re.compile(r"^Sign in")):
    page.get_by_role("button", name=trigger_name).first.click()
    google = page.get_by_role("link", name="Sign in with Google")
    microsoft = page.get_by_role("link", name="Sign in with Microsoft")
    expect(google).to_be_visible()
    expect(microsoft).to_be_visible()
    return google, microsoft


def create_project(page: Page, name: str, requirements: str):
    page.get_by_role("button", name="+ New Project").click()
    page.get_by_label("Project name").fill(name)
    page.get_by_label(re.compile(r"^Requirements")).fill(requirements)
    page.get_by_role("button", name="Generate Project").click()


def stage_done(page: Page, label: str):
    return page.get_by_role("button", name=f"✓ {label}", exact=True)


def progress(page: Page):
    return page.get_by_test_id("generation-progress")


def open_upgrade(page: Page, plan: str = "Mid"):
    button = page.get_by_role("button", name=f"Upgrade to {plan}")
    expect(button).to_be_enabled()  # disabled until hydrated
    button.click()
    dialog = page.get_by_role("dialog", name=f"Upgrade to {plan}")
    expect(dialog).to_be_visible()
    return dialog


def fill_lead(dialog, email="priya@example.com", name="Priya Raman", company="Example Corp", phone="+91 98765 43210"):
    dialog.get_by_label(re.compile(r"^Full name")).fill(name)
    dialog.get_by_label(re.compile(r"^Work email")).fill(email)
    dialog.get_by_label(re.compile(r"^Company")).fill(company)
    dialog.get_by_label(re.compile(r"^Phone")).fill(phone)


def expect_on_stripe(page: Page):
    expect(page).to_have_url(re.compile(r"^https://checkout\.stripe\.com/"))
    expect(page.get_by_role("heading", name="Secure checkout")).to_be_visible()


def contact_field(page: Page, label: str):
    return page.get_by_label(re.compile(rf"^{label}"))


# ---------------------------------------------------------------------------
# Live backend helpers (probe once, fail fast)
# ---------------------------------------------------------------------------

_LIVE = {"base": None, "error": None}


def live_backend(page: Page) -> str:
    if _LIVE["error"]:
        pytest.fail(_LIVE["error"], pytrace=False)
    accept_consent_when_in_the_way(page)
    if _LIVE["base"]:
        return _LIVE["base"]
    try:
        goto(page, "/crown-ai")
        google, _ = reveal_providers(page)
        href = google.get_attribute("href") or ""
        assert href.endswith("/auth/google/login"), f"Unexpected Google sign-in href {href!r}"
        base = href[: -len("/auth/google/login")]
        resp = page.request.get(base + "/pricing/plans", timeout=5000)
        assert resp.status == 200, f"GET {base}/pricing/plans -> HTTP {resp.status}"
    except Exception as exc:
        first = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        _LIVE["error"] = (
            f"Live backend not reachable from the browser ({first}). Start the API at the address the "
            "frontend was built with (NEXT_PUBLIC_API_BASE, default http://127.0.0.1:8000)."
        )
        pytest.fail(_LIVE["error"], pytrace=False)
    _LIVE["base"] = base
    return base


def stub_external_documents(page: Page, local_hosts):
    def handler(route: Route):
        req = route.request
        host = urlparse(req.url).hostname
        if req.resource_type == "document" and host not in local_hosts:
            route.fulfill(status=200, content_type="text/html",
                          body=f"<html><body><h1>External page stub</h1><p>{host}</p></body></html>")
        else:
            route.fallback()

    page.route("**/*", handler)


# ===========================================================================
# US-1: Sign in with Google or Microsoft
# ===========================================================================


def test_us_1_service_page_sign_in_offers_only_google_and_microsoft(page: Page, backend: FakeBackend):
    goto(page, "/services/crown-ai")
    google, microsoft = reveal_providers(page, "Sign in & start free")
    expect(page.get_by_text("Choose a provider to sign in")).to_be_visible()
    expect(google).to_have_attribute("href", re.compile(r"/auth/google/login$"))
    expect(microsoft).to_have_attribute("href", re.compile(r"/auth/microsoft/login$"))
    expect(page.locator("input[type=password]")).to_have_count(0)
    expect(page.get_by_role("link", name=re.compile(r"^Sign in with")).filter(
        has_not_text=re.compile("Google|Microsoft"))).to_have_count(0)
    google.click()
    expect(page).to_have_url(re.compile(r"/auth/google/login$"))


def test_us_1_microsoft_option_starts_microsoft_oauth(page: Page, backend: FakeBackend):
    goto(page, "/crown-ai")
    expect(sign_in_heading(page)).to_be_visible()
    _, microsoft = reveal_providers(page)
    microsoft.click()
    expect(page).to_have_url(re.compile(r"/auth/microsoft/login$"))


def test_us_1_header_launch_leads_to_sign_in(page: Page, backend: FakeBackend):
    goto(page, "/")
    banner(page).get_by_role("link", name="Launch Crown AI").click()
    expect(page).to_have_url(re.compile(r"/crown-ai/?$"))
    expect(sign_in_heading(page)).to_be_visible()
    expect(page.get_by_role("button", name="Sign in", exact=True)).to_have_attribute("aria-expanded", "false")


def test_us_1_callback_lands_in_workspace_with_session(page: Page, backend: FakeBackend):
    sign_in(page)
    expect(page.get_by_text("Free tier", exact=True)).to_be_visible()
    me = backend.calls_to("GET", r"^/auth/me$")
    assert me, "Workspace never loaded the signed-in user"
    assert me[-1][3].get("authorization") == f"Bearer {TEST_TOKEN}", "Session token not sent to the API"
    page.reload()
    expect(page.get_by_role("button", name="Sign out")).to_be_visible()


def test_us_1_callback_without_token_shows_error(page: Page, backend: FakeBackend):
    goto(page, "/crown-ai/callback")
    expect(page).to_have_url(re.compile(r"/crown-ai\?error=sign_in_failed"))
    expect(page.get_by_text(re.compile(r"Sign-in didn.t complete"))).to_be_visible()
    expect(sign_in_heading(page)).to_be_visible()
    expect(page.get_by_role("button", name="Sign out")).to_have_count(0)


def test_us_1_invalid_or_expired_token_returns_to_sign_in(page: Page, backend: FakeBackend):
    goto(page, "/crown-ai/callback?token=expired-token")
    expect(sign_in_heading(page)).to_be_visible()
    expect(page.get_by_role("button", name="Sign out")).to_have_count(0)
    page.reload()
    expect(sign_in_heading(page)).to_be_visible()


def test_us_1_sign_out_ends_session(page: Page, backend: FakeBackend):
    sign_in(page)
    page.get_by_role("button", name="Sign out").click()
    expect(sign_in_heading(page)).to_be_visible()
    page.reload()
    expect(sign_in_heading(page)).to_be_visible()


def test_us_1_live_oauth_login_redirects_to_provider(page: Page):
    base = live_backend(page)
    for provider, rx in (("google", r"google"), ("microsoft", r"microsoft|login\.live\.com")):
        resp = page.request.get(f"{base}/auth/{provider}/login", max_redirects=0, timeout=10000)
        assert resp.status in (302, 303, 307), f"/auth/{provider}/login -> HTTP {resp.status}, expected a redirect"
        location = resp.headers.get("location", "")
        assert re.search(rx, location, re.I), f"{provider} login redirected to {location!r}"


# ===========================================================================
# US-2: Corporate marketing site with full sections
# ===========================================================================

NAV = [
    ("About Us", "/about"), ("Services", "/services"), ("Case Studies", "/case-studies"),
    ("Blog", "/blog"), ("Careers", "/careers"), ("Team", "/team"), ("Pricing", "/pricing"),
    ("Contact", "/contact"), ("Home", "/"),
]


def test_us_2_main_menu_reaches_every_section(page: Page, backend: FakeBackend):
    page.set_viewport_size({"width": 1440, "height": 900})
    goto(page, "/")
    nav = banner(page).get_by_role("navigation", name="Main")
    for label, path in NAV:
        nav.get_by_role("link", name=label, exact=True).click()
        expect(page).to_have_url(re.compile(re.escape(BASE_URL + path) + r"/?$"))
        expect(page.get_by_role("heading", level=1)).to_contain_text(PAGES[path])
        expect(nav.get_by_role("link", name=label, exact=True)).to_have_attribute("aria-current", "page")


def test_us_2_about_page_has_no_logo_showcase_and_new_legal_name(page: Page, backend: FakeBackend):
    goto(page, "/about")
    expect(page.get_by_role("heading", level=1)).to_contain_text(PAGES["/about"])
    expect(page.get_by_text(LEGAL_NAME).first).to_be_visible()
    expect(page.get_by_role("heading", name=re.compile(r"One brand for the company"))).to_have_count(0)
    expect(page.get_by_text(re.compile(r"(Light|Dark) theme logo"))).to_have_count(0)
    expect(page.locator("body")).not_to_contain_text("Ishanvi Technologies")


def test_us_2_type_scale_is_restrained_and_readable(page: Page, backend: FakeBackend):
    page.set_viewport_size({"width": 1440, "height": 900})
    for path in ("/", "/about", "/pricing"):
        goto(page, path)
        sizes = page.evaluate(
            """() => {
                const px = (el) => el ? parseFloat(getComputedStyle(el).fontSize) : null;
                const small = [...document.querySelectorAll('main p, main li')]
                    .filter((el) => el.offsetParent !== null)
                    .map(px);
                return { body: px(document.body), h1: px(document.querySelector('h1')),
                         minText: Math.min(...small) };
            }"""
        )
        assert sizes["body"] == 16, (path, sizes)
        assert 28 <= sizes["h1"] <= 50, (path, sizes)
        assert sizes["minText"] >= 12, (path, sizes)


def test_us_2_services_feature_crown_ai_as_flagship(page: Page, backend: FakeBackend):
    goto(page, "/services")
    flagship = page.get_by_role("link", name=re.compile(r"Flagship product.*Crown AI", re.S))
    expect(flagship).to_be_visible()
    for other in ("Custom Software Development", "Quality Engineering & Test Automation"):
        expect(page.get_by_role("heading", name=other)).to_be_visible()
    flagship.click()
    expect(page).to_have_url(re.compile(r"/services/crown-ai$"))
    for stage in ("Requirements Gathering", "Design", "Coding", "Testing (STLC)", "Non-Functional Testing"):
        expect(page.get_by_role("heading", name=stage, exact=True)).to_be_visible()


def test_us_2_footer_shows_company_address_and_legal_pages(page: Page, backend: FakeBackend):
    for path in ("/", "/pricing", "/crown-ai"):
        goto(page, path)
        expect(footer(page).get_by_text(LEGAL_NAME).first).to_be_visible()
        expect(footer(page).get_by_text(ADDRESS_RE)).to_be_visible()
        expect(footer(page).get_by_text("Tamil Nadu, India")).to_be_visible()
    footer(page).get_by_role("link", name="Privacy Policy").click()
    expect(page).to_have_url(re.compile(r"/legal/privacy$"))
    expect(page.get_by_role("heading", level=1)).to_have_text("Privacy Policy")
    footer(page).get_by_role("link", name="Terms of Service").click()
    expect(page).to_have_url(re.compile(r"/legal/terms$"))
    expect(page.get_by_role("heading", level=1)).to_have_text("Terms of Service")
    expect(page.get_by_text(ADDRESS_RE).first).to_be_visible()


def test_us_2_mobile_menu_and_no_horizontal_scroll(page: Page, backend: FakeBackend):
    page.set_viewport_size({"width": 375, "height": 812})
    goto(page, "/")
    toggle = page.get_by_role("button", name="Open menu")
    expect(toggle).to_have_attribute("aria-expanded", "false")
    toggle.click()
    close = page.get_by_role("button", name="Close menu")
    expect(close).to_have_attribute("aria-expanded", "true")
    page.get_by_role("navigation", name="Main").get_by_role("link", name="Pricing", exact=True).click()
    expect(page).to_have_url(re.compile(r"/pricing$"))
    expect(page.get_by_role("button", name="Open menu")).to_be_visible()  # closed after navigating
    for path in list(PAGES) + ["/crown-ai"]:
        goto(page, path, wait_until="load")
        overflow = page.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
        assert overflow <= 1, f"{path} scrolls sideways by {overflow}px on a 375px phone"


def test_us_2_marketing_pages_load_under_2_5_seconds(page: Page, backend: FakeBackend):
    for path in PAGES:  # warm-up (first request may compile in dev mode)
        goto(page, path, wait_until="load")
    slow = []
    for path in PAGES:
        goto(page, path, wait_until="load")
        ms = page.evaluate(
            "() => { const n = performance.getEntriesByType('navigation')[0]; return n ? n.loadEventEnd - n.startTime : -1; }"
        )
        if ms < 0 or ms > 2500:
            slow.append(f"{path}: {ms:.0f}ms")
    assert not slow, "Pages over the 2.5s load budget: " + ", ".join(slow)


# -- Suite health: the UI suite must run and record a real result ----------


def test_us_2_suite_base_url_is_used_and_home_renders(page: Page, backend: FakeBackend):
    assert BASE_URL == os.environ["UI_BASE_URL"].strip().rstrip("/")
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    response = goto(page, "/", wait_until="load")
    assert response is not None and response.status == 200, f"{BASE_URL}/ -> {response.status if response else None}"
    expect(page.get_by_role("heading", level=1)).to_contain_text("Crownwright builds software")
    expect(page.get_by_text(re.compile(r"Unhandled Runtime Error|Failed to compile|Build Error"))).to_have_count(0)
    assert not errors, f"JavaScript errors on the home page: {errors}"


def test_us_2_suite_every_route_renders_without_runtime_errors(page: Page, backend: FakeBackend):
    errors = []
    page.on("pageerror", lambda e: errors.append(f"{page.url}: {e}"))
    routes = dict(PAGES)
    routes["/crown-ai"] = re.compile(r"Sign in to Crown AI")
    for slug, client in CASE_STUDIES.items():
        routes[f"/case-studies/{slug}"] = re.compile(re.escape(client))
    problems = []
    for path, heading in routes.items():
        response = goto(page, path)
        if response is None or response.status != 200:
            problems.append(f"{path} -> HTTP {response.status if response else None}")
            continue
        expect(page.get_by_role("heading", level=1)).to_contain_text(heading)
        expect(footer(page).get_by_text(ADDRESS_RE)).to_be_visible()
    assert not problems, "Routes not served: " + "; ".join(problems)
    assert not errors, "Runtime errors: " + "; ".join(errors)


def test_us_2_suite_unknown_route_is_branded_404(page: Page, backend: FakeBackend):
    response = goto(page, f"/no-such-page-{uuid.uuid4().hex[:6]}")
    assert response is not None and response.status == 404, f"Unknown route -> {response.status if response else None}"
    expect(page.get_by_role("heading", level=1)).to_have_text(re.compile(r"couldn.t find that page"))
    page.get_by_role("link", name="Go to the home page").click()
    expect(page).to_have_url(re.compile(r"/$"))


def test_us_2_suite_every_story_has_ui_tests():
    with open(__file__, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    names = [n.name for n in tree.body if isinstance(n, ast.FunctionDef) and n.name.startswith("test")]
    dupes = sorted({n for n in names if names.count(n) > 1})
    assert not dupes, f"Duplicate test names shadow each other (they never run): {dupes}"
    bad = [n for n in names if not re.match(r"^test_us_(\d+)_", n)]
    assert not bad, f"Tests not named after a story key: {bad}"
    for n in range(1, 13):
        mine = [x for x in names if x.startswith(f"test_us_{n}_")]
        assert len(mine) >= 3, f"US-{n} has only {len(mine)} UI tests: {mine}"


def test_us_2_suite_live_backend_reachable_from_browser(page: Page):
    base = live_backend(page)
    host = urlparse(base).hostname or ""
    assert host in ("localhost", "127.0.0.1") or "." in host, f"API base {base!r} is not browser-resolvable"


# ===========================================================================
# US-3: Unified branding across tool and company
# ===========================================================================

CROWN_FINGERPRINT_JS = """
() => Array.from(document.querySelectorAll('svg.cw-logo'))
  .filter(svg => svg.getBoundingClientRect().width > 0)
  .map(svg => { const p = svg.querySelector('.cw-cr path'); return p ? p.getAttribute('d') : null; })
"""


def test_us_3_same_logo_on_site_and_crown_ai_tool(page: Page, backend: FakeBackend):
    page.set_viewport_size({"width": 1280, "height": 900})
    seen = {}
    for path in ("/", "/services/crown-ai", "/pricing", "/crown-ai"):
        goto(page, path)
        expect(banner(page).locator("svg.cw-logo:visible").first).to_be_visible()
        seen[path] = page.evaluate(CROWN_FINGERPRINT_JS)
    sign_in(page)
    seen["workspace"] = page.evaluate(CROWN_FINGERPRINT_JS)
    marks = {d for ds in seen.values() for d in ds}
    assert None not in marks and len(marks) == 1, f"Different logo artwork across site and tool: {seen}"
    assert len(seen["workspace"]) >= 3, "Workspace should show the brand logo in header, tool bar and footer"


def test_us_3_brand_names_company_and_tool_together(page: Page, backend: FakeBackend):
    goto(page, "/")
    expect(banner(page).get_by_role("link", name=f"{BRAND} home")).to_be_visible()
    expect(footer(page).get_by_text(re.compile(
        rf"{BRAND} and Crown AI are brands of {re.escape(LEGAL_NAME)}"))).to_be_visible()
    expect(page.get_by_text(re.compile(r"One identity for the company and the tool"))).to_be_visible()
    sign_in(page)
    expect(page.get_by_text(BRAND, exact=True).first).to_be_visible()


def test_us_3_logo_link_returns_home_from_tool_pages(page: Page, backend: FakeBackend):
    goto(page, "/crown-ai")
    banner(page).get_by_role("link", name=f"{BRAND} home").click()
    expect(page).to_have_url(re.compile(r"/$"))
    page.set_viewport_size({"width": 360, "height": 740})
    goto(page, "/pricing")
    expect(banner(page).locator("svg.cw-logo:visible").first).to_be_visible()


# ===========================================================================
# US-4: Create a Crown AI project and generate SDLC/STLC artifacts
# ===========================================================================


def test_us_4_create_project_generates_all_five_artifacts(page: Page, backend: FakeBackend):
    sign_in(page)
    expect(page.get_by_text("No projects yet")).to_be_visible()
    create_project(page, "Loyalty App", "A customer loyalty points system for a retail app.")
    expect(page).to_have_url(re.compile(r"/crown-ai/projects/[^/?]+"))
    expect(page.get_by_role("heading", level=1)).to_have_text("Loyalty App")
    expect(progress(page)).to_contain_text("All stages complete (5/5)", timeout=30000)
    assert backend.generated_stages() == [s[0] for s in STAGES], backend.generated_stages()
    for key, label, artifact in STAGES:
        stage_done(page, label).click()
        expect(page.get_by_role("heading", name=artifact, exact=True)).to_be_visible()
        expect(page.get_by_text(f"{artifact} generated for Loyalty App")).to_be_visible()
    expect(page.get_by_role("button", name="Generate all artifacts")).to_have_count(0)


def test_us_4_progress_shows_within_3_seconds(page: Page, backend: FakeBackend):
    # Warm the project route first so a dev server's one-off compile isn't
    # counted against the 3s budget.
    goto(page, "/crown-ai/projects/warm-up", wait_until="load")
    sign_in(page)
    backend.hold("POST", r"/generate/requirements$")
    page.get_by_role("button", name="+ New Project").click()
    page.get_by_label("Project name").fill("Progress App")
    page.get_by_label(re.compile(r"^Requirements")).fill("Track parcels.")
    started = time.monotonic()
    page.get_by_role("button", name="Generate Project").click()
    expect(page.get_by_role("button", name="Generating…")).to_be_visible(timeout=3000)
    assert time.monotonic() - started < 3
    expect(page).to_have_url(re.compile(r"/crown-ai/projects/"))
    landed = time.monotonic()
    expect(progress(page)).to_contain_text("Generating Requirements (1/5)", timeout=3000)
    assert time.monotonic() - landed < 3, "Generation progress took over 3s to appear"
    expect(page.get_by_text("Generating your first artifact…")).to_be_visible()
    backend.wait_for_held()
    backend.release()
    expect(progress(page)).to_contain_text("All stages complete (5/5)", timeout=30000)


def test_us_4_create_form_validation_and_double_submit(page: Page, backend: FakeBackend):
    sign_in(page)
    page.get_by_role("button", name="+ New Project").click()
    page.get_by_role("button", name="Generate Project").click()
    expect(page.get_by_text("Give your project a name.")).to_be_visible()
    expect(page.get_by_text(re.compile(r"Describe what you want to build"))).to_be_visible()
    expect(page.get_by_label("Project name")).to_be_focused()
    page.get_by_label("Project name").fill("   ")
    page.get_by_label(re.compile(r"^Requirements")).fill("Spec")
    page.get_by_role("button", name="Generate Project").click()
    expect(page.get_by_text("Give your project a name.")).to_be_visible()
    assert not backend.calls_to("POST", r"^/projects$"), "Invalid form was sent to the API"
    expect(page.get_by_text("4 / 8,000")).to_be_visible()

    backend.hold("POST", r"^/projects$")
    page.get_by_label("Project name").fill("Once Only")
    page.get_by_role("button", name="Generate Project").click()
    busy = page.get_by_role("button", name="Generating…")
    expect(busy).to_be_disabled()
    expect(page.get_by_text(re.compile(r"Creating your project"))).to_be_visible()
    backend.wait_for_held()
    backend.release()
    expect(page).to_have_url(re.compile(r"/crown-ai/projects/"))
    assert len(backend.calls_to("POST", r"^/projects$")) == 1


def test_us_4_create_project_server_error_is_shown(page: Page, backend: FakeBackend):
    backend.create_error = (500, {"detail": "Project service is unavailable."})
    sign_in(page)
    create_project(page, "Broken", "Anything")
    expect(page.get_by_text("Project service is unavailable.")).to_be_visible()
    expect(page.get_by_label("Project name")).to_have_value("Broken")
    expect(page.get_by_role("button", name="Generate Project")).to_be_enabled()


def test_us_4_stage_failure_stops_pipeline_and_can_resume(page: Page, backend: FakeBackend):
    backend.fail_stage_once["design"] = (500, {"detail": "Design generation failed. Please retry."})
    sign_in(page)
    create_project(page, "Resume App", "Inventory tracking.")
    expect(page.get_by_test_id("generation-error")).to_contain_text("Design generation failed", timeout=15000)
    expect(stage_done(page, "Requirements")).to_be_visible()
    expect(progress(page)).to_contain_text("1/5 stages complete")
    assert backend.generated_stages() == ["requirements", "design"], "Pipeline kept going after a failure"
    expect(page.get_by_role("button", name="Code", exact=True)).to_be_disabled()  # locked
    page.get_by_role("button", name="Generate all artifacts").click()
    expect(progress(page)).to_contain_text("All stages complete (5/5)", timeout=30000)


def test_us_4_manual_stage_by_stage_generation(page: Page, backend: FakeBackend):
    pid = backend.seed_project("Manual App")
    sign_in(page)
    goto(page, f"/crown-ai/projects/{pid}")
    expect(page.get_by_text("No artifacts generated yet")).to_be_visible()
    expect(progress(page)).to_contain_text("No stages run yet (0/5)")
    expect(page.get_by_role("button", name="Download artifacts")).to_be_disabled()
    for _, label, artifact in STAGES:
        page.get_by_role("button", name=f"Generate {label}", exact=True).click()
        expect(stage_done(page, label)).to_be_visible(timeout=10000)
        expect(page.get_by_role("heading", name=artifact, exact=True)).to_be_visible()
    expect(progress(page)).to_contain_text("All stages complete (5/5)")


def test_us_4_project_access_rules(page: Page, backend: FakeBackend):
    goto(page, "/crown-ai/projects/some-project")
    expect(sign_in_heading(page)).to_be_visible()  # signed-out visitors are sent to sign in
    sign_in(page)
    goto(page, "/crown-ai/projects/someone-elses-project")
    expect(page.get_by_text("Project not found")).to_be_visible()
    page.get_by_role("link", name="Back to workspace").click()
    expect(page).to_have_url(re.compile(r"/crown-ai/?$"))


def test_us_4_workspace_load_error_and_retry(page: Page, backend: FakeBackend):
    backend.projects_fail_times = 1
    sign_in_url = f"/crown-ai/callback?token={TEST_TOKEN}"
    goto(page, sign_in_url)
    expect(page.get_by_text("Couldn't load your workspace")).to_be_visible()
    page.get_by_role("button", name="Try again").click()
    expect(page.get_by_text("No projects yet")).to_be_visible()


def test_us_4_workspace_shows_loading_state(page: Page, backend: FakeBackend):
    backend.hold("GET", r"^/projects$")
    goto(page, f"/crown-ai/callback?token={TEST_TOKEN}")
    expect(page.get_by_text("Loading projects…")).to_be_visible()
    backend.wait_for_held()
    backend.release()
    expect(page.get_by_text("No projects yet")).to_be_visible()


# ===========================================================================
# US-5: Free-tier limit blocks code download
# ===========================================================================


def test_us_5_free_tier_download_from_project_redirects_to_pricing(page: Page, backend: FakeBackend):
    pid = backend.seed_project("Code App", stages=["requirements", "design", "code"])
    sign_in(page)
    goto(page, f"/crown-ai/projects/{pid}")
    expect(page.get_by_text("Requires a paid plan", exact=False)).to_be_visible()
    page.get_by_role("button", name="Download artifacts").click()
    expect(page).to_have_url(re.compile(r"/pricing\?reason=download"))
    expect(page.get_by_text(re.compile(r"Downloading generated code and reports is part of our paid plans"))).to_be_visible()
    expect(page.get_by_role("button", name="Upgrade to Mid")).to_be_visible()


def test_us_5_free_tier_download_from_workspace_list_redirects(page: Page, backend: FakeBackend):
    backend.seed_project("List App", stages=["requirements", "design", "code"])
    sign_in(page)
    download = page.get_by_role("button", name="Download List App")
    expect(download).to_have_text(re.compile(r"🔒\s*Download"))
    download.click()
    expect(page).to_have_url(re.compile(r"/pricing\?reason=download"))


def test_us_5_free_tier_can_still_generate_code(page: Page, backend: FakeBackend):
    pid = backend.seed_project("Gen App", stages=["requirements", "design"])
    sign_in(page)
    expect(page.get_by_text("Free tier", exact=True)).to_be_visible()
    expect(page.get_by_text(re.compile(r"Free tier: up to 5 generations per day"))).to_be_visible()
    goto(page, f"/crown-ai/projects/{pid}")
    page.get_by_role("button", name="Generate Code", exact=True).click()
    expect(page.get_by_text("Source Code generated for Gen App")).to_be_visible()


def test_us_5_daily_cap_shows_limit_and_link_to_pricing(page: Page, backend: FakeBackend):
    backend.gen_limit = 2
    sign_in(page)
    create_project(page, "Capped App", "Something big.")
    alert = page.get_by_test_id("generation-error")
    expect(alert).to_contain_text("limit reached", timeout=15000)
    expect(stage_done(page, "Design")).to_be_visible()
    assert backend.generated_stages() == ["requirements", "design", "code"], "Kept generating past the cap"
    alert.get_by_role("link", name="View plans").click()
    expect(page).to_have_url(re.compile(r"/pricing"))


def test_us_5_offsite_upgrade_url_is_not_followed(page: Page, backend: FakeBackend):
    backend.download_detail = {"message": "Paid plan required", "upgrade_url": "https://evil.example/phish"}
    pid = backend.seed_project("Safe App", stages=["requirements"])
    sign_in(page)
    goto(page, f"/crown-ai/projects/{pid}")
    page.get_by_role("button", name="Download artifacts").click()
    expect(page).to_have_url(re.compile(re.escape(BASE_URL) + r"/pricing\?reason=download"))


def test_us_5_workspace_upgrade_link_goes_to_pricing(page: Page, backend: FakeBackend):
    sign_in(page)
    page.get_by_role("link", name="Upgrade", exact=True).click()
    expect(page).to_have_url(re.compile(r"/pricing$"))
    expect(page.get_by_role("heading", level=1)).to_contain_text("Simple pricing for Crown AI")


# ===========================================================================
# US-6: Lead capture before checkout
# ===========================================================================


def test_us_6_lead_form_must_be_submitted_before_payment(page: Page, backend: FakeBackend):
    goto(page, "/pricing")
    dialog = open_upgrade(page)
    expect(dialog.get_by_text("Step 1 of 2: your details")).to_be_visible()
    for label in ("Full name", "Work email", "Company", "Phone"):
        expect(dialog.get_by_label(re.compile(rf"^{label}"))).to_be_visible()
    expect(dialog.get_by_label(re.compile(r"^Full name"))).to_be_focused()
    assert not backend.calls_to("POST", r"^/pricing/(leads|checkout)$"), "Checkout started before lead capture"
    fill_lead(dialog)
    dialog.get_by_role("button", name="Continue to checkout").click()
    expect_on_stripe(page)
    lead = backend.calls_to("POST", r"^/pricing/leads$")
    checkout = backend.calls_to("POST", r"^/pricing/checkout$")
    assert len(lead) == 1 and lead[0][2] == {
        "name": "Priya Raman", "email": "priya@example.com", "company": "Example Corp",
        "phone": "+91 98765 43210", "plan": "mid"}, lead
    assert len(checkout) == 1 and checkout[0][2] == {"lead_id": "lead-1", "plan": "mid"}, checkout


def test_us_6_lead_form_validates_every_field(page: Page, backend: FakeBackend):
    goto(page, "/pricing")
    dialog = open_upgrade(page)
    dialog.get_by_role("button", name="Continue to checkout").click()
    for msg in ("Please enter your name.", "Please enter your email.", "Please enter your company.",
                "Please enter a phone number."):
        expect(dialog.get_by_text(msg)).to_be_visible()
    fill_lead(dialog, email="not-an-email", phone="abc")
    dialog.get_by_role("button", name="Continue to checkout").click()
    expect(dialog.get_by_text("Enter a valid email address, like you@company.com.")).to_be_visible()
    expect(dialog.get_by_text(re.compile(r"Use 7.20 digits"))).to_be_visible()
    fill_lead(dialog, name="   ", email="a@b.co", phone="12345")
    dialog.get_by_role("button", name="Continue to checkout").click()
    expect(dialog.get_by_text("Please enter your name.")).to_be_visible()
    assert not backend.calls_to("POST", r"^/pricing/"), "Invalid lead details were submitted"
    expect(page).to_have_url(re.compile(r"/pricing"))


def test_us_6_lead_save_error_keeps_user_on_form(page: Page, backend: FakeBackend):
    backend.lead_error = (500, {"detail": "Could not save lead right now."})
    goto(page, "/pricing")
    dialog = open_upgrade(page)
    fill_lead(dialog)
    dialog.get_by_role("button", name="Continue to checkout").click()
    expect(dialog.get_by_text("Could not save lead right now.")).to_be_visible()
    expect(dialog.get_by_label(re.compile(r"^Work email"))).to_have_value("priya@example.com")
    assert not backend.calls_to("POST", r"^/pricing/checkout$"), "Checkout started without a saved lead"


def test_us_6_closing_form_cancels_upgrade(page: Page, backend: FakeBackend):
    goto(page, "/pricing")
    dialog = open_upgrade(page)
    dialog.get_by_role("button", name="Close").click()
    expect(dialog).to_be_hidden()
    dialog = open_upgrade(page, "Global")
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    assert not backend.calls_to("POST", r"^/pricing/")


def test_us_6_pricing_has_no_free_plan_card(page: Page, backend: FakeBackend):
    goto(page, "/pricing")
    expect(page.get_by_role("heading", name="Mid", exact=True, level=2)).to_be_visible()
    expect(page.get_by_role("heading", name="Free", exact=True, level=2)).to_have_count(0)
    expect(page.get_by_role("link", name="Start free")).to_have_count(0)
    expect(page.get_by_role("button", name=re.compile(r"^Upgrade to"))).to_have_count(3)


def test_us_6_submitting_state_disables_form(page: Page, backend: FakeBackend):
    backend.hold("POST", r"^/pricing/leads$")
    goto(page, "/pricing")
    dialog = open_upgrade(page)
    fill_lead(dialog)
    dialog.get_by_role("button", name="Continue to checkout").click()
    expect(dialog.get_by_role("button", name="Submitting…")).to_be_disabled()
    expect(dialog.get_by_label(re.compile(r"^Full name"))).to_be_disabled()
    expect(dialog.get_by_role("button", name="Close")).to_be_disabled()
    backend.wait_for_held()
    backend.release()
    expect_on_stripe(page)


def test_us_6_live_lead_capture_reaches_backend(page: Page):
    base = live_backend(page)
    stub_external_documents(page, {urlparse(BASE_URL).hostname, urlparse(base).hostname})
    goto(page, "/pricing")
    dialog = open_upgrade(page)
    fill_lead(dialog, email=unique_email("lead"))
    with page.expect_response(lambda r: r.url.endswith("/pricing/leads") and r.request.method == "POST") as info:
        dialog.get_by_role("button", name="Continue to checkout").click()
    assert info.value.status in (200, 201), f"Lead capture -> HTTP {info.value.status}"
    expect(page.get_by_text("External page stub").or_(page.get_by_text(SAVED_BUT_FAILED_RE))).to_be_visible(timeout=20000)


# ===========================================================================
# US-7: Live plan purchase
# ===========================================================================


def test_us_7_pricing_shows_paid_plans_and_hosted_secure_payment(page: Page, backend: FakeBackend):
    goto(page, "/pricing")
    for plan in ("Mid", "Large", "Global"):
        expect(page.get_by_role("heading", name=plan, exact=True, level=2)).to_be_visible()
    for seats in ("50 seats, shared platform", "250 seats, dedicated", "1,000 seats, HA + DR"):
        expect(page.get_by_text(seats, exact=True)).to_be_visible()
    for monthly, annual in (("$7,500", "$90,000"), ("$22,500", "$270,000"), ("$60,000", "$720,000")):
        expect(page.get_by_text(re.compile(rf"^{re.escape(monthly)}\s*per month$"))).to_be_visible()
        expect(page.get_by_text(f"or {annual} per year", exact=True)).to_be_visible()
    expect(page.get_by_text("+ one-time payment of $5,000", exact=True)).to_have_count(3)
    expect(page.get_by_text(re.compile(r"processed live by Stripe on a secure hosted checkout page"))).to_be_visible()
    expect(page.locator("input[autocomplete^='cc-'], input[name*='card' i]")).to_have_count(0)


def test_us_7_signed_in_purchase_goes_to_stripe_with_session(page: Page, backend: FakeBackend):
    sign_in(page)
    goto(page, "/pricing")
    dialog = open_upgrade(page, "Global")
    fill_lead(dialog)
    dialog.get_by_role("button", name="Continue to checkout").click()
    expect_on_stripe(page)
    checkout = backend.calls_to("POST", r"^/pricing/checkout$")[-1]
    assert checkout[2]["plan"] == "global"
    assert checkout[3].get("authorization") == f"Bearer {TEST_TOKEN}", "Checkout not tied to the signed-in account"


def test_us_7_checkout_failure_keeps_lead_and_retry_succeeds(page: Page, backend: FakeBackend):
    backend.checkout_fail_times = 1
    goto(page, "/pricing")
    dialog = open_upgrade(page)
    fill_lead(dialog)
    dialog.get_by_role("button", name="Continue to checkout").click()
    expect(dialog.get_by_text(SAVED_BUT_FAILED_RE)).to_be_visible()
    expect(dialog.get_by_text(re.compile(r"Payment gateway unavailable"))).to_be_visible()
    expect(dialog.get_by_role("button", name="Or sign in to resume checkout later")).to_be_visible()
    dialog.get_by_role("button", name="Continue to checkout").click()
    expect_on_stripe(page)
    assert len(backend.calls_to("POST", r"^/pricing/leads$")) == 1, "Retry recorded a duplicate lead"
    assert len(backend.calls_to("POST", r"^/pricing/checkout$")) == 2


def test_us_7_checkout_resumes_after_sign_in(page: Page, backend: FakeBackend):
    backend.checkout_fail_times = 1
    goto(page, "/pricing")
    dialog = open_upgrade(page)
    fill_lead(dialog)
    dialog.get_by_role("button", name="Continue to checkout").click()
    expect(dialog.get_by_text(SAVED_BUT_FAILED_RE)).to_be_visible()
    goto(page, f"/crown-ai/callback?token={TEST_TOKEN}")
    expect_on_stripe(page)
    last = backend.calls_to("POST", r"^/pricing/checkout$")[-1]
    assert last[2] == {"lead_id": "lead-1", "plan": "mid"}, last
    assert last[3].get("authorization") == f"Bearer {TEST_TOKEN}"


def test_us_7_return_from_checkout_messages(page: Page, backend: FakeBackend):
    goto(page, "/pricing?checkout=success&session_id=cs_test_1")
    expect(page.get_by_text(re.compile(r"Payment confirmed\. Thank you! Your Crown AI Mid plan is active"))).to_be_visible()
    page.get_by_role("link", name="Open your workspace").click()
    expect(page).to_have_url(re.compile(r"/crown-ai/?$"))

    backend.checkout_status = "pending"
    goto(page, "/pricing?checkout=success&session_id=cs_test_2")
    expect(page.get_by_text(re.compile(r"still processing"))).to_be_visible()
    backend.checkout_status = "succeeded"
    page.get_by_role("button", name="Check again").click()
    expect(page.get_by_text(re.compile(r"Payment confirmed"))).to_be_visible()

    goto(page, "/pricing?checkout=cancelled")
    expect(page.get_by_text(re.compile(r"Checkout was cancelled and you have not been charged"))).to_be_visible()

    goto(page, "/pricing?error=Card%20declined")
    expect(page.get_by_text("We couldn't complete checkout: Card declined. Please try again.")).to_be_visible()


def test_us_7_checkout_error_param_is_rendered_as_text(page: Page, backend: FakeBackend):
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    goto(page, "/pricing?error=%3Cimg%20src%3Dx%20onerror%3Dalert(1)%3E")
    expect(page.get_by_text(re.compile(r"<img src=x onerror=alert\(1\)>"))).to_be_visible()
    page.wait_for_timeout(500)
    assert not dialogs, "Checkout error parameter executed as HTML"


def test_us_7_upgraded_account_can_download(page: Page, backend: FakeBackend):
    backend.tier = "mid"
    backend.seed_project("Paid App", stages=["requirements", "design", "code"])
    sign_in(page)
    expect(page.get_by_text("♛ Mid plan")).to_be_visible()
    expect(page.get_by_role("link", name="Upgrade", exact=True)).to_have_count(0)
    download_button = page.get_by_role("button", name="Download Paid App")
    expect(download_button).to_have_text("Download")
    with page.expect_download() as info:
        download_button.click()
    assert info.value.suggested_filename == "crownai_export.zip"
    expect(page).to_have_url(re.compile(r"/crown-ai/?$"))


def test_us_7_upgrade_still_possible_when_pricing_api_down(page: Page, backend: FakeBackend):
    backend.plans_abort = True
    goto(page, "/pricing")
    expect(page.get_by_text("Live pricing unavailable")).to_be_visible(timeout=20000)
    dialog = open_upgrade(page)
    fill_lead(dialog)
    dialog.get_by_role("button", name="Continue to checkout").click()
    expect_on_stripe(page)


def test_us_7_live_pricing_plans_load_from_backend(page: Page):
    live_backend(page)
    with page.expect_response(lambda r: r.url.endswith("/pricing/plans") and r.request.method == "GET") as info:
        goto(page, "/pricing")
    assert info.value.status == 200
    plans = info.value.json()
    assert any(p.get("amount_cents", 0) > 0 for p in plans), "No paid plan offered by the backend"
    expect(page.get_by_role("button", name=re.compile(r"^Upgrade to")).first).to_be_enabled()
    expect(page.get_by_text("Live pricing unavailable")).to_have_count(0)


# ===========================================================================
# US-8: Geo-based privacy compliance
# ===========================================================================


def consent_page(browser: Browser, locale: str, timezone_id: str, api: str = "down", seen=None):
    context = browser.new_context(locale=locale, timezone_id=timezone_id)
    context.set_default_timeout(ACTION_TIMEOUT_MS)

    def handler(route: Route):
        if seen is not None:
            seen.append(route.request.url)
        if api == "down":
            route.abort()
        else:
            route.fulfill(status=200, content_type="application/json",
                          headers={"access-control-allow-origin": "*"},
                          body=json.dumps({"country": "IN", "regime": "API_TEST", "regime_name": "Policy from server",
                                           "requires_opt_in": False, "banner_text": "Server banner.", "rights": []}))

    context.route(re.compile(r".*/consent/policy(\?.*)?$"), handler)
    page = context.new_page()
    page.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)
    return context, page


def test_us_8_india_visitor_gets_dpdp_banner(browser: Browser):
    context, page = consent_page(browser, "en-IN", "Asia/Kolkata")
    try:
        goto(page, "/")
        region = consent_region(page)
        expect(region).to_contain_text("Digital Personal Data Protection Act, 2023 (India)")
        expect(region).to_contain_text("DPDP Act")
        expect(region.get_by_role("button", name="Reject non-essential")).to_be_visible()
        region.get_by_role("link", name="Privacy Policy").click()
        expect(page).to_have_url(re.compile(r"/legal/privacy$"))
        expect(page.get_by_text(re.compile(r"Digital Personal Data Protection Act, 2023 for users in India"))).to_be_visible()
    finally:
        context.close()


def test_us_8_eu_visitor_gets_gdpr_opt_in_and_reject_is_remembered(browser: Browser):
    context, page = consent_page(browser, "de-DE", "Europe/Berlin")
    try:
        goto(page, "/")
        region = consent_region(page)
        expect(region).to_contain_text("General Data Protection Regulation (EU/EEA)")
        region.get_by_role("button", name="Reject non-essential").click()
        expect(region).to_be_hidden()
        assert page.evaluate("() => localStorage.getItem('crownai_consent_choice')") == "rejected"
        page.reload()
        page.wait_for_timeout(1500)
        expect(consent_region(page)).to_have_count(0)
    finally:
        context.close()


def test_us_8_us_visitor_gets_ccpa_notice_and_accept_persists(browser: Browser):
    context, page = consent_page(browser, "en-US", "America/New_York")
    try:
        goto(page, "/pricing")
        region = consent_region(page)
        expect(region).to_contain_text("California Consumer Privacy Act")
        expect(region.get_by_role("button", name="Reject non-essential")).to_have_count(0)
        region.get_by_role("button", name="Accept", exact=True).click()
        expect(region).to_be_hidden()
        goto(page, "/contact")
        page.wait_for_timeout(1500)
        expect(consent_region(page)).to_have_count(0)
    finally:
        context.close()


def test_us_8_other_region_gets_generic_notice(browser: Browser):
    context, page = consent_page(browser, "ja-JP", "Asia/Tokyo")
    try:
        goto(page, "/")
        expect(consent_region(page)).to_contain_text("General data protection best practices")
    finally:
        context.close()


def test_us_8_india_detected_from_time_zone_and_sent_to_api(browser: Browser):
    seen = []
    context, page = consent_page(browser, "en", "Asia/Kolkata", api="answer", seen=seen)
    try:
        goto(page, "/")
        expect(consent_region(page)).to_contain_text("Policy from server")  # API answer replaces local copy
        assert any("country=IN" in u for u in seen), f"Detected country not sent to the consent API: {seen}"
    finally:
        context.close()


def test_us_8_banner_shows_on_crown_ai_tool_too(browser: Browser):
    context, page = consent_page(browser, "fr-FR", "Europe/Paris")
    try:
        goto(page, "/crown-ai")
        expect(sign_in_heading(page)).to_be_visible()
        expect(consent_region(page)).to_contain_text("GDPR")
    finally:
        context.close()


# ===========================================================================
# US-9: Contact/inquiry form
# ===========================================================================


def test_us_9_contact_form_submits_inquiry(page: Page, backend: FakeBackend):
    goto(page, "/contact")
    expect(page.get_by_role("heading", level=1)).to_have_text(re.compile(r"Let.s talk"))
    expect(page.get_by_text(LEGAL_NAME).first).to_be_visible()
    backend.hold("POST", r"^/contact$")
    contact_field(page, "Name").fill("Arun Kumar")
    contact_field(page, "Email").fill("arun@example.com")
    contact_field(page, "Message").fill("We'd like a quote for a QA automation project.")
    page.get_by_role("button", name="Send message").click()
    expect(page.get_by_role("button", name="Sending…")).to_be_disabled()
    backend.wait_for_held()
    backend.release()
    expect(page.get_by_text("Thank you!")).to_be_visible()
    expect(page.get_by_text(re.compile(r"Your inquiry has been received"))).to_be_visible()
    sent = backend.calls_to("POST", r"^/contact$")
    assert sent[0][2] == {"name": "Arun Kumar", "email": "arun@example.com",
                          "message": "We'd like a quote for a QA automation project."}, sent
    page.get_by_role("button", name="Send another message").click()
    expect(contact_field(page, "Name")).to_have_value("")


def test_us_9_contact_form_validation(page: Page, backend: FakeBackend):
    goto(page, "/contact")
    page.get_by_role("button", name="Send message").click()
    expect(page.get_by_text("Please enter your name.")).to_be_visible()
    expect(page.get_by_text("Please enter your email.")).to_be_visible()
    expect(page.get_by_text("Please tell us how we can help.")).to_be_visible()
    expect(contact_field(page, "Name")).to_be_focused()
    contact_field(page, "Name").fill("Arun")
    expect(page.get_by_text("Please enter your name.")).to_have_count(0)
    contact_field(page, "Email").fill("arun@nowhere")
    contact_field(page, "Message").fill("   ")
    page.get_by_role("button", name="Send message").click()
    expect(page.get_by_text("Enter a valid email address, like you@company.com.")).to_be_visible()
    expect(page.get_by_text("Please tell us how we can help.")).to_be_visible()
    assert not backend.calls_to("POST", r"^/contact$"), "Invalid inquiry was sent"


def test_us_9_contact_server_errors_keep_message(page: Page, backend: FakeBackend):
    backend.contact_error = (500, {"detail": "Inbox temporarily unavailable."})
    goto(page, "/contact")
    contact_field(page, "Name").fill("Arun")
    contact_field(page, "Email").fill("arun@example.com")
    contact_field(page, "Message").fill("Please call me back.")
    page.get_by_role("button", name="Send message").click()
    expect(page.get_by_text("Inbox temporarily unavailable.")).to_be_visible()
    expect(contact_field(page, "Message")).to_have_value("Please call me back.")
    expect(page.get_by_text("Thank you!")).to_have_count(0)
    backend.contact_error = (404, {"detail": "Not Found"})
    page.get_by_role("button", name="Send message").click()
    expect(page.get_by_text(re.compile(r"contact service is unavailable right now"))).to_be_visible()
    backend.contact_error = None
    page.get_by_role("button", name="Send message").click()
    expect(page.get_by_text("Thank you!")).to_be_visible()


def test_us_9_map_failure_shows_error_not_blank(page: Page, backend: FakeBackend):
    goto(page, "/contact")
    expect(page.get_by_text("Map unavailable")).to_be_visible()
    expect(page.get_by_role("button", name="Try again")).to_be_visible()
    expect(page.get_by_text("Registered office")).to_be_visible()
    expect(page.get_by_text(ADDRESS_RE).first).to_be_visible()


def test_us_9_live_contact_submission_is_captured(page: Page):
    live_backend(page)
    goto(page, "/contact")
    contact_field(page, "Name").fill("ASR Live QA")
    contact_field(page, "Email").fill(unique_email("contact"))
    contact_field(page, "Message").fill(f"Automated UI check {uuid.uuid4().hex[:8]}")
    with page.expect_response(lambda r: r.url.endswith("/contact") and r.request.method == "POST") as info:
        page.get_by_role("button", name="Send message").click()
    assert info.value.status in (200, 201), f"POST /contact -> HTTP {info.value.status}"
    expect(page.get_by_text("Thank you!")).to_be_visible(timeout=20000)


# ===========================================================================
# US-10: User deletes their own project data
# ===========================================================================


def test_us_10_delete_project_with_confirmation(page: Page, backend: FakeBackend):
    backend.seed_project("Keep Me", stages=["requirements"])
    doomed = backend.seed_project("Delete Me", stages=["requirements", "design"])
    sign_in(page)
    page.get_by_role("button", name="Delete Delete Me", exact=True).click()
    dialog = page.get_by_role("alertdialog", name="Delete “Delete Me”?")
    expect(dialog).to_contain_text("permanently removes the project and all generated artifacts")
    dialog.get_by_role("button", name="Cancel").click()
    expect(dialog).to_be_hidden()
    assert not backend.calls_to("DELETE", r"/projects/")
    page.get_by_role("button", name="Delete Delete Me", exact=True).click()
    page.get_by_role("alertdialog").get_by_role("button", name="Delete permanently").click()
    expect(page.get_by_text("Project deleted. All of its generated artifacts were removed too.")).to_be_visible()
    expect(page.get_by_role("link", name="Delete Me", exact=True)).to_have_count(0)
    expect(page.get_by_role("link", name="Keep Me", exact=True)).to_be_visible()
    assert backend.calls_to("DELETE", rf"^/projects/{doomed}$")
    page.reload()
    expect(page.get_by_role("link", name="Keep Me", exact=True)).to_be_visible()
    expect(page.get_by_role("link", name="Delete Me", exact=True)).to_have_count(0)


def test_us_10_deleting_last_project_shows_empty_state(page: Page, backend: FakeBackend):
    backend.seed_project("Only One")
    sign_in(page)
    page.get_by_role("button", name="Delete Only One", exact=True).click()
    page.get_by_role("alertdialog").get_by_role("button", name="Delete permanently").click()
    expect(page.get_by_text("No projects yet")).to_be_visible()
    expect(page.get_by_role("button", name="Create a project")).to_be_visible()


def test_us_10_delete_failure_keeps_project(page: Page, backend: FakeBackend):
    backend.delete_error = True
    backend.seed_project("Sticky")
    sign_in(page)
    page.get_by_role("button", name="Delete Sticky", exact=True).click()
    page.get_by_role("alertdialog").get_by_role("button", name="Delete permanently").click()
    expect(page.get_by_test_id("workspace-error")).to_have_text("Could not delete this project. Please try again.")
    expect(page.get_by_role("link", name="Sticky", exact=True)).to_be_visible()


def test_us_10_deleted_project_url_is_not_found(page: Page, backend: FakeBackend):
    pid = backend.seed_project("Gone Soon", stages=["requirements"])
    sign_in(page)
    page.get_by_role("button", name="Delete Gone Soon", exact=True).click()
    page.get_by_role("alertdialog").get_by_role("button", name="Delete permanently").click()
    expect(page.get_by_text("No projects yet")).to_be_visible()
    goto(page, f"/crown-ai/projects/{pid}")
    expect(page.get_by_text("Project not found")).to_be_visible()


def test_us_10_delete_account_erases_everything(page: Page, backend: FakeBackend):
    backend.seed_project("Account Data")
    sign_in(page)
    page.get_by_role("button", name="Delete my account").click()
    dialog = page.get_by_role("alertdialog", name="Delete your account and data?")
    expect(dialog).to_contain_text("permanently erases")
    dialog.get_by_role("button", name="Delete everything").click()
    expect(page).to_have_url(re.compile(r"/$"))
    assert backend.calls_to("DELETE", r"^/auth/me$")
    goto(page, "/crown-ai")
    expect(sign_in_heading(page)).to_be_visible()


# ===========================================================================
# US-11: Blog and Careers sections
# ===========================================================================


def test_us_11_blog_lists_published_articles(page: Page, backend: FakeBackend):
    goto(page, "/blog")
    articles = page.get_by_role("article")
    expect(articles).to_have_count(3)
    for i in range(3):
        expect(articles.nth(i).get_by_role("heading", level=2)).to_be_visible()
        expect(articles.nth(i).locator("time")).to_have_attribute("datetime", re.compile(r"^\d{4}-\d{2}-\d{2}$"))
        expect(articles.nth(i).get_by_text(re.compile(r"\d+ min read"))).to_be_visible()
    expect(page.get_by_role("heading", name="Why we automated the STLC, not just the SDLC")).to_be_visible()


def test_us_11_careers_lists_open_jobs_with_apply(page: Page, backend: FakeBackend):
    goto(page, "/careers")
    expect(page.get_by_role("heading", name="Open positions (5)")).to_be_visible()
    expect(page.get_by_role("table").get_by_role("row")).to_have_count(6)  # header + 5 roles
    apply = page.get_by_role("link", name="Apply for Senior Full-Stack Engineer")
    expect(apply).to_be_visible()
    apply.click()
    expect(page).to_have_url(re.compile(r"/contact$"))


def test_us_11_blog_and_careers_on_mobile(page: Page, backend: FakeBackend):
    page.set_viewport_size({"width": 375, "height": 812})
    goto(page, "/careers")
    expect(page.get_by_text("Chennai / Remote · Full-time").first).to_be_visible()
    expect(page.get_by_role("link", name="Apply for Product Designer")).to_be_visible()
    goto(page, "/blog")
    expect(page.get_by_role("article").first).to_be_visible()


# ===========================================================================
# US-12: Case studies/portfolio showcase
# ===========================================================================


def test_us_12_case_studies_list_featured_clients(page: Page, backend: FakeBackend):
    goto(page, "/case-studies")
    expect(page.get_by_role("article")).to_have_count(3)
    for client in CASE_STUDIES.values():
        expect(page.get_by_role("heading", name=client, exact=True)).to_be_visible()


def test_us_12_case_study_detail_shows_story_and_sample_output(page: Page, backend: FakeBackend):
    for slug, client in CASE_STUDIES.items():
        goto(page, "/case-studies")
        page.get_by_role("link", name=re.compile(rf"View case study: {re.escape(client)}")).click()
        expect(page).to_have_url(re.compile(rf"/case-studies/{slug}$"))
        expect(page.get_by_role("heading", level=1)).to_have_text(client)
        for section in ("The challenge", "How Crown AI helped", "Sample Crown AI output", "Results"):
            expect(page.get_by_role("heading", name=section, exact=True)).to_be_visible()
        expect(page.locator("pre")).not_to_be_empty()
    page.get_by_role("link", name="← Back to case studies").click()
    expect(page).to_have_url(re.compile(r"/case-studies$"))


def test_us_12_unknown_case_study_is_not_found(page: Page, backend: FakeBackend):
    response = goto(page, "/case-studies/does-not-exist")
    assert response is not None and response.status == 404, f"-> {response.status if response else None}"
    expect(page.get_by_role("heading", level=1)).to_have_text(re.compile(r"couldn.t find that page"))


def test_us_12_case_study_on_mobile(page: Page, backend: FakeBackend):
    page.set_viewport_size({"width": 375, "height": 812})
    goto(page, "/case-studies/eu-retail-gdpr")
    expect(page.get_by_role("heading", level=1)).to_have_text("European retail client (GDPR scope)")
    page.get_by_role("link", name="See how Crown AI works").click()
    expect(page).to_have_url(re.compile(r"/services/crown-ai$"))


# ===========================================================================
# Regression coverage for the Testing defects
# ===========================================================================

# -- Defect 1: blur-triggered error shifted the submit button mid-click -----


def open_create_form(page: Page):
    page.get_by_role("button", name="+ New Project").click()
    name = page.get_by_label("Project name")
    reqs = page.get_by_label(re.compile(r"^Requirements"))
    submit = page.get_by_role("button", name="Generate Project")
    expect(name).to_be_focused()  # autoFocus: the case that used to break the first click
    return name, reqs, submit


def _box(locator):
    box = locator.bounding_box()
    assert box is not None, "Element is not rendered"
    return box


def test_us_4_slow_mouse_click_on_submit_from_autofocused_name_submits(page: Page, backend: FakeBackend):
    sign_in(page)
    name, reqs, submit = open_create_form(page)
    before = _box(submit)
    page.mouse.move(before["x"] + before["width"] / 2, before["y"] + before["height"] / 2)
    page.mouse.down()  # blurs the name field
    page.wait_for_timeout(250)
    after_down = _box(submit)
    assert (after_down["x"], after_down["y"]) == (before["x"], before["y"]), (
        f"Submit button moved between mousedown and mouseup: {before} -> {after_down}")
    expect(page.get_by_text("Give your project a name.")).to_have_count(0)
    page.mouse.up()
    expect(page.get_by_text("Give your project a name.")).to_be_visible()
    expect(page.get_by_text(re.compile(r"Describe what you want to build"))).to_be_visible()
    expect(name).to_have_attribute("aria-invalid", "true")
    expect(reqs).to_have_attribute("aria-invalid", "true")
    expect(name).to_be_focused()
    assert not backend.calls_to("POST", r"^/projects$"), "Empty form was sent to the API"


def test_us_4_blurring_untouched_fields_shows_no_errors(page: Page, backend: FakeBackend):
    sign_in(page)
    name, reqs, submit = open_create_form(page)
    before = _box(submit)
    reqs.focus()
    page.get_by_role("heading", name="New project").click()
    expect(page.get_by_text("Give your project a name.")).to_have_count(0)
    expect(page.get_by_text(re.compile(r"Describe what you want to build"))).to_have_count(0)
    expect(name).not_to_have_attribute("aria-invalid", "true")
    after = _box(submit)
    assert after["y"] == before["y"], f"Submit button moved after a plain blur: {before} -> {after}"


def test_us_4_editing_then_clearing_a_field_shows_inline_error(page: Page, backend: FakeBackend):
    sign_in(page)
    name, reqs, _ = open_create_form(page)
    name.fill("Draft")
    expect(page.get_by_text("Give your project a name.")).to_have_count(0)
    name.fill("")
    expect(page.get_by_text("Give your project a name.")).to_be_visible()
    expect(name).to_have_attribute("aria-describedby", "proj-name-error")
    name.fill("Back again")
    expect(page.get_by_text("Give your project a name.")).to_have_count(0)
    reqs.fill("x")
    reqs.fill("   ")
    expect(page.get_by_text(re.compile(r"Describe what you want to build"))).to_be_visible()
    expect(reqs).to_have_attribute("aria-describedby", re.compile(r"proj-reqs-error"))


def test_us_4_missing_requirements_only_focuses_requirements(page: Page, backend: FakeBackend):
    sign_in(page)
    name, reqs, submit = open_create_form(page)
    name.fill("Named App")
    submit.click()
    expect(page.get_by_text(re.compile(r"Describe what you want to build"))).to_be_visible()
    expect(page.get_by_text("Give your project a name.")).to_have_count(0)
    expect(reqs).to_be_focused()
    reqs.fill("   \n  ")
    submit.click()
    expect(page.get_by_text(re.compile(r"Describe what you want to build"))).to_be_visible()
    assert not backend.calls_to("POST", r"^/projects$"), "Whitespace-only requirements were sent"


def test_us_4_first_click_after_typing_submits_and_trims_input(page: Page, backend: FakeBackend):
    sign_in(page)
    name, reqs, submit = open_create_form(page)
    name.fill("  Trimmed App  ")
    reqs.fill("  Build a parcel tracker.  ")
    name.focus()  # submit straight from the focused name field, as a user would
    submit.click()
    expect(page).to_have_url(re.compile(r"/crown-ai/projects/[^/?]+"))
    created = backend.calls_to("POST", r"^/projects$")
    assert len(created) == 1, created
    assert created[0][2] == {"name": "Trimmed App", "requirements": "Build a parcel tracker."}, created[0][2]


def test_us_4_enter_key_submits_create_form(page: Page, backend: FakeBackend):
    sign_in(page)
    name, reqs, _ = open_create_form(page)
    reqs.fill("Keyboard-only spec.")
    name.fill("Keyboard App")
    name.press("Enter")
    expect(page).to_have_url(re.compile(r"/crown-ai/projects/[^/?]+"))
    expect(page.get_by_role("heading", level=1)).to_have_text("Keyboard App")


def test_us_4_create_form_on_mobile_submits_with_tap_target_stable(page: Page, backend: FakeBackend):
    page.set_viewport_size({"width": 375, "height": 812})
    sign_in(page)
    _, _, submit = open_create_form(page)
    submit.scroll_into_view_if_needed()
    submit.click()
    expect(page.get_by_text("Give your project a name.")).to_be_visible()
    expect(page.get_by_text(re.compile(r"Describe what you want to build"))).to_be_visible()


# -- Defect 2: case-study link accessible name had a stray space ------------


def test_us_12_case_study_links_have_exact_accessible_names(page: Page, backend: FakeBackend):
    goto(page, "/case-studies")
    for slug, client in CASE_STUDIES.items():
        link = page.get_by_role("link", name=f"View case study: {client}", exact=True)
        expect(link).to_have_count(1)
        expect(link).to_be_visible()
        expect(link).to_have_text(re.compile(r"^\s*View case study\s*→\s*$"))
        expect(link).to_have_attribute("href", re.compile(rf"/case-studies/{slug}$"))
    expect(page.get_by_role("link", name=re.compile(r"View case study\s+:"))).to_have_count(0)
    expect(page.get_by_role("link", name=re.compile(r"^View case study"))).to_have_count(len(CASE_STUDIES))


def test_us_12_case_study_link_works_with_keyboard(page: Page, backend: FakeBackend):
    goto(page, "/case-studies")
    client = CASE_STUDIES["saas-logistics-bengaluru"]
    link = page.get_by_role("link", name=f"View case study: {client}", exact=True)
    link.focus()
    expect(link).to_be_focused()
    page.keyboard.press("Enter")
    expect(page).to_have_url(re.compile(r"/case-studies/saas-logistics-bengaluru$"))
    expect(page.get_by_role("heading", level=1)).to_have_text(client)


def test_us_12_case_study_links_named_on_mobile(page: Page, backend: FakeBackend):
    page.set_viewport_size({"width": 375, "height": 812})
    goto(page, "/case-studies")
    client = CASE_STUDIES["eu-retail-gdpr"]
    page.get_by_role("link", name=f"View case study: {client}", exact=True).click()
    expect(page).to_have_url(re.compile(r"/case-studies/eu-retail-gdpr$"))


# -- Defect 3: Tailwind v4 must compile through @tailwindcss/postcss --------


def test_us_2_tailwind_utilities_and_design_system_compile(page: Page, backend: FakeBackend):
    page.set_viewport_size({"width": 1440, "height": 900})
    goto(page, "/case-studies", wait_until="load")
    styles = page.evaluate(
        """() => {
            const cs = (el) => el ? getComputedStyle(el) : null;
            const grid = document.querySelector('main .grid');
            const card = document.querySelector('main article.card');
            const container = document.querySelector('.container-page');
            const raw = [...document.styleSheets].flatMap((s) => {
                try { return [...s.cssRules].map((r) => r.cssText); } catch (e) { return []; }
            });
            return {
                gridDisplay: cs(grid)?.display,
                gridCols: cs(grid)?.gridTemplateColumns.split(' ').filter(Boolean).length,
                cardRadius: parseFloat(cs(card)?.borderTopLeftRadius || '0'),
                cardBorder: parseFloat(cs(card)?.borderTopWidth || '0'),
                containerMax: cs(container)?.maxWidth,
                ruleCount: raw.length,
                rawDirectives: raw.filter((t) => /@tailwind|@config|@import\\s+["']tailwindcss/.test(t)).length,
            };
        }"""
    )
    assert styles["ruleCount"] > 50, f"Stylesheet looks empty -- Tailwind did not compile: {styles}"
    assert styles["rawDirectives"] == 0, f"Unprocessed Tailwind directives reached the browser: {styles}"
    assert styles["gridDisplay"] == "grid", f"Tailwind 'grid' utility not applied: {styles}"
    assert styles["gridCols"] == 3, f"Responsive 'lg:grid-cols-3' variant not applied at 1440px: {styles}"
    assert styles["cardRadius"] > 0 and styles["cardBorder"] > 0, f"'.card' component styles missing: {styles}"
    assert styles["containerMax"] == "1152px", f"'.container-page' max-width (72rem) missing: {styles}"


def test_us_2_tailwind_responsive_variants_collapse_on_mobile(page: Page, backend: FakeBackend):
    page.set_viewport_size({"width": 375, "height": 812})
    goto(page, "/case-studies", wait_until="load")
    cols = page.evaluate(
        "() => getComputedStyle(document.querySelector('main .grid')).gridTemplateColumns.split(' ').filter(Boolean).length"
    )
    assert cols == 1, f"Case-study grid should be a single column on a phone, got {cols}"


def test_us_2_tailwind_button_and_form_styles_in_workspace(page: Page, backend: FakeBackend):
    sign_in(page)
    open_create_form(page)
    styles = page.evaluate(
        """() => {
            const btn = getComputedStyle([...document.querySelectorAll('button')]
                .find((b) => b.textContent.includes('Generate Project')));
            const input = getComputedStyle(document.getElementById('proj-name'));
            return { btnBg: btn.backgroundImage, btnRadius: parseFloat(btn.borderTopLeftRadius),
                     inputBorder: parseFloat(input.borderTopWidth), inputWidth: input.width };
        }"""
    )
    assert "gradient" in styles["btnBg"], f"'.btn-primary' gold gradient not compiled: {styles}"
    assert styles["btnRadius"] > 0 and styles["inputBorder"] > 0, f"Form component styles missing: {styles}"


# -- Defects 4/5: the app must start and serve its first page in time ------


def test_us_2_suite_first_request_to_home_finishes_compiling(page: Page, backend: FakeBackend):
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    response = page.goto(BASE_URL + "/", wait_until="load", timeout=90000)
    assert response is not None and response.status == 200, f"/ -> {response.status if response else None}"
    expect(page.get_by_role("heading", level=1)).to_contain_text(PAGES["/"])
    expect(page.get_by_text(re.compile(r"Compiling|Failed to compile|Module not found|Build Error"))).to_have_count(0)
    expect(page.locator("[data-nextjs-dialog], [data-nextjs-error-overlay]")).to_have_count(0)
    assert not errors, f"Runtime errors on first load: {errors}"


def test_us_2_suite_tool_routes_start_without_errors(page: Page, backend: FakeBackend):
    errors = []
    page.on("pageerror", lambda e: errors.append(f"{page.url}: {e}"))
    for path in ("/crown-ai", "/crown-ai/projects/startup-check", "/pricing", "/contact"):
        response = page.goto(BASE_URL + path, wait_until="load", timeout=90000)
        assert response is not None and response.status == 200, f"{path} -> {response.status if response else None}"
        expect(page.get_by_role("heading", level=1)).to_be_visible()
        expect(page.locator("[data-nextjs-dialog], [data-nextjs-error-overlay]")).to_have_count(0)
    assert not errors, "Runtime errors: " + "; ".join(errors)


# ===========================================================================
# Regression coverage: params refactor, shim removal, menu hydration, hover
# ===========================================================================

# -- Project detail page must use its route id for every request ------------


def project_paths(backend: FakeBackend):
    return [c[1] for c in backend.calls if c[1].startswith("/projects/")]


def assert_only_project(backend: FakeBackend, pid: str):
    paths = project_paths(backend)
    assert paths, "Project page never called the API"
    stray = [p for p in paths if not re.match(rf"^/projects/{re.escape(pid)}(/|$)", p)]
    assert not stray, f"Requests for project {pid} hit other ids: {stray}"
    assert not any(re.search(r"undefined|null|\[id\]|%5Bid%5D", p) for p in paths), paths


def test_us_4_project_detail_load_generate_download_use_route_id(page: Page, backend: FakeBackend):
    pid = backend.seed_project("Route Id App", stages=["requirements"])
    backend.seed_project("Other App", stages=["requirements", "design"])
    sign_in(page)
    goto(page, f"/crown-ai/projects/{pid}")
    expect(page.get_by_role("heading", level=1)).to_have_text("Route Id App")
    expect(page.get_by_text("Requirements Document generated for Route Id App")).to_be_visible()
    page.get_by_role("button", name="Generate Design", exact=True).click()
    expect(stage_done(page, "Design")).to_be_visible(timeout=10000)
    expect(page.get_by_text("System Design generated for Route Id App")).to_be_visible()
    page.get_by_role("button", name="Download artifacts").click()
    expect(page).to_have_url(re.compile(r"/pricing\?reason=download"))
    assert backend.calls_to("GET", rf"^/projects/{pid}$"), "Project not loaded by its route id"
    assert backend.calls_to("POST", rf"^/projects/{pid}/generate/design$")
    assert backend.calls_to("POST", rf"^/projects/{pid}/download$")
    assert_only_project(backend, pid)


def test_us_4_autogenerate_after_create_targets_new_project(page: Page, backend: FakeBackend):
    backend.seed_project("Existing", stages=["requirements"])
    sign_in(page)
    backend.calls.clear()
    create_project(page, "Fresh App", "A brand-new spec.")
    expect(page).to_have_url(re.compile(r"/crown-ai/projects/[^/?]+"))
    expect(progress(page)).to_contain_text("All stages complete (5/5)", timeout=30000)
    pid = urlparse(page.url).path.rsplit("/", 1)[-1]
    assert pid in backend.projects, f"URL id {pid!r} is not the created project"
    assert [c[1] for c in backend.calls_to("POST", r"/generate/")] == [
        f"/projects/{pid}/generate/{key}" for key, _, _ in STAGES]
    assert_only_project(backend, pid)
    assert len(backend.projects["proj-1"]["artifacts"]) == 1, "Generation leaked into another project"


def test_us_4_switching_between_projects_shows_each_projects_data(page: Page, backend: FakeBackend):
    a = backend.seed_project("Alpha App", stages=["requirements"])
    b = backend.seed_project("Beta App", stages=["requirements", "design"])
    sign_in(page)
    page.get_by_role("link", name="Alpha App", exact=True).click()
    expect(page).to_have_url(re.compile(rf"/crown-ai/projects/{a}$"))
    expect(page.get_by_role("heading", level=1)).to_have_text("Alpha App")
    expect(page.get_by_text("Requirements Document generated for Alpha App")).to_be_visible()
    page.get_by_role("link", name="← All projects").click()
    page.get_by_role("link", name="Beta App", exact=True).click()
    expect(page).to_have_url(re.compile(rf"/crown-ai/projects/{b}$"))
    expect(page.get_by_role("heading", level=1)).to_have_text("Beta App")
    expect(page.get_by_text("System Design generated for Beta App")).to_be_visible()
    expect(page.get_by_text(re.compile(r"generated for Alpha App"))).to_have_count(0)
    page.get_by_role("button", name="Generate Code", exact=True).click()
    expect(stage_done(page, "Code")).to_be_visible(timeout=10000)
    assert backend.generated_stages() == ["code"]
    assert backend.calls_to("POST", rf"^/projects/{b}/generate/code$"), "Generated against the previous project's id"
    assert [x["stage"] for x in backend.projects[a]["artifacts"]] == ["requirements"]
    page.go_back()
    page.go_back()
    expect(page).to_have_url(re.compile(rf"/crown-ai/projects/{a}$"))
    expect(page.get_by_role("heading", level=1)).to_have_text("Alpha App")
    expect(page.get_by_role("button", name="Generate Design", exact=True)).to_be_visible()


def test_us_4_project_load_error_retry_reuses_route_id(page: Page, backend: FakeBackend):
    pid = backend.seed_project("Flaky App", stages=["requirements"])
    sign_in(page)
    backend.detail_fail_times = 1
    goto(page, f"/crown-ai/projects/{pid}")
    expect(page.get_by_text("Couldn't load this project")).to_be_visible()
    page.get_by_role("button", name="Try again").click()
    expect(page.get_by_role("heading", level=1)).to_have_text("Flaky App")
    assert len(backend.calls_to("GET", rf"^/projects/{pid}$")) >= 2
    assert_only_project(backend, pid)


def test_us_7_paid_download_from_project_page_uses_route_id(page: Page, backend: FakeBackend):
    backend.tier = "mid"
    pid = backend.seed_project("Paid Detail", stages=["requirements", "design", "code"])
    sign_in(page)
    goto(page, f"/crown-ai/projects/{pid}")
    with page.expect_download() as info:
        page.get_by_role("button", name="Download artifacts").click()
    assert info.value.suggested_filename == "crownai_export.zip"
    assert backend.calls_to("POST", rf"^/projects/{pid}/download$")
    expect(page).to_have_url(re.compile(rf"/crown-ai/projects/{pid}$"))


def test_us_10_unknown_project_id_after_deep_link_is_not_found(page: Page, backend: FakeBackend):
    backend.seed_project("Real One")
    sign_in(page)
    goto(page, "/crown-ai/projects/does-not-exist-123")
    expect(page.get_by_text("Project not found")).to_be_visible()
    assert backend.calls_to("GET", r"^/projects/does-not-exist-123$"), project_paths(backend)


# -- Tailwind via @tailwindcss/postcss (shim removed) -----------------------


def test_us_2_tailwind_arbitrary_values_and_breakpoints_compile(page: Page, backend: FakeBackend):
    def header_height():
        return page.evaluate("() => document.querySelector('header > div').getBoundingClientRect().height")

    page.set_viewport_size({"width": 375, "height": 812})
    goto(page, "/", wait_until="load")
    assert header_height() == 64, f"'h-16' not applied on mobile: {header_height()}"
    page.set_viewport_size({"width": 1024, "height": 800})
    page.wait_for_timeout(200)
    assert header_height() == 72, f"Arbitrary value 'md:h-[4.5rem]' not compiled: {header_height()}"
    toggle = page.get_by_role("button", name="Open menu")
    expect(toggle).to_be_visible()
    page.set_viewport_size({"width": 1280, "height": 800})
    expect(toggle).to_be_hidden()  # 'xl:hidden'
    expect(banner(page).get_by_role("navigation", name="Main")).to_be_visible()  # 'xl:flex'


def test_us_2_stylesheets_are_served_and_compiled(page: Page, backend: FakeBackend):
    failed = []
    page.on("response", lambda r: failed.append(f"{r.url} -> {r.status}")
            if r.request.resource_type == "stylesheet" and r.status >= 400 else None)
    goto(page, "/pricing", wait_until="load")
    sheets = page.evaluate("() => document.styleSheets.length")
    assert sheets > 0, "No stylesheets loaded"
    assert not failed, f"Stylesheets failed to load: {failed}"
    bg = page.get_by_role("button", name="Upgrade to Mid").evaluate("el => getComputedStyle(el).backgroundImage")
    assert "gradient" in bg, f"'.btn-primary' styles missing on pricing: {bg}"


# -- Mobile menu must not swallow taps before hydration ---------------------


def test_us_2_mobile_menu_toggle_disabled_until_hydrated(page: Page, backend: FakeBackend):
    html = page.request.get(BASE_URL + "/").text()
    tag = re.search(r"<button[^>]*aria-label=\"Open menu\"[^>]*>", html)
    assert tag, "Server HTML has no 'Open menu' toggle"
    assert re.search(r"\sdisabled(=|\s|>)", tag.group(0)), f"SSR toggle is clickable before hydration: {tag.group(0)}"

    page.set_viewport_size({"width": 375, "height": 812})
    held, state = [], {"released": False}

    def hold_js(route: Route):
        if state["released"]:
            route.fallback()
        else:
            held.append(route)

    page.route(re.compile(r"/_next/static/.*\.js(\?.*)?$"), hold_js)
    goto(page, "/", wait_until="commit")
    toggle = page.get_by_role("button", name="Open menu")
    expect(toggle).to_be_visible()
    expect(toggle).to_be_disabled()
    expect(toggle).to_have_attribute("aria-expanded", "false")
    toggle.click(force=True)  # an impatient early tap
    expect(page.get_by_role("button", name="Close menu")).to_have_count(0)

    state["released"] = True
    for route in held:
        route.fallback()
    expect(toggle).to_be_enabled()
    toggle.click()
    expect(page.get_by_role("button", name="Close menu")).to_have_attribute("aria-expanded", "true")
    expect(page.get_by_role("navigation", name="Main")).to_be_visible()


def test_us_2_mobile_menu_opens_on_first_tap_right_after_load(page: Page, backend: FakeBackend):
    page.set_viewport_size({"width": 375, "height": 812})
    for path in ("/", "/pricing", "/contact", "/crown-ai", "/case-studies/eu-retail-gdpr"):
        goto(page, path)  # domcontentloaded only -- tap as soon as the button shows
        page.get_by_role("button", name="Open menu").click()
        close = page.get_by_role("button", name="Close menu")
        expect(close, f"Menu did not open on {path}").to_have_attribute("aria-expanded", "true")
        menu = page.get_by_role("navigation", name="Main")
        expect(menu.get_by_role("link")).to_have_count(10)  # 9 sections + Launch Crown AI


def test_us_2_mobile_menu_close_and_reopen(page: Page, backend: FakeBackend):
    page.set_viewport_size({"width": 375, "height": 812})
    goto(page, "/about")
    toggle = page.get_by_role("button", name="Open menu")
    expect(toggle).to_have_attribute("aria-controls", "mobile-nav")
    toggle.click()
    menu = page.get_by_role("navigation", name="Main")
    expect(menu).to_have_attribute("id", "mobile-nav")
    expect(menu.get_by_role("link", name="About Us", exact=True)).to_have_attribute("aria-current", "page")
    overflow = page.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
    assert overflow <= 1, f"Open menu scrolls sideways by {overflow}px"
    page.get_by_role("button", name="Close menu").click()
    expect(menu).to_have_count(0)
    expect(page.get_by_role("button", name="Open menu")).to_have_attribute("aria-expanded", "false")
    page.get_by_role("button", name="Open menu").click()
    expect(page.get_by_role("button", name="Close menu")).to_be_visible()
    expect(menu).to_be_visible()


def test_us_2_mobile_menu_every_link_navigates_and_closes(page: Page, backend: FakeBackend):
    page.set_viewport_size({"width": 375, "height": 812})
    goto(page, "/")
    page.get_by_role("button", name="Open menu").click()
    menu = page.get_by_role("navigation", name="Main")
    for label, path in NAV:
        menu.get_by_role("link", name=label, exact=True).click()
        expect(page).to_have_url(re.compile(re.escape(BASE_URL + path) + r"/?$"))
        expect(page.get_by_role("heading", level=1)).to_contain_text(PAGES[path])
        expect(page.get_by_role("button", name="Open menu")).to_be_visible()
        expect(menu).to_have_count(0)
        page.get_by_role("button", name="Open menu").click()
        expect(menu.get_by_role("link", name=label, exact=True)).to_have_attribute("aria-current", "page")


def test_us_1_mobile_menu_launch_crown_ai_reaches_sign_in(page: Page, backend: FakeBackend):
    page.set_viewport_size({"width": 375, "height": 812})
    goto(page, "/services")
    expect(banner(page).get_by_role("link", name="Launch Crown AI")).to_be_hidden()  # only in the menu on phones
    page.get_by_role("button", name="Open menu").click()
    page.get_by_role("navigation", name="Main").get_by_role("link", name="Launch Crown AI").click()
    expect(page).to_have_url(re.compile(r"/crown-ai/?$"))
    expect(sign_in_heading(page)).to_be_visible()
    expect(page.get_by_role("button", name="Open menu")).to_be_visible()


def test_us_2_menu_toggle_by_breakpoint(page: Page, backend: FakeBackend):
    for width, toggle_shown in ((360, True), (768, True), (1279, True), (1280, False), (1440, False)):
        page.set_viewport_size({"width": width, "height": 900})
        goto(page, "/blog")
        toggle = page.get_by_role("button", name="Open menu")
        if toggle_shown:
            toggle.click()
            expect(page.get_by_role("button", name="Close menu"), f"{width}px").to_be_visible()
            page.get_by_role("navigation", name="Main").get_by_role("link", name="Team", exact=True).click()
            expect(page).to_have_url(re.compile(r"/team$"))
        else:
            expect(toggle, f"{width}px").to_be_hidden()
            expect(banner(page).get_by_role("navigation", name="Main").get_by_role(
                "link", name="Team", exact=True)).to_be_visible()


# -- Primary buttons must not move under the pointer on hover ---------------


def assert_stays_put_on_hover(page: Page, locator, what: str):
    locator.scroll_into_view_if_needed()
    page.mouse.move(0, 0)
    page.wait_for_timeout(250)
    before = _box(locator)
    locator.hover()
    page.wait_for_timeout(300)  # longer than the 0.15s transition
    after = _box(locator)
    assert (after["x"], after["y"], after["height"]) == (before["x"], before["y"], before["height"]), (
        f"{what} moved on hover: {before} -> {after}")
    transform = locator.evaluate("el => getComputedStyle(el).transform")
    assert transform in ("none", "matrix(1, 0, 0, 1, 0, 0)"), f"{what} has a hover transform: {transform}"


def test_us_4_generate_project_button_does_not_lift_on_hover(page: Page, backend: FakeBackend):
    sign_in(page)
    name, reqs, submit = open_create_form(page)
    assert_stays_put_on_hover(page, submit, "'Generate Project'")
    hover_filter = submit.evaluate("el => getComputedStyle(el).filter")
    assert "brightness" in hover_filter, f"Hover feedback (brightness) was lost: {hover_filter!r}"
    page.mouse.move(0, 0)
    expect(submit).to_have_css("filter", "none")


def test_us_4_slow_click_with_valid_form_creates_once(page: Page, backend: FakeBackend):
    sign_in(page)
    name, reqs, submit = open_create_form(page)
    reqs.fill("Slow-click spec.")
    name.fill("Slow Click App")
    name.focus()
    box = _box(submit)
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=8)
    page.wait_for_timeout(300)  # pointer rests on the button before pressing
    assert _box(submit)["y"] == box["y"], "Submit moved while hovered"
    page.mouse.down()
    page.wait_for_timeout(250)
    page.mouse.up()
    expect(page).to_have_url(re.compile(r"/crown-ai/projects/[^/?]+"))
    assert len(backend.calls_to("POST", r"^/projects$")) == 1


def test_us_4_click_near_bottom_edge_of_submit_still_submits(page: Page, backend: FakeBackend):
    sign_in(page)
    name, reqs, submit = open_create_form(page)
    name.fill("Edge App")
    reqs.fill("Edge spec.")
    box = _box(submit)
    # Bottom pixel row: a 1px lift would move the button off this point mid-click.
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] - 0.5)
    page.wait_for_timeout(300)
    page.mouse.down()
    page.wait_for_timeout(150)
    page.mouse.up()
    expect(page).to_have_url(re.compile(r"/crown-ai/projects/[^/?]+"))


def test_us_4_project_page_primary_buttons_stay_put_on_hover(page: Page, backend: FakeBackend):
    pid = backend.seed_project("Hover App", stages=["requirements"])
    sign_in(page)
    goto(page, f"/crown-ai/projects/{pid}")
    assert_stays_put_on_hover(page, page.get_by_role("button", name="Generate all artifacts"), "'Generate all artifacts'")
    assert_stays_put_on_hover(page, page.get_by_role("button", name="Download artifacts"), "'Download artifacts'")


def test_us_6_upgrade_buttons_stay_put_on_hover_and_slow_click_opens_form(page: Page, backend: FakeBackend):
    goto(page, "/pricing")
    for plan in ("Mid", "Large", "Global"):
        button = page.get_by_role("button", name=f"Upgrade to {plan}")
        expect(button).to_be_enabled()
        assert_stays_put_on_hover(page, button, f"'Upgrade to {plan}'")
    button = page.get_by_role("button", name="Upgrade to Mid")
    box = _box(button)
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] - 0.5)
    page.wait_for_timeout(300)
    page.mouse.down()
    page.wait_for_timeout(200)
    page.mouse.up()
    expect(page.get_by_role("dialog", name="Upgrade to Mid")).to_be_visible()


def test_us_9_send_message_button_stays_put_on_hover(page: Page, backend: FakeBackend):
    goto(page, "/contact")
    assert_stays_put_on_hover(page, page.get_by_role("button", name="Send message"), "'Send message'")


def test_us_3_header_launch_button_stays_put_on_hover(page: Page, backend: FakeBackend):
    page.set_viewport_size({"width": 1440, "height": 900})
    for path in ("/", "/services/crown-ai"):
        goto(page, path)
        assert_stays_put_on_hover(page, banner(page).get_by_role("link", name="Launch Crown AI"),
                                  f"Header 'Launch Crown AI' on {path}")


# ===========================================================================
# Regression coverage: hand-rolled tailwind-postcss.js shim deleted
# ===========================================================================
# The shim (PostCSS plugin "crownai-tailwindcss" calling tailwindcss.compile())
# is gone, and app/globals.css now compiles only through @tailwindcss/postcss.
# The user-visible risk is a site that loads unstyled or half-styled: raw
# directives reaching the browser, @config theme tokens dropped, utilities
# losing to component classes, variants or keyframes missing. These tests
# check each of those on the live pages.

LIGHT = {"brand": "rgb(161, 73, 10)", "ink": "rgb(28, 25, 23)", "muted": "rgb(87, 83, 78)",
         "subtle": "rgb(255, 250, 235)", "line": "rgb(236, 223, 191)", "canvas": "rgb(255, 255, 255)"}
DARK = {"brand": "rgb(252, 211, 77)", "canvas": "rgb(11, 11, 16)", "muted": "rgb(188, 182, 171)"}

RAW_TAILWIND_RE = re.compile(
    r"@tailwind\b|@config\b|@apply\b|@theme\b|@import\s+[\"']tailwindcss|@plugin\b|@source\b")
SHIM_MARKER_RE = re.compile(r"crownai-tailwindcss|tailwind-postcss", re.I)


def served_css(page: Page):
    """Every stylesheet the page actually uses, as the browser received it."""
    hrefs = page.evaluate(
        "() => [...document.querySelectorAll('link[rel=stylesheet]')].map((l) => l.href)"
        ".filter((h) => h.startsWith(location.origin))"
    )
    inline = page.evaluate("() => [...document.querySelectorAll('style')].map((s) => s.textContent || '')")
    sheets = {}
    for href in hrefs:
        resp = page.request.get(href, timeout=15000)
        assert resp.status == 200, f"Stylesheet {href} -> HTTP {resp.status}"
        assert "text/css" in resp.headers.get("content-type", ""), f"{href} not served as CSS"
        sheets[href] = resp.text()
    for i, text in enumerate(inline):
        sheets[f"<style #{i}>"] = text
    return sheets


def css_of(locator, prop: str) -> str:
    return locator.evaluate(f"(el) => getComputedStyle(el).{prop}")


def test_us_2_shim_removed_served_css_is_compiled_not_raw(page: Page, backend: FakeBackend):
    for path in ("/", "/about", "/pricing", "/crown-ai"):
        goto(page, path, wait_until="load")
        sheets = served_css(page)
        assert sheets, f"{path}: no stylesheet reached the browser"
        everything = "\n".join(sheets.values())
        raw = RAW_TAILWIND_RE.findall(everything)
        assert not raw, f"{path}: uncompiled Tailwind directives reached the browser: {sorted(set(raw))}"
        assert not SHIM_MARKER_RE.search(everything), f"{path}: CSS still produced by the old shim plugin"
        # Utilities actually used in the markup must exist as compiled rules.
        for selector in (r".text-brand", r".bg-subtle", r".grid-cols-3", r"lg\:grid-cols-3"):
            assert selector in everything, f"{path}: compiled CSS has no {selector!r} rule"


def test_us_2_shim_removed_shim_file_is_not_served(page: Page, backend: FakeBackend):
    for name in ("tailwind-postcss.js", "tailwind-postcss.cjs", "tailwind-postcss.mjs", "tailwind-postcss.ts"):
        resp = page.request.get(f"{BASE_URL}/{name}", timeout=10000)
        assert resp.status == 404, f"/{name} -> HTTP {resp.status}; the deleted shim must not be served"
        assert "crownai-tailwindcss" not in resp.text(), f"/{name} still serves the shim source"


def test_us_2_shim_removed_utilities_win_over_component_classes(page: Page, backend: FakeBackend):
    page.emulate_media(color_scheme="light")
    goto(page, "/about", wait_until="load")
    title = page.get_by_role("heading", name="Our mission", exact=True)
    expect(title).to_be_visible()
    # `.card-title` (components layer) sets --ink; `text-brand` (utilities) must win.
    expect(title).to_have_css("color", LIGHT["brand"])
    expect(title).to_have_css("font-weight", "600")  # from .card-title, untouched by utilities
    card = title.locator("xpath=..")
    assert float(css_of(card, "borderTopLeftRadius").rstrip("px")) == 16, ".card radius (--radius-lg) missing"
    assert float(css_of(card, "paddingTop").rstrip("px")) == 24, ".card padding (--space-6) missing"


def test_us_2_shim_removed_config_theme_tokens_reach_utilities(page: Page, backend: FakeBackend):
    page.emulate_media(color_scheme="light")
    page.set_viewport_size({"width": 375, "height": 812})
    goto(page, "/careers", wait_until="load")
    foot = footer(page)
    # Colours and type scale come from tailwind.config.js via `@config`.
    expect(foot).to_have_css("background-color", LIGHT["subtle"])       # bg-subtle
    expect(foot).to_have_css("border-top-color", LIGHT["line"])          # border-line
    expect(foot).to_have_css("border-top-width", "1px")                  # border-t
    copyright_line = foot.get_by_text(re.compile(r"All rights\s+reserved"))
    expect(copyright_line).to_have_css("font-size", "13px")              # text-xs -> --text-xs
    expect(copyright_line).to_have_css("color", LIGHT["muted"])          # text-muted
    expect(copyright_line).to_have_css("text-align", "center")
    expect(page.locator("body")).to_have_css("background-color", LIGHT["canvas"])  # @layer base
    expect(page.locator("body")).to_have_css("font-family", re.compile(r"^\"?Inter"))


def test_us_2_shim_removed_dark_theme_tokens_follow_through(page: Page, backend: FakeBackend):
    page.emulate_media(color_scheme="dark")
    goto(page, "/about", wait_until="load")
    expect(page.locator("body")).to_have_css("background-color", DARK["canvas"])
    expect(page.get_by_role("heading", name="Our mission", exact=True)).to_have_css("color", DARK["brand"])
    expect(footer(page).get_by_text(re.compile(r"All rights\s+reserved"))).to_have_css("color", DARK["muted"])
    # An explicit light choice overrides the OS preference.
    page.evaluate("() => document.documentElement.setAttribute('data-theme', 'light')")
    expect(page.locator("body")).to_have_css("background-color", LIGHT["canvas"])
    expect(page.get_by_role("heading", name="Our mission", exact=True)).to_have_css("color", LIGHT["brand"])


def test_us_2_shim_removed_hover_variant_compiles(page: Page, backend: FakeBackend):
    page.emulate_media(color_scheme="light")
    page.set_viewport_size({"width": 1440, "height": 900})
    goto(page, "/", wait_until="load")
    link = footer(page).get_by_role("link", name="Privacy Policy")
    link.scroll_into_view_if_needed()
    page.mouse.move(0, 0)
    expect(link).to_have_css("color", LIGHT["muted"])
    link.hover()
    expect(link).to_have_css("color", LIGHT["brand"])  # hover:text-brand
    page.mouse.move(0, 0)
    expect(link).to_have_css("color", LIGHT["muted"])


def test_us_2_shim_removed_arbitrary_grid_template_by_breakpoint(page: Page, backend: FakeBackend):
    def footer_columns():
        return page.evaluate(
            "() => getComputedStyle(document.querySelector('footer > .grid'))"
            ".gridTemplateColumns.split(' ').filter(Boolean).length"
        )

    for width, expected in ((375, 1), (800, 2), (1440, 4)):  # base, sm:grid-cols-2, lg:grid-cols-[1.4fr_1fr_1fr_1fr]
        page.set_viewport_size({"width": width, "height": 900})
        goto(page, "/contact", wait_until="load")
        assert footer_columns() == expected, f"Footer grid at {width}px has {footer_columns()} columns, expected {expected}"
    widths = page.evaluate(
        "() => getComputedStyle(document.querySelector('footer > .grid')).gridTemplateColumns"
        ".split(' ').map(parseFloat)"
    )
    assert widths[0] > widths[1] * 1.2, f"Arbitrary 1.4fr first column not applied: {widths}"


def test_us_2_shim_removed_keyframes_and_reduced_motion(page: Page, backend: FakeBackend):
    page.set_viewport_size({"width": 1440, "height": 900})
    page.emulate_media(reduced_motion="no-preference")
    goto(page, "/", wait_until="load")
    crown = banner(page).locator("svg.cw-logo:visible .cw-cr").first
    expect(crown).to_have_css("animation-name", "cw-crown")
    page.emulate_media(reduced_motion="reduce")
    expect(crown).to_have_css("animation-name", "none")


def test_us_2_shim_removed_every_route_is_styled(page: Page, backend: FakeBackend):
    page.emulate_media(color_scheme="light")
    page.set_viewport_size({"width": 1440, "height": 900})
    bad_css = []
    page.on("response", lambda r: bad_css.append(f"{r.url} -> {r.status}")
            if r.request.resource_type == "stylesheet" and r.status >= 400 else None)
    problems = []
    for path in list(PAGES) + ["/crown-ai", "/case-studies/regional-nbfc-chennai"]:
        goto(page, path, wait_until="load")
        expect(page.locator("[data-nextjs-dialog], [data-nextjs-error-overlay]")).to_have_count(0)
        expect(page.get_by_text(re.compile(r"Cannot find module '@tailwindcss/postcss'|Failed to compile"))).to_have_count(0)
        got = page.evaluate(
            """() => {
                const c = document.querySelector('.container-page');
                const h1 = document.querySelector('h1');
                return {
                    container: c ? getComputedStyle(c).maxWidth : null,
                    h1Weight: h1 ? getComputedStyle(h1).fontWeight : null,
                    bodyDisplay: getComputedStyle(document.body).display,
                    footerBg: getComputedStyle(document.querySelector('footer')).backgroundColor,
                };
            }"""
        )
        want = {"container": "1152px", "h1Weight": "700", "bodyDisplay": "flex", "footerBg": LIGHT["subtle"]}
        if got != want:
            problems.append(f"{path}: {got}")
    assert not bad_css, f"Stylesheets failed to load: {bad_css}"
    assert not problems, "Pages rendered without compiled Tailwind styles: " + "; ".join(problems)


def test_us_3_shim_removed_brand_gold_styles_on_site_and_tool(page: Page, backend: FakeBackend):
    page.emulate_media(color_scheme="light")
    goto(page, "/pricing", wait_until="load")
    site_button = page.get_by_role("button", name="Upgrade to Mid")
    site_bg = css_of(site_button, "backgroundImage")
    sign_in(page)
    page.get_by_role("button", name="+ New Project").click()
    tool_bg = css_of(page.get_by_role("button", name="Generate Project"), "backgroundImage")
    assert "gradient" in site_bg and site_bg == tool_bg, (
        f"Brand gold gradient differs between site and Crown AI tool: {site_bg!r} vs {tool_bg!r}")
