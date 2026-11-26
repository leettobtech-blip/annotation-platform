// k6 run -e BASE_URL=https://staging.example -e PROJECT_ID=... -e TOKEN=... deploy/load.js
import http from 'k6/http';
import { check } from 'k6';
import { Rate, Trend } from 'k6/metrics';
import { sleep } from 'k6';

const chatCompleted = new Rate('chat_completed');
const turnTime = new Trend('turn_time', true);

export const options = {
  scenarios: {
    chat_turns: {
      executor: 'constant-arrival-rate',
      rate: Number(__ENV.TURNS_PER_SECOND || 2),
      timeUnit: '1s',
      duration: __ENV.DURATION || '2m',
      preAllocatedVUs: Number(__ENV.PREALLOCATED_VUS || 20),
      maxVUs: Number(__ENV.MAX_VUS || 100),
    },
  },
  thresholds: {
    dropped_iterations: ['count==0'],
    http_req_failed: ['rate<0.01'],
    chat_completed: ['rate>0.99'],
    turn_time: ['p(95)<10000'],
  },
};

const root = `${__ENV.BASE_URL}/v1/projects/${__ENV.PROJECT_ID}`;
const auth = { Authorization: `Bearer ${__ENV.TOKEN}` };

export default function () {
  const start = Date.now();
  const submitted = http.post(`${root}/chat`, JSON.stringify({
    question: __ENV.QUESTION || 'What does the quartz calibrator use?',
  }), { headers: { ...auth, 'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID() }, tags: { name: 'submit chat' } });
  if (!check(submitted, { accepted: (r) => r.status === 202 })) {
    chatCompleted.add(false);
    return;
  }
  const id = submitted.json('id');
  while (Date.now() - start < 15000) {
    sleep(2 + Math.random());
    const poll = http.get(`${root}/jobs/${id}`, { headers: auth, tags: { name: 'poll chat' } });
    if (poll.status !== 200) break;
    const status = poll.json('status');
    if (status === 'succeeded') {
      chatCompleted.add(true);
      turnTime.add(Date.now() - start);
      return;
    }
    if (status === 'failed') break;
  }
  chatCompleted.add(false);
}
