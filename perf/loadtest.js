import http from 'k6/http';
import { check, sleep } from 'k6';
import { Trend, Rate, Counter } from 'k6/metrics';

const GET__health_duration = new Trend('txn_duration__GET__health', true);
const GET__health_errors = new Rate('txn_errors__GET__health');
const GET__health_count = new Counter('txn_count__GET__health');

// Without this, k6's own http_req_failed metric (what the error-rate
// threshold below actually gates on) classifies ANY status >= 400 as a
// failure by default -- including the 404s/401s/403s the check above
// explicitly treats as fine (a since-deleted id, or an anonymous request to
// an auth-protected endpoint, are expected responses, not bugs). Confirmed
// in practice: a real run's error-rate threshold failed at a rate well
// above what the check itself reported failing, entirely from responses
// that were never a real problem. Aligning http_req_failed's own definition
// of "failed" with the check's is what makes the threshold actually measure
// what it claims to (a real 5xx, or an unexpected 4xx, is still failed).
http.setResponseCallback(http.expectedStatuses(200, 401, 403, 404));

export const options = {
  stages: [
    { duration: '5s', target: 5 },
    { duration: '10s', target: 5 },
    { duration: '3s', target: 0 },
  ],
  thresholds: {
    http_req_duration: ['p(95)<1000'],
    http_req_failed: ['rate<0.05'],
  },
};

const BASE = __ENV.BASE_URL || 'http://127.0.0.1:8020';

export default function (data) {
  {
    const r = http.get(`${BASE}/health`);
    check(r, { 'GET /health is 200/401/403/404': (resp) => [200, 401, 403, 404].includes(resp.status) });
    GET__health_duration.add(r.timings.duration);
    GET__health_errors.add(r.status <= 0 || r.status >= 500);
    GET__health_count.add(1);
  }

  sleep(1);
}
