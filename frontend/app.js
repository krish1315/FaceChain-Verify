/* ============================================================
   faceid-chain-verify — Frontend JavaScript
   ============================================================ */

'use strict';

// ── Config ────────────────────────────────────────────────────
const API_BASE = 'http://127.0.0.1:8000';
const POLL_INTERVAL_MS = 1500;
const MAX_POLL_MS = 300_000; // 5 min

// ── Pipeline stages in order ──────────────────────────────────
const STAGES = [
  { key: 'starting',        label: 'Starting',        icon: '⚡', desc: 'Initialising…' },
  { key: 'input_sha256',    label: 'Hashing image',  icon: '#',  desc: 'Computing SHA-256' },
  { key: 'face_encode',     label: 'Detecting face', icon: '🔍', desc: 'InsightFace buffalo_l' },
  { key: 'web_search_full', label: 'Searching web',  icon: '🌐', desc: 'Google Cloud Vision' },
  { key: 'rank_social',     label: 'Ranking matches', icon: '🏆', desc: 'Social domain filter' },
  { key: 'build_record',    label: 'Building record', icon: '📋', desc: 'Canonical JSON' },
  { key: 'ipfs_pin',        label: 'Pinning to IPFS', icon: '📌', desc: 'Pinata' },
  { key: 'hash_record',     label: 'Hashing record',  icon: '🔏', desc: 'SHA-256' },
  { key: 'chain_submit',    label: 'Writing to chain',icon: '⛓',  desc: 'Polygon Amoy' },
  { key: 'complete',        label: 'Done',            icon: '✅', desc: '' },
];

// Map backend stage → display stage (some backend stages are not in STAGES above)
const BACKEND_STAGE_MAP = {
  'queued': 'starting',
  'input_sha256': 'input_sha256',
  'face_encode': 'face_encode',
  'web_search_full': 'web_search_full',
  'rank_social': 'rank_social',
  'build_record': 'build_record',
  'ipfs_pin': 'ipfs_pin',
  'hash_record': 'hash_record',
  'chain_submit': 'chain_submit',
  'complete': 'complete',
};

// Stages that indicate the job is still running
const RUNNING_STAGES = new Set([
  'starting', 'input_sha256', 'face_encode',
  'web_search_full', 'rank_social', 'build_record',
  'ipfs_pin', 'hash_record', 'chain_submit',
]);

// ── State ────────────────────────────────────────────────────
let currentJobId = null;
let pollTimer = null;
let selectedFile = null;

// ── DOM refs ─────────────────────────────────────────────────
const $ = id => document.getElementById(id);
const $$ = sel => document.querySelectorAll(sel);

// ── Utilities ─────────────────────────────────────────────────

function formatBytes(n) {
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
  return (n / (1024 * 1024)).toFixed(1) + ' MB';
}

function truncate(str, len = 16) {
  if (!str) return '—';
  if (str.length <= len) return str;
  return str.slice(0, len) + '…';
}

function api(path, opts = {}) {
  return fetch(`${API_BASE}${path}`, opts).then(async r => {
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.detail || `HTTP ${r.status}`);
    return body;
  });
}

function poll(path, callback) {
  return new Promise((resolve, reject) => {
    const deadline = Date.now() + MAX_POLL_MS;
    function tick() {
      if (Date.now() > deadline) {
        reject(new Error('Polling timed out after 5 minutes'));
        return;
      }
      api(path)
        .then(data => {
          const done = callback(data);
          if (done) { resolve(data); return; }
          pollTimer = setTimeout(tick, POLL_INTERVAL_MS);
        })
        .catch(reject);
    }
    tick();
  });
}

// ── Tab switching ──────────────────────────────────────────────
$$('.tab').forEach(btn => {
  btn.addEventListener('click', () => {
    $$('.tab').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    const tab = btn.dataset.tab;
    $$('.tab-panel').forEach(p => p.classList.add('hidden'));
    $(`tab-${tab}`).classList.remove('hidden');
  });
});

// ── Upload zone ───────────────────────────────────────────────
const dropZone = $('drop-zone');
const fileInput = $('file-input');

