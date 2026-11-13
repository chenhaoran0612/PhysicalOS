'use strict';
// Issue #7: applies web/readable.js to everything the console renders, so no page shows code
// or JSON by default. Pages keep emitting their markup unchanged; this pass rewrites:
//   - every <pre> (the json() helper's .json-view and the raw blocks) into a readable view,
//     with commands, raw output and file contents folded behind a "展开查看…" summary;
//   - any element whose whole text is JSON / a traceback / text with ``` fences.
// Opt out with data-keep-code on the element or an ancestor. Folds keep their open state
// across the app's re-renders.
(() => {
  const R = window.nxReadable;
  if (!R) return;
  const SKIP_TAGS = new Set(['SCRIPT', 'STYLE', 'TEXTAREA', 'OPTION', 'SELECT', 'INPUT', 'SUMMARY', 'BUTTON', 'TITLE']);
  const SKIP_INSIDE = '.nx-readable,[data-keep-code],[contenteditable],#auth-gate .auth-card';
  const MIN_JSON_LENGTH = 2;
  const openFolds = new Set();

  const skipped = el => !el || SKIP_TAGS.has(el.tagName) || Boolean(el.closest(SKIP_INSIDE));

  // What the block holds, read from the nearest heading or label before it (an approval reads:
  // 执行 SSH 命令 / host / purpose / command), then from the summary of the <details> around it.
  const CONTEXT_SIBLINGS = 3;
  const CONTEXT_WORDS = [[/错误输出|stderr/i, '错误输出'], [/输出|采集|返回/, '原始输出'], [/命令/, '完整命令'],
    [/差异|改动|diff/i, '改动内容'], [/文件|产物|快照|Skill/i, '文件内容']];
  function contextWord(pre) {
    if (pre.classList.contains('hw-command')) return '完整命令';
    const texts = [];
    for (let el = pre.previousElementSibling, i = 0; el && i < CONTEXT_SIBLINGS; el = el.previousElementSibling, i += 1) {
      if (/^(H\d|B|STRONG|SUMMARY|P|LABEL)$/.test(el.tagName)) texts.push(el.textContent);
    }
    const summary = pre.closest('details')?.querySelector('summary')?.textContent;
    if (summary) texts.push(summary);
    for (const text of texts) for (const [re, word] of CONTEXT_WORDS) if (re.test(text)) return word;
    return '内容';
  }

  // Approvals: what the user is asked to approve (the command of a device change, an SSH command) must be
  // readable without a click, so folds inside an approval form start open; the user can still close them.
  // They get an empty key: their state is not remembered and cannot open a fold elsewhere.
  // (The fix chat of web/investigation.js keeps its approval card as rendered, with data-keep-code.)
  const APPROVAL = 'form[data-form="engine-approval"]';

  function build(html, context) {
    const box = document.createElement('div');
    box.className = 'nx-readable';
    box.innerHTML = html;
    if (context?.closest(APPROVAL)) box.querySelectorAll('details.nx-fold').forEach(d => { d.open = true; d.dataset.foldKey = ''; });
    return box;
  }

  function convertPre(pre) {
    if (pre.classList.contains('nx-raw') || skipped(pre)) return;
    const text = pre.textContent;
    const kind = R.classify(text);
    const insideDetails = Boolean(pre.parentElement?.closest('details'));
    let html;
    if (kind === 'code' && insideDetails) html = `<pre class="nx-raw">${R.esc(text)}</pre>`; // the page already folds it
    else html = R.renderText(text, {what: contextWord(pre)});
    const box = build(html, pre);
    box.classList.add('nx-from-pre');
    if (pre.className) box.dataset.was = pre.className;
    pre.replaceWith(box);
  }

  const looksLikeData = t => t.length >= MIN_JSON_LENGTH && /^[{[]/.test(t) && /[}\]]$|"[^"]*$|,$/.test(t);
  const needsText = t => looksLikeData(t) || /```|Traceback \(most recent call last\)/.test(t);
  // Session transcripts and job logs: one entry per paragraph, next to a tag naming its kind.
  const LOG_ENTRY = '.log-item > p';
  const MARKDOWN = /^\s{0,3}#{1,6}\s|\*\*[^*\n]+\*\*|^\s*\|.*\|\s*$|`[^`\n]+`/m;
  const inLog = el => el.matches(LOG_ENTRY);
  // Entries whose kind is machine output (tag text set by web/engineering.js) fold however they read.
  const MACHINE_TAGS = new Set(['命令输出', '命令', '文件修改']);

  function convertTextNode(node) {
    const parent = node.parentElement;
    const text = node.textContent.trim();
    if (!parent || skipped(parent) || !(needsText(text) || inLog(parent))) return;
    if (parent.textContent.trim() !== text) return; // only when the text is the whole element
    const kind = R.classify(text);
    const tag = inLog(parent) ? parent.parentElement.querySelector('.tag')?.textContent.trim() || '' : '';
    const machine = MACHINE_TAGS.has(tag) && (kind === 'prose' || kind === 'code');
    if (!machine && (kind === 'empty' || (kind === 'prose' && !/```/.test(text) && !(inLog(parent) && MARKDOWN.test(text))))) return;
    const box = build(machine ? R.foldCode(text, tag) : R.renderText(text, {what: '内容'}), parent);
    if (parent.tagName === 'CODE') parent.replaceWith(box);
    else node.replaceWith(box);
  }

  // A fold is remembered by where it sits, not by what it holds: live output (the session stream,
  // growing command output) changes on every poll. Key = nearest ancestor with an id + the fold's
  // label without its line count + its position among folds with that label under that ancestor.
  const MAX_REMEMBERED = 200;
  const ownerOf = d => d.parentElement?.closest('[id]') || document.body;
  const foldLabel = d => (d.querySelector('summary')?.textContent || '').replace(/（\d+ 行）$/, '');
  function assignKeys() {
    const fresh = document.querySelectorAll('details.nx-fold:not([data-fold-key])');
    if (!fresh.length) return;
    new Set([...fresh].map(ownerOf)).forEach(owner => {
      const seen = new Map();
      owner.querySelectorAll('details.nx-fold').forEach(d => {
        if (ownerOf(d) !== owner) return; // belongs to an inner owner
        const label = foldLabel(d);
        const n = seen.get(label) || 0;
        seen.set(label, n + 1);
        if (d.dataset.foldKey) return;
        d.dataset.foldKey = `${owner.id}|${label}|${n}`;
        if (openFolds.has(d.dataset.foldKey)) d.open = true;
      });
    });
  }
  function remember(d) {
    const key = d.dataset.foldKey;
    if (!key) return;
    openFolds.delete(key);
    if (!d.open) return;
    openFolds.add(key);
    if (openFolds.size > MAX_REMEMBERED) openFolds.delete(openFolds.values().next().value);
  }
  document.addEventListener('toggle', e => {
    if (e.target instanceof HTMLDetailsElement && e.target.classList.contains('nx-fold')) remember(e.target);
  }, true);

  // Definition lists that print raw field names (e.g. evidence_kind) get the field's label.
  function labelTerm(dt) {
    const key = dt.textContent.trim();
    if (dt.children.length || !/^[a-z][a-z0-9_]*$/.test(key) || !R.hasLabel(key) || skipped(dt)) return;
    dt.textContent = R.label(key);
    dt.dataset.key = key;
  }

  function scan(root) {
    if (root.nodeType === Node.TEXT_NODE) { convertTextNode(root); return; }
    if (root.nodeType !== Node.ELEMENT_NODE || skipped(root)) return;
    if (root.tagName === 'PRE') { convertPre(root); return; }
    if (root.tagName === 'DT') { labelTerm(root); return; }
    root.querySelectorAll('pre').forEach(convertPre);
    root.querySelectorAll('dt').forEach(labelTerm);
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: n => (needsText(n.textContent.trim()) || (n.parentElement && inLog(n.parentElement)) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP),
    });
    const hits = [];
    while (walker.nextNode()) hits.push(walker.currentNode);
    hits.forEach(convertTextNode);
  }

  // Re-renders replace whole subtrees: read the open state of the folds they removed (the
  // `toggle` event is asynchronous and can arrive after the re-render), then convert the new ones.
  function rememberRemoved(node) {
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    const folds = node.matches('details.nx-fold') ? [node] : node.querySelectorAll('details.nx-fold');
    folds.forEach(remember);
  }

  new MutationObserver(records => {
    for (const record of records) record.removedNodes.forEach(rememberRemoved);
    for (const record of records) {
      if (record.type === 'characterData') scan(record.target);
      else record.addedNodes.forEach(n => { if (n.isConnected) scan(n); });
    }
    assignKeys();
  }).observe(document.documentElement, {childList: true, subtree: true, characterData: true});

  const start = () => { scan(document.body); assignKeys(); };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
