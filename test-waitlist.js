// Simple local test script for /waitlist/join and /subscribe
const API = process.env.API_BASE_URL || 'http://localhost:5000';

async function post(path, body) {
  const res = await fetch(`${API}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });
  const text = await res.text();
  let json; try { json = JSON.parse(text); } catch { json = { raw: text }; }
  console.log(`[${res.status}] ${path}:`, json);
  return json;
}

(async () => {
  await post('/subscribe', { email: 'waitlist.tester@example.com', firstName: 'Waitlist', lastName: 'Tester' });
  await post('/waitlist/join', {
    firstName: 'Wait',
    lastName: 'Lister',
    email: 'waitlist.tester@example.com',
    university: 'Test University',
    graduationYear: '2027',
    notifications: true,
    // Provide a captcha token via env TURNSTILE_TEST_TOKEN when server verification is enabled
    captchaToken: process.env.TURNSTILE_TEST_TOKEN || undefined,
  });
})();