['dragenter', 'dragover'].forEach(evt => {
  dropZone.addEventListener(evt, e => { e.preventDefault(); dropZone.classList.add('drag-over'); });
});
['dragleave', 'drop'].forEach(evt => {
  dropZone.addEventListener(evt, e => { e.preventDefault(); dropZone.classList.remove('drag-over'); });
});
dropZone.addEventListener('drop', e => {
  const f = e.dataTransfer.files[0];
  if (f && f.type.startsWith('image/')) handleFile(f);
});
fileInput.addEventListener('change', () => {
  if (fileInput.files[0]) handleFile(fileInput.files[0]);
});

function handleFile(file) {
  selectedFile = file;
  const url = URL.createObjectURL(file);
  $('preview-img').src = url;
  $('preview-filename').textContent = file.name;
  $('preview-size').textContent = formatBytes(file.size);
  showView('preview');
}

function showView(name) {
  ['upload', 'preview', 'progress', 'results'].forEach(v => {
    $(`${v}-view`).classList.toggle('hidden', v !== name);
  });
}

// ── Progress tracker ──────────────────────────────────────────
function buildTracker() {
  const tracker = $('progress-tracker');
  tracker.innerHTML = '';
  STAGES.forEach(s => {
    const div = document.createElement('div');
    div.className = 'progress-step';
    div.id = `step-${s.key}`;
    div.innerHTML = `
      <div class="step-icon">${s.icon}</div>
      <div class="step-info">
        <div class="step-label">${s.label}</div>
        <div class="step-sub" id="sub-${s.key}">${s.desc}</div>
      </div>`;
    tracker.appendChild(div);
  });
}

function advanceToStage(backendStage) {
  const key = BACKEND_STAGE_MAP[backendStage] || backendStage;
  STAGES.forEach(s => {
    const el = $(`step-${s.key}`);
    el.classList.remove('done', 'active', 'error');
    if (s.key === key) {
      el.classList.add('active');
      $(`sub-${s.key}`).textContent = 'In progress…';
    } else if (STAGES.findIndex(x => x.key === s.key) < STAGES.findIndex(x => x.key === key)) {
      el.classList.add('done');
      $(`sub-${s.key}`).textContent = 'Complete';
    }
  });
  // Update badge
  const label = STAGES.find(s => s.key === key)?.label || backendStage;
  $('job-stage-badge').textContent = label;
}

function completeAllStages() {
  STAGES.forEach(s => {
    const el = $(`step-${s.key}`);
    el.classList.remove('active', 'error');
    el.classList.add('done');
    $(`sub-${s.key}`).textContent = 'Complete';
  });
  $('job-stage-badge').textContent = 'Complete';
  $('job-stage-badge').style.background = 'var(--pass-dim)';
  $('job-stage-badge').style.color = 'var(--pass)';
  $('job-stage-badge').style.borderColor = 'rgba(34,197,94,0.3)';
}

// ── Run pipeline ───────────────────────────────────────────────
$('run-btn').addEventListener('click', runPipeline);
$('cancel-btn').addEventListener('click', () => { showView('upload'); selectedFile = null; });
$('reset-btn').addEventListener('click', () => {
  showView('upload');
  selectedFile = null;
  currentJobId = null;
  if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
  $('job-stage-badge').textContent = 'queued';
  $('job-stage-badge').style.cssText = '';
});

