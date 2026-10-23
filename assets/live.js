'use strict';
// A serial poll reads a complete atomic snapshot. Committed DOM nodes are never
// replaced, preserving selections, open source details, focus and reading position.
const byId = id => document.getElementById(id);
const chapters = new Map();
const events = new Map();
const notice = byId('new-events');
let gameId;
let revision = 0;
let initialized = false;
let following = true;
let unseen = 0;
let busy = false;
let finished = false;
let timer;
let offlineUrl;

function nearBottom() {
  return document.documentElement.scrollHeight - innerHeight - scrollY < 120;
}
function followLatest() {
  following = true;
  unseen = 0;
  notice.hidden = true;
  scrollTo({top: document.documentElement.scrollHeight, behavior: 'instant'});
}
byId('follow').addEventListener('click', followLatest);
notice.addEventListener('click', followLatest);
addEventListener('scroll', () => {
  following = nearBottom();
  if (following) {
    unseen = 0;
    notice.hidden = true;
  }
}, {passive: true});

function captureAnchor() {
  const cutoff = document.querySelector('.live-bar').getBoundingClientRect().bottom;
  return [...document.querySelectorAll('#hero, .chapter-header, .event, .appendix')]
    .find(node => node.getBoundingClientRect().bottom > cutoff);
}
function appendMarkup(parent, markup) {
  // Markup is produced by the escaping Python renderer, never by player HTML.
  const template = document.createElement('template');
  template.innerHTML = markup;
  const node = template.content.firstElementChild;
  parent.append(template.content);
  return node;
}

function applySnapshot(snapshot) {
  if (gameId && gameId !== snapshot.game_id) throw new Error('game changed');
  if (snapshot.revision <= revision) return;
  const anchor = initialized ? captureAnchor() : null;
  const offset = anchor?.getBoundingClientRect().top;
  const previousY = scrollY;
  const shouldFollow = initialized && following && nearBottom();
  const previousCount = events.size;
  if (!initialized) {
    byId('hero').innerHTML = snapshot.hero_html;
    byId('hero').querySelector('.eyebrow').textContent = 'WEREWOLF · LIVE TIMELINE';
    document.title = `${snapshot.replay.title} · 实时观战`;
  }
  for (const chapter of snapshot.chapters) {
    let section = chapters.get(chapter.id);
    if (!section) {
      section = document.createElement('section');
      section.id = chapter.anchor;
      section.className = 'chapter';
      section.dataset.chapterId = chapter.id;
      section.innerHTML = chapter.header_html;
      byId('chapters').append(section);
      chapters.set(chapter.id, section);
      const link = document.createElement('a');
      link.href = `#${chapter.anchor}`;
      link.textContent = snapshot.replay.chapters[chapters.size - 1].title;
      byId('navigation').append(link);
    }
    for (const event of chapter.events) {
      if (events.has(event.id)) continue;
      events.set(event.id, appendMarkup(section.querySelector('.chapter-events'), event.html));
    }
  }
  const progress = snapshot.progress;
  byId('progress').textContent = progress.stage + (progress.expected === null ? '' : ` · 已收到 ${progress.received}/${progress.expected}`);
  byId('message').textContent = progress.message;
  byId('revision').textContent = `修订 ${snapshot.revision} · ${events.size} 条事件`;
  byId('published').textContent = `法官最近发布：${new Date(snapshot.published_at).toLocaleString()}`;
  // Only the stat text changes; never replace the cast or an existing event.
  const stats = byId('hero').querySelectorAll('.stats b');
  [snapshot.replay.players.length, chapters.size, events.size, snapshot.replay.records.length]
    .forEach((value, index) => { if (stats[index]) stats[index].textContent = value; });
  if (snapshot.status === 'finished') {
    byId('appendices').innerHTML = snapshot.appendix_html;
    offlineUrl = URL.createObjectURL(new Blob([snapshot.final_html], {type: 'text/html;charset=utf-8'}));
    const link = byId('offline');
    link.href = offlineUrl;
    link.download = '狼人杀复盘.html';
    link.hidden = false;
    byId('final-path').textContent = `离线文件已保存：${snapshot.final_path}`;
    byId('final-path').hidden = false;
    finished = true;
  }
  if (shouldFollow) followLatest();
  else if (anchor?.isConnected) scrollBy({top: anchor.getBoundingClientRect().top - offset, behavior: 'instant'});
  else scrollTo({top: previousY, behavior: 'instant'});
  if (initialized && !shouldFollow) {
    unseen += events.size - previousCount;
    notice.textContent = `新增 ${unseen} 条 · 查看最新`;
    notice.hidden = unseen === 0;
  }
  initialized = true;
  revision = snapshot.revision;
  gameId = snapshot.game_id;
}

async function poll() {
  if (busy || finished) return;
  busy = true;
  clearTimeout(timer);
  try {
    const response = await fetch('/snapshot.json', {cache: 'no-store', signal: AbortSignal.timeout(5000)});
    if (!response.ok) throw new Error('snapshot unavailable');
    const snapshot = await response.json();
    applySnapshot(snapshot);
    byId('connection').textContent = finished ? '已结束 · 离线复盘已保存' : '已连接 · 自动同步';
    byId('connection').className = '';
    if (finished) {
      // Acknowledge after the full export is loaded. The CLI will then close
      // its temporary service; this page and its download link still work.
      await fetch(`/ack-final?revision=${revision}`, {method: 'POST'}).catch(() => {});
    }
  } catch (_error) {
    byId('connection').textContent = '连接中断 · 保留已读内容并重试';
    byId('connection').className = 'error';
  } finally {
    busy = false;
    if (!finished) timer = setTimeout(poll, document.hidden ? 5000 : 1000);
  }
}
document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
addEventListener('pagehide', event => { if (!event.persisted && offlineUrl) URL.revokeObjectURL(offlineUrl); });
poll();