async function runPipeline() {
  if (!selectedFile) return;
  showView('progress');
  buildTracker();
  $('job-id').textContent = '—';
  $('progress-bar').style.width = '0%';
  $('progress-pct').textContent = '0%';
  $('face-preview').classList.add('hidden');

  // Submit
  let jobId;
  try {
    const fd = new FormData();
    fd.append('image', selectedFile);
    const res = await fetch(`${API_BASE}/api/pipeline/run`, { method: 'POST', body: fd });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    jobId = data.job_id;
    currentJobId = jobId;
    $('job-id').textContent = jobId;
  } catch (err) {
    showError('Failed to start job: ' + err.message);
    showView('upload');
    return;
  }

  // Poll
  pollTimer = setTimeout(function poll() {
    api(`/api/pipeline/status/${jobId}`)
      .then(data => {
        // Progress bar
        const pct = Math.round((data.progress || 0) * 100);
        $('progress-bar').style.width = pct + '%';
        $('progress-pct').textContent = pct + '%';

        // Stage tracker
        advanceToStage(data.stage || 'starting');

        // Show face crop as soon as available
        if (data.result?.face) {
          showFacePreview(data.result);
        }

        if (data.status === 'done') {
          clearTimeout(pollTimer);
          pollTimer = null;
          completeAllStages();
          renderResults(data.result);
          showView('results');
        } else if (data.status === 'error') {
          clearTimeout(pollTimer);
          pollTimer = null;
          const errMsg = data.error || 'Pipeline failed';
          // Mark the current step as error
          const step = $(`step-${BACKEND_STAGE_MAP[data.stage] || data.stage}`);
          if (step) { step.classList.remove('active'); step.classList.add('error'); }
          showError(`Pipeline error: ${errMsg}`);
          showView('upload');
        } else {
          pollTimer = setTimeout(poll, POLL_INTERVAL_MS);
        }
      })
      .catch(err => {
        clearTimeout(pollTimer);
        showError('Poll failed: ' + err.message);
        showView('upload');
      });
  }, POLL_INTERVAL_MS);
}

// ── Face preview (shown as soon as detection finishes) ─────────
function showFacePreview(result) {
  $('face-preview').classList.remove('hidden');
  if (result.face?.cropped_face_path) {
    // cropped_face_path is a local temp path — use a direct file:// URL won't work in browser.
    // The API doesn't expose a direct URL for the cropped face.
    // We display the face confidence as a proxy.
    $('face-crop-img').style.display = 'none';
  }
  $('face-confidence').textContent = result.face?.detection_confidence != null
    ? (result.face.detection_confidence * 100).toFixed(1) + '%'
    : '—';
  $('face-embedding-hash').textContent = truncate(result.face?.embedding_hash || '—', 20);
}

// ── Results rendering ─────────────────────────────────────────
function renderResults(result) {
  if (!result) return;

  // Face panel
  const face = result.face || {};
  $('result-face-img').style.display = 'none'; // no direct URL for cropped face
  $('result-confidence').textContent = face.detection_confidence != null
    ? (face.detection_confidence * 100).toFixed(1) + '%'
    : '—';
  $('result-emb-hash').textContent = truncate(face.embedding_hash || '—', 32);

  // Summary banner
  const summaryEl = $('results-summary');
  if (result.match_found) {
    summaryEl.innerHTML = `
      <div class="badge badge-teal">✓ Social match found</div>`;
  } else {
    summaryEl.innerHTML = `
      <div class="warn-banner" style="margin: 0;">
        <strong>No social-domain match found</strong>
        The pipeline completed, but no results matched the social media filter.
        ${
          result.best_guess_labels?.length
            ? `<div style="font-size:0.8rem;margin-top:0.25rem;">Google Vision guessed: <em>${result.best_guess_labels.join(', ')}</em></div>`
            : ''
        }
      </div>`;
  }

  // Match card
  const matchContainer = $('match-card-container');
  if (result.match_found && result.social_matches?.length) {
    const m = result.social_matches[0];
    const thumbUrl = `${API_BASE}/api/image-proxy?url=${encodeURIComponent(m.matched_image_url || m.url)}`;
    const typeClass = m.match_type === 'full_match' ? 'full' : m.match_type === 'partial_match' ? 'partial' : 'similar';
    matchContainer.innerHTML = `
      <div class="match-card">
        <img class="match-thumb" src="${thumbUrl}" alt="Matched image"
             onerror="this.style.display='none'" />
        <div class="match-body">
          <div class="platform-badge">
            ${platformEmoji(m.domain)} ${m.domain}
          </div>
          <div class="match-type-badge ${typeClass}">${m.match_type.replace('_', ' ')}</div>
          <div class="match-title">${m.page_title || '—'}</div>
          <div class="match-url">${truncate(m.url, 60)}</div>
          <a class="btn btn-outline btn-sm" href="${m.url}" target="_blank" rel="noopener">
            View original post ↗
          </a>
        </div>
      </div>`;
  } else {
    matchContainer.innerHTML = `
      <div class="empty-state">
        <div class="empty-state-icon">🔍</div>
        <h4>No social post matched</h4>
        <p>The image wasn't found on the targeted social platforms.<br>The record was still anchored on-chain.</p>
      </div>`;
  }

  // Blockchain proof card
  const proofRows = $('proof-rows');
  const recordHash = result.record_hash || '';
  const ipfsCid = result.ipfs_cid || '';
  const txHash = result.tx_hash || '';
  const recordId = result.record_id;
  const polygonscanUrl = result.polygonscan_url || '';
  const gatewayUrl = ipfsCid ? `https://gateway.pinata.cloud/ipfs/${ipfsCid}` : '';

  proofRows.innerHTML = `
    ${ipfsCid ? `
    <div class="proof-row">
      <div class="proof-key">IPFS CID</div>
      <div class="proof-val">
        <a href="${gatewayUrl}" target="_blank" rel="noopener" style="color:var(--teal)">${ipfsCid}</a>
      </div>
    </div>` : ''}
    ${recordHash ? `
    <div class="proof-row">
      <div class="proof-key">Record hash (sha256)</div>
      <div class="proof-val flex items-center gap-1" style="justify-content:flex-end;">
        <span>${truncate(recordHash, 24)}</span>
        <button class="copy-btn" data-copy="${recordHash}">copy</button>
      </div>
    </div>` : ''}
    ${txHash ? `
    <div class="proof-row">
      <div class="proof-key">Tx hash</div>
      <div class="proof-val">
        <span>${truncate(txHash, 24)}</span>
        <button class="copy-btn" data-copy="${txHash}">copy</button>
      </div>
    </div>` : ''}
    <div class="proof-row">
      <div class="proof-key">Record ID</div>
      <div class="proof-val">#${recordId ?? '—'}</div>
    </div>
    <div class="proof-row">
      <div class="proof-key">Pipeline duration</div>
      <div class="proof-val">${result.total_duration_sec?.toFixed(2) ?? '—'}s</div>
    </div>`;

  $('polygonscan-link').href = polygonscanUrl || '#';
  $('polygonscan-link').style.display = polygonscanUrl ? '' : 'none';

  // Copy buttons
  $$('.copy-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      navigator.clipboard.writeText(btn.dataset.copy).then(() => {
        btn.textContent = 'copied!';
        btn.classList.add('copied');
        setTimeout(() => { btn.textContent = 'copy'; btn.classList.remove('copied'); }, 2000);
      });
    });
  });
}

// ── Verification tab ───────────────────────────────────────────
$('verify-run-btn').addEventListener('click', runVerification);

async function runVerification() {
  const recordId = parseInt($('verify-record-id').value, 10);
  if (!recordId) {
    alert('Please enter a record ID');
    return;
  }

  const card = $('verify-results-card');
  const checksEl = $('verify-checks');
  const linksEl = $('verify-links');
  const badgeEl = $('verify-overall-badge');

  card.classList.remove('hidden');
  checksEl.innerHTML = '<div class="flex items-center gap-2"><div class="spinner"></div> Verifying record…</div>';
  badgeEl.innerHTML = '';
  linksEl.innerHTML = '';

  const query = new URLSearchParams({ record_id: recordId });

  // If an image was uploaded for face comparison
  const imgFile = $('verify-image-input').files[0];
  // Note: the API expects 'image' query param as local path (not usable in browser).
  // Face-match verification via API requires a local path which the browser can't provide.
  // So we skip the face match in browser mode and explain it in the detail.

  try {
    const data = await api(`/api/pipeline/verify/${recordId}`);
    renderVerificationResult(data);
  } catch (err) {
    checksEl.innerHTML = `
      <div class="error-banner">
        <strong>Verification failed</strong>
        ${err.message}
      </div>`;
    badgeEl.innerHTML = '<span class="badge badge-fail">Error</span>';
  }
}

function renderVerificationResult(data) {
  const checksEl = $('verify-checks');
  const badgeEl = $('verify-overall-badge');
  const linksEl = $('verify-links');

  checksEl.innerHTML = '';
  badgeEl.innerHTML = data.all_passed
    ? '<span class="badge badge-teal">✓ All checks passed</span>'
    : '<span class="badge badge-fail">✗ Some checks failed</span>';

  data.checks.forEach(c => {
    const item = document.createElement('div');
    item.className = `check-item ${c.passed ? 'pass' : 'fail'}`;
    item.innerHTML = `
      <div class="check-icon">${c.passed ? '✓' : '✗'}</div>
      <div>
        <div class="check-name">${c.name.replace(/_/g, ' ')}</div>
        ${c.detail ? `<div class="check-detail">${c.detail}</div>` : ''}
      </div>`;
    checksEl.appendChild(item);
  });

  // On-chain details
  if (data.on_chain) {
    const oc = data.on_chain;
    const cid = oc.ipfsCID;
    const gatewayUrl = cid ? `https://gateway.pinata.cloud/ipfs/${cid}` : '';
    const ts = oc.timestamp ? new Date(Number(oc.timestamp) * 1000).toISOString() : '—';

    linksEl.innerHTML = `
      <div class="section-title mt-2">On-chain record details</div>
      <div class="flex-col gap-1">
        <div class="flex justify-between" style="font-size:0.8rem;">
          <span class="text-muted">Submitter</span>
          <span class="font-mono">${oc.submitter || '—'}</span>
        </div>
        <div class="flex justify-between" style="font-size:0.8rem;">
          <span class="text-muted">Timestamp</span>
          <span class="font-mono">${ts}</span>
        </div>
        ${cid ? `
        <div class="flex justify-between" style="font-size:0.8rem;">
          <span class="text-muted">IPFS CID</span>
          <a href="${gatewayUrl}" target="_blank" style="color:var(--teal);font-size:0.78rem;">${truncate(cid, 20)} ↗</a>
        </div>` : ''}
      </div>`;
  }

  // Fetched record snippet
  if (data.fetched_record) {
    const rec = data.fetched_record;
    const snippet = JSON.stringify({
      matchedDomain: rec.matchedDomain,
      matchedUrl: truncate(rec.matchedUrl, 60),
      pageTitle: truncate(rec.pageTitle, 60),
      matchFound: rec.matchFound,
    }, null, 2);
    const recSection = document.createElement('div');
    recSection.className = 'mt-2';
    recSection.innerHTML = `
      <div class="section-title">Fetched IPFS record</div>
      <pre style="background:var(--bg-elevated);border:1px solid var(--border);border-radius:var(--radius);padding:0.75rem;font-size:0.75rem;font-family:'JetBrains Mono',monospace;overflow:auto;max-height:200px;white-space:pre;">${snippet}</pre>`;
    linksEl.appendChild(recSection);
  }
}

// ── Error display ─────────────────────────────────────────────
function showError(msg) {
  // Inject a temporary banner at the top of the page
  const existing = document.getElementById('global-error');
  if (existing) existing.remove();
  const banner = document.createElement('div');
  banner.id = 'global-error';
  banner.className = 'error-banner';
  banner.style.cssText = 'position:sticky;top:0;z-index:100;margin:0 0 1rem;border-radius:var(--radius-lg);';
  banner.innerHTML = `<strong>Error</strong>${msg}`;
  $('app').prepend(banner);
  setTimeout(() => banner.remove(), 8000);
}

// ── Platform emoji helper ──────────────────────────────────────
function platformEmoji(domain) {
  const map = {
    'x.com': '🐦', 'twitter.com': '🐦',
    'instagram.com': '📸',
    'facebook.com': '👤',
    'linkedin.com': '💼',
    'reddit.com': '🤖',
    'pinterest.com': '📌',
    'tumblr.com': '🔮',
    'vk.com': '💬',
  };
  return map[domain] || '🔗';
}

// ── Initial state ──────────────────────────────────────────────
showView('upload');
